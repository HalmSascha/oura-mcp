<div align="center">
  <img src="docs/logo.svg" alt="oura-mcp" width="120" height="120">
  <h1>oura-mcp</h1>
  <p><strong>MCP server for Oura Ring data — OAuth2 only, self-hosted.</strong></p>
  <p>
    <a href="https://github.com/HalmSascha/oura-mcp/actions/workflows/ci.yml"><img src="https://github.com/HalmSascha/oura-mcp/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
    <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT"></a>
    <img src="https://img.shields.io/badge/python-3.12%20%7C%203.13-blue.svg" alt="Python 3.12 | 3.13">
  </p>
  <p><a href="README.de.md">Deutsche Fassung</a></p>
</div>

---

> **Forge mirror.** The canonical repository is on [GitHub](https://github.com/HalmSascha/oura-mcp); an identical mirror is kept on [Codeberg](https://codeberg.org/saschahalm/oura-mcp).
> Issues and pull requests are handled on GitHub and are disabled on the mirror.

Sleep, readiness, activity, workouts, HRV and resting heart rate from your Oura
Ring, exposed to any MCP client. Runs over stdio for local clients or over
streamable HTTP behind a shared secret for networked deployment.

Not affiliated with, endorsed by, or sponsored by Ōura Health Oy.

## Why another one

Oura **deprecated Personal Access Tokens in December 2025**. New ones can no
longer be created, which quietly broke most existing Oura MCP servers for fresh
installations — they authenticate with a PAT. This one is OAuth2 only.

It also handles three things that are easy to get wrong, each of which cost real
debugging time:

**Oura runs two token endpoints.** The published documentation names only
`api.ouraring.com/oauth/token`, but Oura's infrastructure actually serves
`moi.ouraring.com/oauth/v2/ext/oauth-token` today. The order matters:
authorization codes are single-use ([RFC 6749 §4.1.2](https://datatracker.ietf.org/doc/html/rfc6749#section-4.1.2)),
so a rejected attempt against the wrong endpoint burns the code and makes any
fallback useless. This server tries `moi` first and falls back to legacy.

**Refresh tokens are single-use too.** Each refresh returns a new one and
invalidates the old. Losing the response means the authorization is dead and
must be re-established by hand. Tokens are therefore written atomically before
being handed out, and concurrent refreshes are serialised behind a lock.

**Date ranges are inclusive here.** Oura's own `end_date` is exclusive, so a
naive single-day request returns nothing. That trap is closed once, in one
place, rather than left for every caller to remember.

## Tools

| Tool | Purpose |
|---|---|
| `get_sleep` | Daily sleep scores with contributors |
| `get_sleep_detail` | Per-session sleep phases, HRV, lowest heart rate |
| `get_readiness` | Readiness scores, temperature deviation, recovery index |
| `get_activity` | Steps, calories, targets, inactivity |
| `get_workouts` | Detected and manual workouts with distance and intensity |
| `get_sessions` | Guided and unguided app sessions |
| `get_tags` | User-entered tags (enhanced format) |
| `get_stress` | Daily stress and recovery time |
| `get_resilience` | Resilience level and contributors |
| `get_spo2` | Nightly blood oxygen and breathing disturbance index |
| `get_heart_health` | Cardiovascular age and VO2 max |
| `get_heartrate` | Heart rate time series, auto-chunked into 30-day windows |
| `get_personal_info` | Account basics |
| `get_ring_configuration` | Ring generation, size, colour |
| `get_status` | Authorization diagnostics without touching health data |
| `get_event_window` | **Recovery timeline around a single event, in one call** |

### `get_event_window`

This is the tool worth having. Give it an event date and a window; it returns a
day-by-day timeline with sleep score, readiness, HRV, resting heart rate, steps
and workouts aligned on an `offset_days` axis — negative before the event, zero
on the day, positive after.

```
Day          Offset  Sleep  Ready   HRV   RHR    Steps
2026-09-17       -2     78     84    28    53    2,697
2026-09-18       -1     68     71    26    56    2,439
2026-09-19       +0     58     73    23    52    1,942   <- event
2026-09-20       +1     57     78    30    52    1,782
2026-09-21       +2     83     85    25    52    2,110
```

Answering "how hard did that hit me, and how long did I take to recover" is one
call instead of six plus manual joining. Built for correlating hard days — an
ultramarathon, a long hike, a flight, a bad night — against what the ring
recorded afterwards.

Endpoints requiring an Oura membership or newer hardware degrade gracefully:
they answer `{"available": false, "reason": "..."}` instead of failing the call.

## Setup

### 1. Register an Oura application

Go to **https://developer.ouraring.com/applications** — this is the correct
portal, and picking the wrong one is the single most common setup failure. Add
this redirect URI:

```
http://localhost:8765/callback
```

Note the Client ID and Client Secret.

> If you also use the [Home Assistant Oura integration](https://github.com/louispires/Oura-Home-Assistant-Integration),
> one application serves both — just add `https://my.home-assistant.io/redirect/oauth`
> as a second redirect URI.

### 2. Authorize once

The OAuth flow needs a browser, so it cannot run headless in a container:

```bash
export OURA_CLIENT_ID=...
export OURA_CLIENT_SECRET=...
uvx --from oura-mcp oura-mcp --token-store ./tokens.json auth
```

This writes `tokens.json` with mode `0600`.

> **Keep only one copy in active use.** Oura invalidates the refresh token on
> every use, so two copies of the same file will kill each other the first time
> either one refreshes. After copying it to a server, delete the local copy.

### 3a. Run locally over stdio

```json
{
  "mcpServers": {
    "oura": {
      "command": "uvx",
      "args": ["--from", "oura-mcp", "oura-mcp", "--token-store", "/path/to/tokens.json", "serve"],
      "env": {
        "OURA_CLIENT_ID": "...",
        "OURA_CLIENT_SECRET": "..."
      }
    }
  }
}
```

### 3b. Run over HTTP in Docker

Multi-arch images (`amd64` and `arm64`) are published to GHCR:

```bash
docker pull ghcr.io/halmsascha/oura-mcp:latest
```

Copy `docker-compose.example.yaml` to `docker-compose.yaml`, fill in your
values, then:

```bash
mkdir -p data && cp /path/to/tokens.json data/ && chmod 600 data/tokens.json
docker compose up -d
```

Clients authenticate with `Authorization: Bearer $OURA_MCP_TOKEN`:

```bash
claude mcp add --transport http oura http://your-host:8000/mcp \
  --header "Authorization: Bearer $OURA_MCP_TOKEN"
```

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `OURA_CLIENT_ID` | — | required |
| `OURA_CLIENT_SECRET` | — | required |
| `OURA_TOKEN_STORE` | `/data/tokens.json` | token file location |
| `OURA_MCP_TRANSPORT` | `stdio` | `stdio` or `http` |
| `OURA_MCP_TOKEN` | — | shared secret, **required** for `http` |
| `OURA_MCP_HOST` | `0.0.0.0` | bind address |
| `OURA_MCP_PORT` | `8000` | bind port |
| `OURA_MCP_ALLOWED_HOSTS` | — | permitted `Host` headers, comma separated |
| `OURA_LOG_LEVEL` | `INFO` | logging verbosity |

`OURA_MCP_ALLOWED_HOSTS` is required when the server is reached under anything
other than localhost. The MCP SDK validates the `Host` header against DNS
rebinding and otherwise answers `421`, which is easy to misread as a network
problem. Set it to the host and port your clients actually use, for example
`oura.example.com,192.168.1.10:8000`.

`/healthz` answers without a token so container health checks do not need the
secret. Everything else returns `401` without a valid bearer token.

## Security

- `tokens.json` holds access and refresh tokens in clear text. It is in
  `.gitignore` and written `0600`. Do not commit it.
- Rejected token requests log only the endpoint host, grant type and HTTP
  status. Response bodies are never logged, because they can contain tokens.
  A test asserts this.
- **The HTTP transport has no per-user authentication.** The shared secret is
  the only gate. This is a single-user design: do not expose the port to the
  internet without something in front of it.

## Troubleshooting

Start with `get_status`. It reports whether credentials are set, whether the
token store exists, and whether authorization actually works — without reading
any health data.

| Symptom | Likely cause |
|---|---|
| `404` after approving on Oura's site | Redirect URI not registered, or registered in the wrong portal |
| `401` during token exchange | Wrong portal, mismatched redirect URI, or a regenerated client secret |
| Token request rejected on refresh | The refresh token was already used. Re-run `auth --force` |
| `421` on every HTTP request | `OURA_MCP_ALLOWED_HOSTS` does not include the host your client uses |
| Some sensors always `available: false` | Those endpoints need an Oura membership or newer ring hardware |
| Empty result for a single day | Should not happen — please open an issue, the inclusive range handling is meant to prevent exactly this |

## Development

```bash
uv sync --all-extras
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

41 tests cover the inclusive/exclusive date boundary, pagination, heart rate
windowing, rate-limit retry and its cap, optional-endpoint degradation, token
endpoint ordering, atomic persistence, concurrent refresh, error messages
reaching the client, and a regression guard asserting no secret ever reaches the
log.

Contributions welcome. Please run the checks above before opening a PR.

## AI-Assisted Development

This project was developed with AI assistance (Claude / Claude Code), under the
direction and review of [Sascha Halm](https://github.com/HalmSascha).
Architecture decisions, the choice of OAuth-only authentication, and the
verification of Oura's undocumented token endpoint were reviewed by a human
before being committed.

## License

MIT — see [LICENSE](LICENSE).
