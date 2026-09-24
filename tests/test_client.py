"""Tests for date range handling, pagination and error paths."""

from __future__ import annotations

from datetime import date, datetime

import httpx
import pytest
import respx

from oura_mcp.auth import OuraAuth, TokenSet, TokenStore
from oura_mcp.client import (
    OuraApiError,
    OuraClient,
    OuraUnavailableError,
    daterange_params,
)


def _auth(tmp_path) -> OuraAuth:
    store = TokenStore(tmp_path / "tokens.json")
    store.save(
        TokenSet(
            access_token="valid-token",
            refresh_token="refresh",
            expires_at=9_999_999_999.0,
        )
    )
    return OuraAuth(client_id="cid", client_secret="secret", store=store)


class TestDaterangeParams:
    def test_end_date_is_made_exclusive(self):
        """Oura's end_date is exclusive, our interface is inclusive."""
        params = daterange_params(date(2027, 5, 1), date(2027, 5, 3))
        assert params == {"start_date": "2027-05-01", "end_date": "2027-05-04"}

    def test_single_day_range_is_not_empty(self):
        """The most common mistake: a single day must not come back empty."""
        params = daterange_params(date(2027, 5, 1), date(2027, 5, 1))
        assert params["start_date"] == "2027-05-01"
        assert params["end_date"] == "2027-05-02"

    def test_reversed_range_is_rejected(self):
        with pytest.raises(ValueError, match="is before"):
            daterange_params(date(2027, 5, 3), date(2027, 5, 1))


@respx.mock
async def test_pagination_follows_next_token(tmp_path):
    route = respx.get("https://api.ouraring.com/v2/usercollection/daily_sleep").mock(
        side_effect=[
            httpx.Response(200, json={"data": [{"day": "2027-05-01"}], "next_token": "t2"}),
            httpx.Response(200, json={"data": [{"day": "2027-05-02"}], "next_token": None}),
        ]
    )
    async with OuraClient(_auth(tmp_path)) as client:
        records = await client.async_get_range(
            "daily_sleep", date(2027, 5, 1), date(2027, 5, 2)
        )

    assert [r["day"] for r in records] == ["2027-05-01", "2027-05-02"]
    assert route.call_count == 2
    # The follow-up page must send only next_token, not the old filters.
    second_query = dict(httpx.URL(str(route.calls[1].request.url)).params)
    assert second_query == {"next_token": "t2"}


@respx.mock
@pytest.mark.parametrize(
    ("start", "end", "expected_windows"),
    [
        # Exactly one window, no remainder.
        (datetime(2027, 1, 1), datetime(2027, 1, 31), 1),
        # 59 days: 01 Jan - 31 Jan and 31 Jan - 01 Mar.
        (datetime(2027, 1, 1), datetime(2027, 3, 1), 2),
        # 70 days force a third, short window.
        (datetime(2027, 1, 1), datetime(2027, 3, 12), 3),
        # A single day must not end up as zero requests.
        (datetime(2027, 1, 1, 6, 0), datetime(2027, 1, 1, 22, 0), 1),
    ],
)
async def test_heartrate_is_split_into_30_day_windows(tmp_path, start, end, expected_windows):
    """Oura rejects heart rate requests longer than 30 days, so we chunk them."""
    route = respx.get("https://api.ouraring.com/v2/usercollection/heartrate").mock(
        return_value=httpx.Response(200, json={"data": [], "next_token": None})
    )
    async with OuraClient(_auth(tmp_path)) as client:
        await client.async_get_heartrate(start, end)
    assert route.call_count == expected_windows
    # No window may cross the limit, otherwise Oura rejects it.
    for call in route.calls:
        params = httpx.URL(str(call.request.url)).params
        window_start = datetime.fromisoformat(params["start_datetime"])
        window_end = datetime.fromisoformat(params["end_datetime"])
        assert (window_end - window_start).days <= 30
        assert window_start >= start
        assert window_end <= end


@respx.mock
async def test_heartrate_rejects_reversed_range(tmp_path):
    respx.get("https://api.ouraring.com/v2/usercollection/heartrate")
    async with OuraClient(_auth(tmp_path)) as client:
        with pytest.raises(ValueError, match="is before"):
            await client.async_get_heartrate(datetime(2027, 3, 1), datetime(2027, 1, 1))


@respx.mock
async def test_optional_endpoint_403_is_unavailable_not_error(tmp_path):
    """A missing membership is a state, not a crash."""
    respx.get("https://api.ouraring.com/v2/usercollection/daily_resilience").mock(
        return_value=httpx.Response(403)
    )
    async with OuraClient(_auth(tmp_path)) as client:
        with pytest.raises(OuraUnavailableError):
            await client.async_get_range(
                "daily_resilience", date(2027, 5, 1), date(2027, 5, 2)
            )


@respx.mock
async def test_mandatory_endpoint_403_still_raises_api_error(tmp_path):
    respx.get("https://api.ouraring.com/v2/usercollection/daily_sleep").mock(
        return_value=httpx.Response(403)
    )
    async with OuraClient(_auth(tmp_path)) as client:
        with pytest.raises(OuraApiError) as excinfo:
            await client.async_get_range("daily_sleep", date(2027, 5, 1), date(2027, 5, 2))
        assert not isinstance(excinfo.value, OuraUnavailableError)


@respx.mock
async def test_429_is_retried_once_honouring_retry_after(tmp_path, monkeypatch):
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("oura_mcp.client.asyncio.sleep", fake_sleep)
    route = respx.get("https://api.ouraring.com/v2/usercollection/daily_sleep").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, json={"data": [{"day": "2027-05-01"}]}),
        ]
    )
    async with OuraClient(_auth(tmp_path)) as client:
        records = await client.async_get_range(
            "daily_sleep", date(2027, 5, 1), date(2027, 5, 1)
        )

    assert len(records) == 1
    assert route.call_count == 2
    assert slept == [2.0]


@respx.mock
async def test_retry_after_is_capped(tmp_path, monkeypatch):
    """An absurd Retry-After must not block the MCP call indefinitely."""
    slept: list[float] = []
    monkeypatch.setattr("oura_mcp.client.asyncio.sleep", lambda s: slept.append(s) or _noop())
    respx.get("https://api.ouraring.com/v2/usercollection/daily_sleep").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "86400"}),
            httpx.Response(200, json={"data": []}),
        ]
    )
    async with OuraClient(_auth(tmp_path)) as client:
        await client.async_get_range("daily_sleep", date(2027, 5, 1), date(2027, 5, 1))
    assert slept == [30.0]


async def _noop() -> None:
    return None
