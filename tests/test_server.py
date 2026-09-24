"""Tests for the MCP layer: schemas, error propagation, analysis logic."""

from __future__ import annotations

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from oura_mcp import server as server_module
from oura_mcp.auth import OuraAuth, TokenSet, TokenStore
from oura_mcp.client import OuraClient
from oura_mcp.server import mcp

_BASE = "https://api.ouraring.com/v2/usercollection"


@pytest.fixture(autouse=True)
def _reset_client_singleton():
    """The module singleton must not bleed through between tests."""
    server_module._client = None
    yield
    server_module._client = None


@pytest.fixture
def wired_client(tmp_path, monkeypatch):
    """Wire up a client with a valid token, without network access for OAuth."""
    store = TokenStore(tmp_path / "tokens.json")
    store.save(TokenSet("valid", "refresh", expires_at=9_999_999_999.0))
    monkeypatch.setenv("OURA_CLIENT_ID", "cid")
    monkeypatch.setenv("OURA_CLIENT_SECRET", "secret")
    monkeypatch.setenv("OURA_TOKEN_STORE", str(store.path))
    server_module._client = OuraClient(
        OuraAuth(client_id="cid", client_secret="secret", store=store)
    )
    return server_module._client


class TestToolRegistration:
    async def test_all_tools_are_registered(self):
        tools = await mcp.list_tools()
        names = {t.name for t in tools}
        assert names == {
            "get_sleep",
            "get_sleep_detail",
            "get_readiness",
            "get_activity",
            "get_workouts",
            "get_sessions",
            "get_tags",
            "get_stress",
            "get_resilience",
            "get_spo2",
            "get_heart_health",
            "get_heartrate",
            "get_personal_info",
            "get_ring_configuration",
            "get_status",
            "get_event_window",
        }

    async def test_decorator_preserves_schemas(self):
        """`@anticipated` must not swallow the schemas derived from the
        signature -- otherwise the client only ever sees *args/**kwargs."""
        tools = {t.name: t for t in await mcp.list_tools()}

        sleep = tools["get_sleep"].input_schema
        assert set(sleep["required"]) == {"start_date", "end_date"}

        window = tools["get_event_window"].input_schema
        assert set(window["required"]) == {"event_date"}
        assert window["properties"]["days_before"]["default"] == 7
        assert window["properties"]["days_after"]["default"] == 7

        assert not tools["get_status"].input_schema.get("required")

    async def test_descriptions_are_present(self):
        """Without a description a model cannot choose the tools sensibly."""
        for tool in await mcp.list_tools():
            assert tool.description, f"{tool.name} without a description"


