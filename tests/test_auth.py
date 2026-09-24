"""Tests for OAuth: endpoint order, token persistence, secret leaks."""

from __future__ import annotations

import json
import logging
import stat

import httpx
import pytest
import respx

from oura_mcp.auth import (
    OuraAuth,
    OuraAuthError,
    TokenSet,
    TokenStore,
    _async_post_token,
    async_exchange_code,
)
from oura_mcp.const import OAUTH2_TOKEN, OAUTH2_TOKEN_LEGACY

_MOI = "https://moi.ouraring.com/oauth/v2/ext/oauth-token"
_LEGACY = "https://api.ouraring.com/oauth/token"

_TOKEN_RESPONSE = {
    "access_token": "new-access",
    "refresh_token": "new-refresh",
    "expires_in": 86400,
    "scope": "daily personal",
}


class TestTokenStore:
    def test_saved_file_is_owner_only(self, tmp_path):
        """The file holds plaintext tokens and must belong to nobody else."""
        store = TokenStore(tmp_path / "tokens.json")
        store.save(TokenSet("a", "r", 123.0))
        mode = stat.S_IMODE((tmp_path / "tokens.json").stat().st_mode)
        assert mode == 0o600

    def test_roundtrip(self, tmp_path):
        store = TokenStore(tmp_path / "nested" / "tokens.json")
        store.save(TokenSet("access", "refresh", 555.0, scope="daily"))
        loaded = store.load()
        assert loaded.access_token == "access"
        assert loaded.refresh_token == "refresh"
        assert loaded.scope == "daily"

    def test_missing_store_names_the_remedy(self, tmp_path):
        with pytest.raises(OuraAuthError, match="oura-mcp auth"):
            TokenStore(tmp_path / "absent.json").load()

    def test_no_temp_file_is_left_behind(self, tmp_path):
        store = TokenStore(tmp_path / "tokens.json")
        store.save(TokenSet("a", "r", 1.0))
        assert [p.name for p in tmp_path.iterdir()] == ["tokens.json"]

    def test_corrupt_store_is_reported_not_swallowed(self, tmp_path):
        path = tmp_path / "tokens.json"
        path.write_text("{ not json", encoding="utf-8")
        with pytest.raises(OuraAuthError, match="unreadable"):
            TokenStore(path).load()


class TestTokenEndpointOrder:
    """The order is not cosmetic.

    Authorization codes are single-use (RFC 6749 section 4.1.2). Whoever asks
    the legacy endpoint first and is rejected there has burned the code --
    the fallback then receives a dead code.
    """

    @respx.mock
    async def test_moi_is_tried_first(self):
        moi = respx.post(_MOI).mock(return_value=httpx.Response(200, json=_TOKEN_RESPONSE))
        legacy = respx.post(_LEGACY)

        async with httpx.AsyncClient() as client:
            tokens = await _async_post_token(client, {"grant_type": "authorization_code"})

        assert moi.call_count == 1
        assert legacy.call_count == 0
        assert tokens.token_endpoint == OAUTH2_TOKEN

    @respx.mock
    async def test_falls_back_to_legacy_on_4xx(self):
        respx.post(_MOI).mock(return_value=httpx.Response(401))
        legacy = respx.post(_LEGACY).mock(
            return_value=httpx.Response(200, json=_TOKEN_RESPONSE)
        )

        async with httpx.AsyncClient() as client:
            tokens = await _async_post_token(client, {"grant_type": "refresh_token"})

        assert legacy.call_count == 1
        assert tokens.token_endpoint == OAUTH2_TOKEN_LEGACY

    @respx.mock
    async def test_no_fallback_on_server_error(self):
        """A 500 is an outage, not a hint that we picked the wrong endpoint."""
        respx.post(_MOI).mock(return_value=httpx.Response(500))
        legacy = respx.post(_LEGACY)

        async with httpx.AsyncClient() as client:
            with pytest.raises(OuraAuthError):
                await _async_post_token(client, {"grant_type": "refresh_token"})

        assert legacy.call_count == 0

    @respx.mock
    async def test_known_endpoint_is_reused_without_retrying_moi(self):
        """Whoever is already authorized against legacy should stay there."""
        moi = respx.post(_MOI)
        legacy = respx.post(_LEGACY).mock(
            return_value=httpx.Response(200, json=_TOKEN_RESPONSE)
        )

        async with httpx.AsyncClient() as client:
            await _async_post_token(
                client, {"grant_type": "refresh_token"}, preferred_endpoint=_LEGACY
            )

        assert moi.call_count == 0
        assert legacy.call_count == 1

    @respx.mock
    async def test_error_message_names_the_likely_causes(self):
        respx.post(_MOI).mock(return_value=httpx.Response(400))
        respx.post(_LEGACY).mock(return_value=httpx.Response(400))

        async with httpx.AsyncClient() as client:
            with pytest.raises(OuraAuthError) as excinfo:
                await _async_post_token(client, {"grant_type": "refresh_token"})

        message = str(excinfo.value)
        assert "developer.ouraring.com" in message
        assert "single-use" in message


