"""MCP server exposing Oura Ring data."""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import secrets
from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any, cast

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__
from .auth import OuraAuth, OuraAuthError, TokenStore
from .client import OuraApiError, OuraClient, OuraUnavailableError
from .const import DEFAULT_TOKEN_STORE

_LOGGER = logging.getLogger(__name__)

# MCP SDK 2.x renamed FastMCP to MCPServer. The decorator API is unchanged, but
# host and port moved from the old `settings` object into the calls to
# `streamable_http_app()` and `run_*_async()`.
mcp = MCPServer(name="oura", version=__version__)

_client: OuraClient | None = None
_client_lock = asyncio.Lock()


async def _async_client() -> OuraClient:
    """Lazily initialise the API client."""
    global _client
    async with _client_lock:
        if _client is None:
            client_id = os.environ.get("OURA_CLIENT_ID")
            client_secret = os.environ.get("OURA_CLIENT_SECRET")
            if not client_id or not client_secret:
                raise OuraAuthError("OURA_CLIENT_ID and OURA_CLIENT_SECRET must be set.")
            store = TokenStore(os.environ.get("OURA_TOKEN_STORE", DEFAULT_TOKEN_STORE))
            _client = OuraClient(
                OuraAuth(client_id=client_id, client_secret=client_secret, store=store)
            )
        return _client


