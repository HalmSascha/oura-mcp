"""HTTP client for the Oura Cloud API v2."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from . import __version__
from .auth import OuraAuth, OuraAuthError
from .const import (
    API_BASE_URL,
    DEFAULT_TIMEOUT_SECONDS,
    HEARTRATE_MAX_WINDOW_DAYS,
    MAX_RETRY_AFTER_SECONDS,
    OPTIONAL_ENDPOINTS,
)

_LOGGER = logging.getLogger(__name__)


class OuraApiError(RuntimeError):
    """The Oura API reported an error."""


class OuraUnavailableError(OuraApiError):
    """Endpoint not available for this account or ring generation.

    Not a failure in any meaningful sense: without an active membership, or on
    older hardware, Oura answers 401/403 here and that is the normal state.
    """


def daterange_params(start: date, end: date) -> dict[str, str]:
    """Build ``start_date``/``end_date`` for an **inclusive** range.

    Oura's ``end_date`` is exclusive. Asking for 2024-05-01 through 2024-05-01
    would otherwise return nothing instead of that one day. This server behaves
    inclusively on the outside because that is what callers expect.
    """
    if end < start:
        raise ValueError(f"end date {end} is before start date {start}")
    return {
        "start_date": start.isoformat(),
        "end_date": (end + timedelta(days=1)).isoformat(),
    }


class OuraClient:
    """Thin wrapper over the Oura REST API with pagination and rate limiting."""

    def __init__(self, auth: OuraAuth, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._auth = auth
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "User-Agent": (
                    f"oura-mcp/{__version__} (+https://github.com/HalmSascha/oura-mcp)"
                )
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> OuraClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # --- internal request handling ----------------------------------------

    async def _async_request(
        self, endpoint: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        url = f"{API_BASE_URL}/{endpoint}"
        token = await self._auth.async_access_token(self._client)
        response = await self._client.get(
            url, params=params, headers={"Authorization": f"Bearer {token}"}
        )

        # A 401 can occur even with a formally valid token. Force one refresh
        # and retry before giving up.
        if response.status_code == 401:
            await self._auth.async_invalidate()
            try:
                token = await self._auth.async_access_token(self._client)
            except OuraAuthError:
                raise
            response = await self._client.get(
                url, params=params, headers={"Authorization": f"Bearer {token}"}
            )

        if response.status_code == 429:
            delay = _retry_after_seconds(response)
            _LOGGER.warning(
                "Rate limited on %s, waiting %.1fs and retrying once",
                endpoint,
                delay,
            )
            await asyncio.sleep(delay)
            response = await self._client.get(
                url, params=params, headers={"Authorization": f"Bearer {token}"}
            )

        if response.status_code in (401, 403) and endpoint in OPTIONAL_ENDPOINTS:
            raise OuraUnavailableError(
                f"{endpoint} is not available for this account "
                f"(HTTP {response.status_code}). Usually this means no active "
                "Oura membership, or a ring generation that does not record "
                "this measurement."
            )

        if not response.is_success:
            raise OuraApiError(f"Oura API returned {response.status_code} for {endpoint}")

        payload = response.json()
        if not isinstance(payload, dict):
            raise OuraApiError(f"Unexpected response shape from {endpoint}")
        return payload

    async def _async_all_pages(
        self, endpoint: str, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Follow ``next_token`` until every page has been fetched."""
        records: list[dict[str, Any]] = []
        current = dict(params)
        while True:
            payload = await self._async_request(endpoint, current)
            records.extend(payload.get("data") or [])
            next_token = payload.get("next_token")
            if not next_token:
                return records
            # On subsequent pages next_token replaces the other filters entirely.
            current = {"next_token": next_token}

    # --- public fetches ---------------------------------------------------

    async def async_get_range(
        self, endpoint: str, start: date, end: date
    ) -> list[dict[str, Any]]:
        """Fetch a day-based endpoint for an inclusive date range."""
        return await self._async_all_pages(endpoint, daterange_params(start, end))

    async def async_get_static(self, endpoint: str) -> dict[str, Any]:
        """Fetch an endpoint that takes no time range."""
        return await self._async_request(endpoint)

    async def async_get_heartrate(
        self, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        """Fetch heart rate time series, split into 30-day windows.

        Oura rejects requests spanning longer periods, so this fetches in
        chunks and stitches the result together.
        """
        if end < start:
            raise ValueError(f"end time {end} is before start time {start}")
        records: list[dict[str, Any]] = []
        window_start = start
        while window_start < end:
            window_end = min(window_start + timedelta(days=HEARTRATE_MAX_WINDOW_DAYS), end)
            records.extend(
                await self._async_all_pages(
                    "heartrate",
                    {
                        "start_datetime": window_start.isoformat(),
                        "end_datetime": window_end.isoformat(),
                    },
                )
            )
            window_start = window_end
        return records


def _retry_after_seconds(response: httpx.Response) -> float:
    """Read ``Retry-After``, capped at a tolerable wait."""
    raw = response.headers.get("Retry-After")
    try:
        delay = float(raw) if raw is not None else 1.0
    except ValueError:
        # Retry-After may also be an HTTP date per RFC. Rather than parse that,
        # fall back to the default -- the call is retried only once anyway.
        delay = 1.0
    return min(max(delay, 0.0), MAX_RETRY_AFTER_SECONDS)