class TestRefresh:
    @respx.mock
    async def test_new_refresh_token_is_persisted_before_use(self, tmp_path):
        """Oura invalidates the old refresh token. If we lose the new one,
        the authorization is irrecoverably dead."""
        respx.post(_MOI).mock(return_value=httpx.Response(200, json=_TOKEN_RESPONSE))
        store = TokenStore(tmp_path / "tokens.json")
        store.save(TokenSet("old-access", "old-refresh", expires_at=0.0))
        auth = OuraAuth(client_id="cid", client_secret="secret", store=store)

        async with httpx.AsyncClient() as client:
            token = await auth.async_access_token(client)

        assert token == "new-access"
        on_disk = json.loads((tmp_path / "tokens.json").read_text(encoding="utf-8"))
        assert on_disk["refresh_token"] == "new-refresh"

    @respx.mock
    async def test_valid_token_triggers_no_network_call(self, tmp_path):
        route = respx.post(_MOI)
        store = TokenStore(tmp_path / "tokens.json")
        store.save(TokenSet("still-good", "refresh", expires_at=9_999_999_999.0))
        auth = OuraAuth(client_id="cid", client_secret="secret", store=store)

        async with httpx.AsyncClient() as client:
            assert await auth.async_access_token(client) == "still-good"

        assert route.call_count == 0

    @respx.mock
    async def test_concurrent_access_refreshes_only_once(self, tmp_path):
        """Two parallel refreshes would redeem the single-use token twice."""
        import asyncio

        route = respx.post(_MOI).mock(return_value=httpx.Response(200, json=_TOKEN_RESPONSE))
        store = TokenStore(tmp_path / "tokens.json")
        store.save(TokenSet("old", "old-refresh", expires_at=0.0))
        auth = OuraAuth(client_id="cid", client_secret="secret", store=store)

        async with httpx.AsyncClient() as client:
            results = await asyncio.gather(
                *(auth.async_access_token(client) for _ in range(5))
            )

        assert set(results) == {"new-access"}
        assert route.call_count == 1

    @respx.mock
    async def test_secrets_never_reach_the_log(self, tmp_path, caplog):
        """Regression guard: rejected requests must not log any tokens."""
        respx.post(_MOI).mock(
            return_value=httpx.Response(
                400, json={"error": "invalid_grant", "refresh_token": "leaked-value"}
            )
        )
        respx.post(_LEGACY).mock(return_value=httpx.Response(400))
        store = TokenStore(tmp_path / "tokens.json")
        store.save(TokenSet("old", "super-secret-refresh", expires_at=0.0))
        auth = OuraAuth(client_id="cid", client_secret="hunter2", store=store)

        with caplog.at_level(logging.DEBUG):
            async with httpx.AsyncClient() as client:
                with pytest.raises(OuraAuthError):
                    await auth.async_access_token(client)

        combined = caplog.text
        for secret in ("super-secret-refresh", "hunter2", "leaked-value"):
            assert secret not in combined


class TestExchangeCode:
    @respx.mock
    async def test_missing_refresh_token_is_reported(self):
        respx.post(_MOI).mock(
            return_value=httpx.Response(200, json={"access_token": "only-access"})
        )
        with pytest.raises(OuraAuthError, match="refresh_token"):
            await async_exchange_code(
                "cid", "secret", "code", "http://localhost:8765/callback"
            )
