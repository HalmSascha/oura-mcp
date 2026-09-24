"""Command line: one-time OAuth authorization and server startup.

Authorization needs a browser, so it cannot run headless inside a container.
Run it locally, then copy the resulting ``tokens.json`` to the target host and
mount it there.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlparse

from .auth import (
    OuraAuthError,
    TokenStore,
    async_exchange_code,
    build_authorize_url,
    new_state,
)
from .const import (
    DEFAULT_TOKEN_STORE,
    LOCAL_REDIRECT_HOST,
    LOCAL_REDIRECT_PATH,
    LOCAL_REDIRECT_PORT,
    LOCAL_REDIRECT_URI,
)

_SUCCESS_PAGE = b"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Oura connected</title></head>
<body style="font-family:system-ui;padding:3rem;max-width:34rem">
<h1>Connected</h1>
<p>Tokens saved. You can close this window.</p>
</body></html>
"""

_ERROR_PAGE = b"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Authorization failed</title></head>
<body style="font-family:system-ui;padding:3rem;max-width:34rem">
<h1>Authorization failed</h1>
<p>Details are in the terminal.</p>
</body></html>
"""


class _CallbackHandler(BaseHTTPRequestHandler):
    """Accepts exactly one redirect back from Oura."""

    # Class level, because BaseHTTPRequestHandler is instantiated per request
    # and the result has to outlive the handler.
    result: ClassVar[dict[str, str]] = {}
    expected_state: ClassVar[str] = ""

    # Name is dictated by BaseHTTPRequestHandler, not ours to choose.
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != LOCAL_REDIRECT_PATH:
            self.send_error(404)
            return
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        # Validate state before accepting the code.
        if not secrets.compare_digest(params.get("state", ""), type(self).expected_state):
            type(self).result = {"error": "state_mismatch"}
            self._respond(400, _ERROR_PAGE)
            return
        if "error" in params:
            type(self).result = {"error": params["error"]}
            self._respond(400, _ERROR_PAGE)
            return
        if "code" not in params:
            type(self).result = {"error": "no_code"}
            self._respond(400, _ERROR_PAGE)
            return

        type(self).result = {"code": params["code"]}
        self._respond(200, _SUCCESS_PAGE)

    def _respond(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        """Suppress access logging: the URL contains the authorization code."""


def _require_credentials() -> tuple[str, str]:
    client_id = os.environ.get("OURA_CLIENT_ID")
    client_secret = os.environ.get("OURA_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise SystemExit(
            "OURA_CLIENT_ID and OURA_CLIENT_SECRET must be set.\n"
            "Both are at https://developer.ouraring.com/applications"
        )
    return client_id, client_secret


def command_auth(args: argparse.Namespace) -> int:
    """Run the authorization code flow in a browser."""
    client_id, client_secret = _require_credentials()
    store = TokenStore(args.token_store)

    if store.exists() and not args.force:
        print(
            f"A token store already exists at {store.path}.\n"
            "Use --force to overwrite it. Note that this renders the existing "
            "refresh token unusable.",
            file=sys.stderr,
        )
        return 1

    state = new_state()
    _CallbackHandler.expected_state = state
    _CallbackHandler.result = {}
    url = build_authorize_url(client_id, LOCAL_REDIRECT_URI, state)

    try:
        server = HTTPServer((LOCAL_REDIRECT_HOST, LOCAL_REDIRECT_PORT), _CallbackHandler)
    except OSError as err:
        raise SystemExit(
            f"Port {LOCAL_REDIRECT_PORT} is in use ({err}). "
            "Stop the other process and try again."
        ) from err

    print("Opening your browser. If it does not open, visit this URL manually:\n")
    print(f"  {url}\n")
    print(
        f"The redirect URI {LOCAL_REDIRECT_URI} must be registered in the Oura "
        "developer portal, otherwise Oura aborts with a 404."
    )
    webbrowser.open(url)

    with server:
        server.handle_request()

    result = _CallbackHandler.result
    if "code" not in result:
        reason = result.get("error", "unknown")
        if reason == "state_mismatch":
            print(
                "State did not match, request discarded. This suggests a "
                "forged redirect. Please try again.",
                file=sys.stderr,
            )
        else:
            print(f"Authorization failed: {reason}", file=sys.stderr)
        return 1

    try:
        tokens = asyncio.run(
            async_exchange_code(client_id, client_secret, result["code"], LOCAL_REDIRECT_URI)
        )
    except OuraAuthError as err:
        print(f"Token exchange failed: {err}", file=sys.stderr)
        return 1

    store.save(tokens)
    print(f"\nTokens saved to {store.path} (mode 0600)")
    print(f"Granted scopes: {tokens.scope or 'not reported by Oura'}")
    print(
        "\nTo run on another host, copy this file there and mount it as "
        "/data/tokens.json. Keep only one copy in active use: Oura invalidates "
        "the refresh token on every use, so two copies will kill each other."
    )
    return 0


def command_serve(args: argparse.Namespace) -> int:
    """Start the MCP server."""
    from .server import main as serve_main

    os.environ.setdefault("OURA_TOKEN_STORE", args.token_store)
    if args.transport:
        os.environ["OURA_MCP_TRANSPORT"] = args.transport
    serve_main()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="oura-mcp", description="MCP server for Oura Ring data"
    )
    parser.add_argument(
        "--token-store",
        default=os.environ.get("OURA_TOKEN_STORE", DEFAULT_TOKEN_STORE),
        help="Path to the token file (default: %(default)s)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth", help="One-time OAuth authorization in a browser")
    auth.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing token store",
    )
    auth.set_defaults(func=command_auth)

    serve = sub.add_parser("serve", help="Start the MCP server")
    serve.add_argument(
        "--transport",
        choices=["stdio", "http"],
        help="Transport (default: stdio, or OURA_MCP_TRANSPORT)",
    )
    serve.set_defaults(func=command_serve)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