class TestErrorPropagation:
    """The SDK suppresses the message of every exception other than ``ToolError``.

    Without the `@anticipated` decorator the caller would see only
    "Error executing tool get_sleep" -- that is, nothing usable at exactly the
    moment a hint is needed most.
    """

    async def _message(self, tool: str, args: dict[str, object]) -> str:
        with pytest.raises(ToolError) as excinfo:
            await mcp.call_tool(tool, args)
        assert not isinstance(excinfo.value, UnexpectedToolError), (
            f"{tool} was treated as a crash, the message was lost"
        )
        return str(excinfo.value)

    async def test_bad_date_format_reaches_the_caller(self, wired_client):
        message = await self._message(
            "get_sleep", {"start_date": "01.05.2027", "end_date": "2027-05-02"}
        )
        assert "YYYY-MM-DD" in message
        assert "01.05.2027" in message

    async def test_negative_window_is_rejected_with_reason(self, wired_client):
        message = await self._message(
            "get_event_window", {"event_date": "2027-05-01", "days_before": -3}
        )
        assert "negative" in message

    async def test_oversized_window_names_the_alternative(self, wired_client):
        message = await self._message(
            "get_event_window",
            {"event_date": "2027-05-01", "days_before": 120, "days_after": 120},
        )
        assert "180" in message

    async def test_missing_token_store_names_the_remedy(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OURA_CLIENT_ID", "cid")
        monkeypatch.setenv("OURA_CLIENT_SECRET", "secret")
        monkeypatch.setenv("OURA_TOKEN_STORE", str(tmp_path / "absent.json"))
        message = await self._message(
            "get_readiness", {"start_date": "2027-05-01", "end_date": "2027-05-02"}
        )
        assert "oura-mcp auth" in message

    async def test_missing_credentials_are_named(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OURA_CLIENT_ID", raising=False)
        monkeypatch.delenv("OURA_CLIENT_SECRET", raising=False)
        monkeypatch.setenv("OURA_TOKEN_STORE", str(tmp_path / "absent.json"))
        message = await self._message(
            "get_activity", {"start_date": "2027-05-01", "end_date": "2027-05-02"}
        )
        assert "OURA_CLIENT_ID" in message

    @respx.mock
    async def test_unavailable_endpoint_returns_state_not_error(self, wired_client):
        """A missing membership is an answer, not an error."""
        respx.get(f"{_BASE}/daily_resilience").mock(return_value=httpx.Response(403))
        result = await mcp.call_tool(
            "get_resilience", {"start_date": "2027-05-01", "end_date": "2027-05-02"}
        )
        payload = result.structured_content
        assert payload["available"] is False
        assert payload["data"] == []


class TestEventWindow:
    @respx.mock
    async def test_timeline_aligns_sources_on_offset_axis(self, wired_client):
        respx.get(f"{_BASE}/daily_sleep").mock(
            return_value=httpx.Response(
                200, json={"data": [{"day": "2027-05-02", "score": 55}]}
            )
        )
        respx.get(f"{_BASE}/daily_readiness").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [{"day": "2027-05-02", "score": 48, "temperature_deviation": 0.7}]
                },
            )
        )
        respx.get(f"{_BASE}/daily_activity").mock(
            return_value=httpx.Response(
                200, json={"data": [{"day": "2027-05-01", "steps": 78000}]}
            )
        )
        respx.get(f"{_BASE}/workout").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "day": "2027-05-01",
                            "activity": "walking",
                            "distance": 55000,
                            "calories": 3400,
                        }
                    ]
                },
            )
        )
        respx.get(f"{_BASE}/sleep").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "day": "2027-05-02",
                            "total_sleep_duration": 30600,
                            "average_hrv": 28,
                            "lowest_heart_rate": 52,
                        }
                    ]
                },
            )
        )

        result = await mcp.call_tool(
            "get_event_window",
            {"event_date": "2027-05-01", "days_before": 1, "days_after": 1},
        )
        payload = result.structured_content
        timeline = payload["timeline"]

        assert [row["offset_days"] for row in timeline] == [-1, 0, 1]
        event_day = next(row for row in timeline if row["is_event_day"])
        assert event_day["day"] == "2027-05-01"
        assert event_day["steps"] == 78000
        assert event_day["workouts"][0]["distance_m"] == 55000

        day_after = next(row for row in timeline if row["offset_days"] == 1)
        assert day_after["sleep_score"] == 55
        assert day_after["readiness_score"] == 48
        assert day_after["average_hrv"] == 28

    @respx.mock
    async def test_longest_session_wins_over_naps(self, wired_client):
        """With a nap plus the night, the nap must not replace the night."""
        for endpoint in ("daily_sleep", "daily_readiness", "daily_activity", "workout"):
            respx.get(f"{_BASE}/{endpoint}").mock(
                return_value=httpx.Response(200, json={"data": []})
            )
        respx.get(f"{_BASE}/sleep").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        # Nap first, so the ordering cannot carry the result.
                        {
                            "day": "2027-05-01",
                            "total_sleep_duration": 2400,
                            "average_hrv": 99,
                            "lowest_heart_rate": 70,
                        },
                        {
                            "day": "2027-05-01",
                            "total_sleep_duration": 28800,
                            "average_hrv": 31,
                            "lowest_heart_rate": 49,
                        },
                    ]
                },
            )
        )
        result = await mcp.call_tool(
            "get_event_window",
            {"event_date": "2027-05-01", "days_before": 0, "days_after": 0},
        )
        row = result.structured_content["timeline"][0]
        assert row["total_sleep_duration"] == 28800
        assert row["average_hrv"] == 31
        assert row["lowest_heart_rate"] == 49

    @respx.mock
    async def test_missing_days_stay_in_the_timeline(self, wired_client):
        """Gaps must stay visible, not silently disappear."""
        for endpoint in (
            "daily_sleep",
            "daily_readiness",
            "daily_activity",
            "workout",
            "sleep",
        ):
            respx.get(f"{_BASE}/{endpoint}").mock(
                return_value=httpx.Response(200, json={"data": []})
            )
        result = await mcp.call_tool(
            "get_event_window",
            {"event_date": "2027-05-01", "days_before": 2, "days_after": 2},
        )
        timeline = result.structured_content["timeline"]
        assert len(timeline) == 5
        assert all("sleep_score" not in row for row in timeline)


class TestHttpGuard:
    def test_http_transport_refuses_to_start_without_token(self, monkeypatch):
        """An open endpoint on the LAN would be a silent data leak."""
        monkeypatch.setenv("OURA_MCP_TRANSPORT", "http")
        monkeypatch.delenv("OURA_MCP_TOKEN", raising=False)
        with pytest.raises(SystemExit, match="OURA_MCP_TOKEN"):
            server_module.main()