def anticipated[ToolFn: Callable[..., Any]](fn: ToolFn) -> ToolFn:
    """Translate anticipated failures into ``ToolError``.

    The MCP SDK treats every exception other than ``ToolError`` as a crash and
    deliberately withholds its message from the client, which then sees only
    "Error executing tool get_sleep". That would hide exactly the messages worth
    reading: a malformed date, an application created in the wrong Oura portal,
    a refresh token already spent. Marking them as anticipated lets the text
    through.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except (ValueError, OuraAuthError, OuraApiError) as err:
            raise ToolError(str(err)) from err

    return cast("ToolFn", wrapper)


def _parse_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as err:
        raise ValueError(f"{label} must be in YYYY-MM-DD format, got {value!r}") from err


async def _fetch(endpoint: str, start: str, end: str) -> dict[str, Any]:
    """Shared path for all day-based tools."""
    client = await _async_client()
    start_date = _parse_date(start, "start_date")
    end_date = _parse_date(end, "end_date")
    try:
        records = await client.async_get_range(endpoint, start_date, end_date)
    except OuraUnavailableError as err:
        return {"endpoint": endpoint, "available": False, "reason": str(err), "data": []}
    return {
        "endpoint": endpoint,
        "available": True,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "count": len(records),
        "data": records,
    }


# --- Daily summaries --------------------------------------------------------


@mcp.tool()
@anticipated
async def get_sleep(start_date: str, end_date: str) -> dict[str, Any]:
    """Daily sleep scores with their contributing factors.

    The range includes both ends. Dates are YYYY-MM-DD.
    """
    return await _fetch("daily_sleep", start_date, end_date)


@mcp.tool()
@anticipated
async def get_sleep_detail(start_date: str, end_date: str) -> dict[str, Any]:
    """Per-session sleep phases, including naps.

    Contains deep, REM and light sleep duration, latency, efficiency, average
    HRV and lowest heart rate. This is the most detailed sleep source;
    `get_sleep` only reports the daily score derived from it.
    """
    return await _fetch("sleep", start_date, end_date)


@mcp.tool()
@anticipated
async def get_readiness(start_date: str, end_date: str) -> dict[str, Any]:
    """Daily readiness scores with temperature deviation and recovery index."""
    return await _fetch("daily_readiness", start_date, end_date)


@mcp.tool()
@anticipated
async def get_activity(start_date: str, end_date: str) -> dict[str, Any]:
    """Daily activity: steps, calories, goal progress and inactive time."""
    return await _fetch("daily_activity", start_date, end_date)


@mcp.tool()
@anticipated
async def get_workouts(start_date: str, end_date: str) -> dict[str, Any]:
    """Detected and manually entered workouts with duration, distance and intensity.

    The right source for hikes and long walks: Oura detects sustained walking
    as a workout automatically.
    """
    return await _fetch("workout", start_date, end_date)


@mcp.tool()
@anticipated
async def get_sessions(start_date: str, end_date: str) -> dict[str, Any]:
    """Guided and unguided sessions from the Oura app (breathing, meditation)."""
    return await _fetch("session", start_date, end_date)


@mcp.tool()
@anticipated
async def get_tags(start_date: str, end_date: str) -> dict[str, Any]:
    """User-entered tags in the enhanced format.

    Useful for marking events in the ring itself and finding them again later.
    """
    return await _fetch("enhanced_tag", start_date, end_date)


# --- Optional measurements --------------------------------------------------


@mcp.tool()
@anticipated
async def get_stress(start_date: str, end_date: str) -> dict[str, Any]:
    """Daily stress and recovery time. Requires an Oura membership."""
    return await _fetch("daily_stress", start_date, end_date)


@mcp.tool()
@anticipated
async def get_resilience(start_date: str, end_date: str) -> dict[str, Any]:
    """Resilience level and its contributors. Requires an Oura membership."""
    return await _fetch("daily_resilience", start_date, end_date)


@mcp.tool()
@anticipated
async def get_spo2(start_date: str, end_date: str) -> dict[str, Any]:
    """Nightly blood oxygen saturation and breathing disturbance index."""
    return await _fetch("daily_spo2", start_date, end_date)


@mcp.tool()
@anticipated
async def get_heart_health(start_date: str, end_date: str) -> dict[str, Any]:
    """Cardiovascular age and VO2 max.

    Both build on several weeks of measurements and are unavailable on a newly
    set up ring.
    """
    client = await _async_client()
    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date")

    async def _safe(endpoint: str) -> dict[str, Any]:
        try:
            return {
                "available": True,
                "data": await client.async_get_range(endpoint, start, end),
            }
        except OuraUnavailableError as err:
            return {"available": False, "reason": str(err), "data": []}

    cardio, vo2 = await asyncio.gather(_safe("daily_cardiovascular_age"), _safe("vO2_max"))
    return {"cardiovascular_age": cardio, "vo2_max": vo2}


@mcp.tool()
@anticipated
async def get_heartrate(start_datetime: str, end_datetime: str) -> dict[str, Any]:
    """Heart rate time series between two timestamps.

    ISO 8601 format, for example 2024-05-01T06:00:00. Ranges longer than 30
    days are split into windows automatically. Be aware this can return a very
    large number of samples: for multi-week analysis prefer `get_sleep_detail`,
    which already aggregates HRV and resting heart rate per night.
    """
    client = await _async_client()
    try:
        start = datetime.fromisoformat(start_datetime)
        end = datetime.fromisoformat(end_datetime)
    except ValueError as err:
        raise ValueError("Timestamps must be ISO 8601, e.g. 2024-05-01T06:00:00") from err
    records = await client.async_get_heartrate(start, end)
    return {"count": len(records), "data": records}


# --- Account data and diagnostics -------------------------------------------


@mcp.tool()
@anticipated
async def get_personal_info() -> dict[str, Any]:
    """Account basics: age, height, weight, biological sex."""
    client = await _async_client()
    return await client.async_get_static("personal_info")


@mcp.tool()
@anticipated
async def get_ring_configuration() -> dict[str, Any]:
    """Ring hardware details: generation, size, colour, design."""
    client = await _async_client()
    return await client.async_get_static("ring_configuration")


@mcp.tool()
@anticipated
async def get_status() -> dict[str, Any]:
    """Check whether authorization is working, without reading health data.

    The first place to look when other calls fail.
    """
    store_path = os.environ.get("OURA_TOKEN_STORE", DEFAULT_TOKEN_STORE)
    status: dict[str, Any] = {
        "token_store": store_path,
        "token_store_exists": TokenStore(store_path).exists(),
        "client_id_set": bool(os.environ.get("OURA_CLIENT_ID")),
        "client_secret_set": bool(os.environ.get("OURA_CLIENT_SECRET")),
    }
    try:
        client = await _async_client()
        info = await client.async_get_static("personal_info")
    except (OuraAuthError, OuraApiError) as err:
        status["authorized"] = False
        status["error"] = str(err)
        return status
    status["authorized"] = True
    # A single innocuous field as proof of life, not health data.
    status["account_age"] = info.get("age")
    return status


# --- Analysis ---------------------------------------------------------------


@mcp.tool()
@anticipated
async def get_event_window(
    event_date: str, days_before: int = 7, days_after: int = 7
) -> dict[str, Any]:
    """Recovery timeline around a single event, in one call.

    Built for the question "how hard did this day hit me, and how long did I
    take to recover". Fetches sleep, readiness, activity and workouts for the
    window and aligns them day by day, so before-and-after is visible without
    further calls or manual joining.

    Args:
        event_date: Day of the event, YYYY-MM-DD.
        days_before: Days before the event. Defaults to 7.
        days_after: Days after the event. Defaults to 7.
    """
    if days_before < 0 or days_after < 0:
        raise ValueError("days_before and days_after must not be negative")
    if days_before + days_after > 180:
        raise ValueError(
            "Windows longer than 180 days are too large for this tool -- "
            "use the individual endpoints with an explicit range instead."
        )

    event = _parse_date(event_date, "event_date")
    start = event - timedelta(days=days_before)
    end = event + timedelta(days=days_after)
    client = await _async_client()

    async def _safe(endpoint: str) -> list[dict[str, Any]]:
        try:
            return await client.async_get_range(endpoint, start, end)
        except OuraUnavailableError:
            return []

    sleep, readiness, activity, workouts, detail = await asyncio.gather(
        _safe("daily_sleep"),
        _safe("daily_readiness"),
        _safe("daily_activity"),
        _safe("workout"),
        _safe("sleep"),
    )

    by_day = {
        (start + timedelta(days=offset)).isoformat(): {
            "day": (start + timedelta(days=offset)).isoformat(),
            "offset_days": offset - days_before,
            "is_event_day": offset == days_before,
        }
        for offset in range((end - start).days + 1)
    }

    for record in sleep:
        if (row := by_day.get(record.get("day", ""))) is not None:
            row["sleep_score"] = record.get("score")
    for record in readiness:
        if (row := by_day.get(record.get("day", ""))) is not None:
            row["readiness_score"] = record.get("score")
            row["temperature_deviation"] = record.get("temperature_deviation")
    for record in activity:
        if (row := by_day.get(record.get("day", ""))) is not None:
            row["steps"] = record.get("steps")
            row["active_calories"] = record.get("active_calories")
    for record in detail:
        if (row := by_day.get(record.get("day", ""))) is None:
            continue
        # With several sessions on one day (naps), the longest wins because it
        # represents the night.
        duration = record.get("total_sleep_duration") or 0
        if duration >= (row.get("total_sleep_duration") or 0):
            row["total_sleep_duration"] = duration
            row["average_hrv"] = record.get("average_hrv")
            row["lowest_heart_rate"] = record.get("lowest_heart_rate")
            row["average_breath"] = record.get("average_breath")

    for record in workouts:
        if (row := by_day.get(record.get("day", ""))) is None:
            continue
        row.setdefault("workouts", []).append(
            {
                "activity": record.get("activity"),
                "distance_m": record.get("distance"),
                "calories": record.get("calories"),
                "intensity": record.get("intensity"),
                "start": record.get("start_datetime"),
                "end": record.get("end_datetime"),
            }
        )

    timeline = [by_day[key] for key in sorted(by_day)]
    return {
        "event_date": event.isoformat(),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "note": (
            "offset_days is negative before the event, 0 on the day itself and "
            "positive afterwards. Missing fields mean Oura reported no data for "
            "that day."
        ),
        "timeline": timeline,
    }


def _transport_security() -> Any:
    """Configure the SDK's DNS rebinding protection.

    The SDK checks the ``Host`` header and allows only localhost by default.
    When the server is reached under any other name or address, the allowed
    hosts must be named explicitly -- otherwise it answers 421, which is easy
    to misread as a network problem.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    raw = os.environ.get("OURA_MCP_ALLOWED_HOSTS", "").strip()
    if not raw:
        # Unset means the SDK defaults stay in force (localhost only).
        return None
    hosts = [entry.strip() for entry in raw.split(",") if entry.strip()]
    return TransportSecuritySettings(
        allowed_hosts=hosts,
        allowed_origins=[f"http://{host}" for host in hosts]
        + [f"https://{host}" for host in hosts],
    )


