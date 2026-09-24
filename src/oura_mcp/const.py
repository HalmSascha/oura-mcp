"""Constants for the Oura MCP server.

Values here are verified against the Oura Cloud documentation and against a
working reference client, not taken from memory. Where the two disagree, the
comment says which one won and why.
"""

from __future__ import annotations

from typing import Final

# --- OAuth2 -----------------------------------------------------------------

OAUTH2_AUTHORIZE: Final = "https://cloud.ouraring.com/oauth/authorize"

# Oura runs two token endpoints. The published documentation names only the
# legacy one, but Oura's infrastructure today serves `moi` for applications
# from both developer portals.
#
# The order matters and is not cosmetic: authorization codes are single-use
# (RFC 6749 section 4.1.2). Trying the legacy endpoint first and being rejected
# consumes the code, so the fallback then receives a dead code and fails too --
# even though `moi` would have accepted the original.
OAUTH2_TOKEN: Final = "https://moi.ouraring.com/oauth/v2/ext/oauth-token"
OAUTH2_TOKEN_LEGACY: Final = "https://api.ouraring.com/oauth/token"

# HTTP statuses where falling back to the legacy endpoint makes sense. A 5xx is
# an outage, not a hint that we picked the wrong endpoint.
TOKEN_FALLBACK_STATUSES: Final = frozenset({400, 401, 403, 404})

OAUTH2_SCOPES: Final = (
    "email",
    "personal",
    "daily",
    "heartrate",
    "workout",
    "session",
    "tag",
    "spo2",
    "ring_configuration",
    "stress",
    "heart_health",
)

# Redirect URI for the one-time CLI authorization. Must be registered at
# https://developer.ouraring.com/applications
LOCAL_REDIRECT_HOST: Final = "localhost"
LOCAL_REDIRECT_PORT: Final = 8765
LOCAL_REDIRECT_PATH: Final = "/callback"
LOCAL_REDIRECT_URI: Final = (
    f"http://{LOCAL_REDIRECT_HOST}:{LOCAL_REDIRECT_PORT}{LOCAL_REDIRECT_PATH}"
)

# --- REST API ---------------------------------------------------------------

API_BASE_URL: Final = "https://api.ouraring.com/v2/usercollection"

#: Endpoints taking day-based parameters (``start_date`` / ``end_date``).
#: Oura's ``end_date`` is **exclusive** -- see `daterange_params`.
DATE_ENDPOINTS: Final = (
    "daily_sleep",
    "sleep",
    "daily_readiness",
    "daily_activity",
    "daily_stress",
    "daily_resilience",
    "daily_spo2",
    "daily_cardiovascular_age",
    "vO2_max",
    "sleep_time",
    "workout",
    "session",
    "tag",
    "enhanced_tag",
    "rest_mode_period",
)

#: Endpoints taking timestamp parameters (``start_datetime`` / ``end_datetime``).
DATETIME_ENDPOINTS: Final = ("heartrate",)

#: Endpoints without a time range filter.
STATIC_ENDPOINTS: Final = ("personal_info", "ring_configuration")

#: Endpoints requiring an active Oura membership or newer ring hardware.
#: A 401/403 on these is not an error -- it is "not available for this account",
#: which is a perfectly normal state.
OPTIONAL_ENDPOINTS: Final = frozenset(
    {
        "daily_resilience",
        "daily_spo2",
        "daily_cardiovascular_age",
        "vO2_max",
        "daily_stress",
        "sleep_time",
    }
)

#: Oura limits the heart rate endpoint to 30-day windows per request.
HEARTRATE_MAX_WINDOW_DAYS: Final = 30

# --- Behaviour --------------------------------------------------------------

DEFAULT_TIMEOUT_SECONDS: Final = 30.0
#: Upper bound for a `Retry-After` induced wait, so a rate limit cannot stall
#: the MCP call indefinitely.
MAX_RETRY_AFTER_SECONDS: Final = 30.0
TOKEN_REFRESH_MARGIN_SECONDS: Final = 300

DEFAULT_TOKEN_STORE: Final = "/data/tokens.json"
