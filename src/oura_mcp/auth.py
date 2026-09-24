"""OAuth2 handling for the Oura Cloud API.

Two things make this harder than an ordinary OAuth client:

1. **Oura refresh tokens are single-use.** Every refresh returns a new one and
   invalidates the old. If the response is lost before it is persisted, the
   authorization is gone for good and the user has to re-run the CLI flow.
   Hence: write before returning, atomically via ``os.replace``, and an
   ``asyncio.Lock`` so concurrent callers cannot redeem the same token twice.

2. **Two token endpoints**, see the comment in :mod:`.const`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .const import (
    DEFAULT_TIMEOUT_SECONDS,
    OAUTH2_AUTHORIZE,
    OAUTH2_SCOPES,
    OAUTH2_TOKEN,
    OAUTH2_TOKEN_LEGACY,
    TOKEN_FALLBACK_STATUSES,
    TOKEN_REFRESH_MARGIN_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


class OuraAuthError(RuntimeError):
    """Authorization failed or was never established."""


@dataclass(slots=True)
class TokenSet:
    """A set of OAuth tokens plus its expiry."""

    access_token: str
    refresh_token: str
    expires_at: float
    scope: str = ""
    token_endpoint: str = OAUTH2_TOKEN

    @property
    def expired(self) -> bool:
        """Is the access token expired or about to be? Includes a safety margin."""
        return time.time() >= self.expires_at - TOKEN_REFRESH_MARGIN_SECONDS

    @classmethod
    def from_response(cls, payload: dict[str, Any], endpoint: str) -> TokenSet:
        try:
            access_token = payload["access_token"]
            refresh_token = payload["refresh_token"]
        except KeyError as err:
            raise OuraAuthError(
                f"Token response missing field {err.args[0]!r} from {endpoint}"
            ) from err
        # Oura reports expires_in in seconds. Default to 24h if absent.
        expires_in = float(payload.get("expires_in") or 86400)
        return cls(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=time.time() + expires_in,
            scope=payload.get("scope", ""),
            token_endpoint=endpoint,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scope": self.scope,
            "token_endpoint": self.token_endpoint,
        }


class TokenStore:
    """Persists tokens as JSON with file mode 0600."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._path.is_file()

    def load(self) -> TokenSet:
        if not self.exists():
            raise OuraAuthError(f"No token store at {self._path}. Run 'oura-mcp auth' once.")
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            raise OuraAuthError(f"Token store {self._path} unreadable: {err}") from err
        return TokenSet(
            access_token=raw["access_token"],
            refresh_token=raw["refresh_token"],
            expires_at=float(raw["expires_at"]),
            scope=raw.get("scope", ""),
            token_endpoint=raw.get("token_endpoint", OAUTH2_TOKEN),
        )

    def save(self, tokens: TokenSet) -> None:
        """Write atomically: temporary file first, then rename.

        A half-written store is equivalent to a lost refresh token, because the
        old one has already been invalidated by the time we get here.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(f".{os.getpid()}.tmp")
        payload = json.dumps(tokens.to_dict(), indent=2)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, self._path)
        os.chmod(self._path, 0o600)


@dataclass(slots=True)
class OuraAuth:
    """Keeps tokens current and hands out valid access tokens."""

    client_id: str
    client_secret: str
    store: TokenStore
    _tokens: TokenSet | None = field(default=None, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def async_access_token(self, client: httpx.AsyncClient) -> str:
        """Return a valid access token, refreshing if necessary."""
        async with self._lock:
            if self._tokens is None:
                self._tokens = self.store.load()
            if self._tokens.expired:
                self._tokens = await self._async_refresh(client, self._tokens)
            return self._tokens.access_token

    async def async_invalidate(self) -> None:
        """Force a refresh on the next access.

        Call this when the API returns 401 despite a seemingly valid token --
        for instance after clock drift, or when Oura revoked it early.
        """
        async with self._lock:
            if self._tokens is not None:
                self._tokens.expires_at = 0.0

    async def _async_refresh(self, client: httpx.AsyncClient, tokens: TokenSet) -> TokenSet:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        refreshed = await _async_post_token(
            client, data, preferred_endpoint=tokens.token_endpoint
        )
        # Persist before returning: the old refresh token is dead from now on.
        self.store.save(refreshed)
        _LOGGER.info(
            "Access token refreshed (endpoint %s, valid for %.0fs)",
            refreshed.token_endpoint,
            refreshed.expires_at - time.time(),
        )
        return refreshed


async def _async_post_token(
    client: httpx.AsyncClient,
    data: dict[str, str],
    *,
    preferred_endpoint: str = OAUTH2_TOKEN,
) -> TokenSet:
    """Request a token, falling back to the legacy endpoint.

    The order is deliberate: `moi` first. For the authorization code grant, a
    failed legacy attempt would consume the code and render the fallback
    useless.
    """
    endpoints = [preferred_endpoint]
    if preferred_endpoint != OAUTH2_TOKEN_LEGACY:
        endpoints.append(OAUTH2_TOKEN_LEGACY)

    last_error: Exception | None = None
    for endpoint in endpoints:
        try:
            response = await client.post(endpoint, data=data)
        except httpx.HTTPError as err:
            _LOGGER.warning("Token endpoint %s unreachable: %s", endpoint, err)
            last_error = err
            continue

        if response.is_success:
            return TokenSet.from_response(response.json(), endpoint)

        # Deliberately without the response body: it can contain tokens.
        _LOGGER.error(
            "Token request rejected: endpoint=%s grant_type=%s status=%s",
            httpx.URL(endpoint).host,
            data.get("grant_type"),
            response.status_code,
        )
        last_error = OuraAuthError(
            f"Token endpoint {httpx.URL(endpoint).host} returned {response.status_code}"
        )
        if response.status_code not in TOKEN_FALLBACK_STATUSES:
            break

    raise OuraAuthError(
        "No token endpoint accepted the request. Common causes: the application "
        "was created in the wrong portal (the correct one is "
        "https://developer.ouraring.com/applications), the redirect URI does not "
        "match exactly, the client secret was regenerated, or the refresh token "
        "was already used (Oura refresh tokens are single-use)."
    ) from last_error


def build_authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Build the URL where the user approves the application."""
    params = httpx.QueryParams(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(OAUTH2_SCOPES),
            "state": state,
        }
    )
    return f"{OAUTH2_AUTHORIZE}?{params}"


def new_state() -> str:
    """CSRF state for the authorization code flow."""
    return secrets.token_urlsafe(24)


async def async_exchange_code(
    client_id: str, client_secret: str, code: str, redirect_uri: str
) -> TokenSet:
    """Exchange an authorization code for tokens."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS) as client:
        return await _async_post_token(client, data)