def _build_http_app(shared_token: str) -> Any:
    """Wrap the ASGI app in a bearer token guard.

    The MCP endpoint itself has no authentication. Without this guard, anyone
    who can reach the port could read the health data behind it.
    """
    from starlette.responses import JSONResponse

    app = mcp.streamable_http_app(
        host=os.environ.get("OURA_MCP_HOST", "0.0.0.0"),
        transport_security=_transport_security(),
    )

    async def guard(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        # The health check passes without a token so container orchestration
        # does not need to know the secret.
        if scope.get("path") == "/healthz":
            await JSONResponse({"status": "ok"})(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        presented = headers.get("authorization", "").removeprefix("Bearer ").strip()
        if not secrets.compare_digest(presented, shared_token):
            await JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )(scope, receive, send)
            return
        await app(scope, receive, send)

    return guard


def main() -> None:
    """Start the server. Transport is selected via OURA_MCP_TRANSPORT."""
    logging.basicConfig(
        level=os.environ.get("OURA_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    transport = os.environ.get("OURA_MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        mcp.run(transport="stdio")
        return

    shared_token = os.environ.get("OURA_MCP_TOKEN", "")
    if not shared_token:
        raise SystemExit(
            "OURA_MCP_TOKEN must be set when serving over HTTP. Without it the "
            "health data would be readable by anyone who can reach the port."
        )

    import uvicorn

    uvicorn.run(
        _build_http_app(shared_token),
        host=os.environ.get("OURA_MCP_HOST", "0.0.0.0"),
        port=int(os.environ.get("OURA_MCP_PORT", "8000")),
        log_level=os.environ.get("OURA_LOG_LEVEL", "info").lower(),
    )


if __name__ == "__main__":
    main()
