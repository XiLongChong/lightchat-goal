#!/usr/bin/env python
"""Dev-only fake attached session callback server.

Standalone FastAPI app that pretends to be an external agent session so the
App ↔ PC Router ↔ attached-session callback chain can be exercised end-to-end
without Claude Code, Codex, or any real agent runtime.

Behaviour
---------
1. Listens on 127.0.0.1:9010 (configurable) for ``POST /api/app-session/messages``.
2. Echoes the user ``content`` back as a single agent reply, matching the
   attached-session callback v1 contract.
3. On startup, registers itself with the PC Router via
   ``POST /api/app-agent-sessions/register``.
4. On shutdown (Ctrl+C), best-effort calls the Router's offline endpoint.

Usage
-----
    python tools/app_session_echo_server.py \
        --router http://127.0.0.1:8899 \
        --host 127.0.0.1 \
        --port 9010 \
        --session-id echo-session-1 \
        --remote-agent-id echo-session-1 \
        --display-name "Echo Session"

Requires ``CHAT_API_KEY`` env var (same as PC Router). The token is never
printed.
"""

from __future__ import annotations

import argparse
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

DEFAULT_DESCRIPTION = "Dev attached session for App callback testing"


# ── Pure helpers (testable without a server) ───────────────────────


def build_echo_reply(
    content: str,
    client_message_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the v1 callback response echoing *content* back to the Router.

    Returns a dict matching the attached-session callback v1 contract::

        {"agentReplies": [{"content", "remoteId", "createdAt"}]}

    *now* may be supplied for deterministic tests; defaults to current UTC.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    return {
        "agentReplies": [
            {
                "content": f"Echo from attached session: {content}",
                "remoteId": f"echo-{client_message_id}",
                "createdAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        ]
    }


def build_registration_payload(
    *,
    session_id: str,
    remote_agent_id: str,
    display_name: str,
    description: str,
    callback_url: str,
) -> dict[str, Any]:
    """Build the ``POST /api/app-agent-sessions/register`` request body."""
    return {
        "sessionId": session_id,
        "remoteAgentId": remote_agent_id,
        "displayName": display_name,
        "description": description,
        "callbackUrl": callback_url,
    }


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


# ── Registration / offline (network) ───────────────────────────────


async def register_to_router(
    *,
    router_base_url: str,
    api_key: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """POST *payload* to the Router's register endpoint.

    Returns the Router's JSON response on success.
    Raises ``RuntimeError`` with a clear message on any failure.
    """
    url = f"{router_base_url.rstrip('/')}/api/app-agent-sessions/register"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload, headers=_auth_headers(api_key))
    except httpx.ConnectError as exc:
        raise RuntimeError(
            f"Cannot connect to PC Router at {url}. Is the Router running? ({exc})"
        ) from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"Network error registering to {url}: {exc}") from exc

    if resp.status_code < 200 or resp.status_code >= 300:
        body = resp.text[:500] if resp.text else ""
        raise RuntimeError(
            f"Router registration failed: HTTP {resp.status_code} from {url}"
            + (f" body={body}" if body else "")
        )
    try:
        return resp.json()
    except Exception as exc:
        raise RuntimeError(f"Router returned non-JSON response: {exc}") from exc


async def offline_from_router(
    *,
    router_base_url: str,
    api_key: str,
    session_id: str,
) -> None:
    """Best-effort POST to the Router's offline endpoint. Never raises."""
    url = (
        f"{router_base_url.rstrip('/')}/api/app-agent-sessions/{session_id}/offline"
    )
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, headers=_auth_headers(api_key))
        if resp.status_code < 200 or resp.status_code >= 300:
            print(
                f"[echo] offline returned HTTP {resp.status_code} (non-fatal)",
                file=sys.stderr,
            )
    except Exception as exc:
        print(f"[echo] offline failed (non-fatal): {exc}", file=sys.stderr)


# ── FastAPI app ────────────────────────────────────────────────────


def create_app(
    *,
    router_base_url: str,
    api_key: str,
    session_id: str,
    remote_agent_id: str,
    display_name: str,
    description: str,
    callback_url: str,
) -> FastAPI:
    """Build the echo FastAPI app with lifespan-managed registration/offline."""

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        # Register on startup — if this fails the server will not start.
        payload = build_registration_payload(
            session_id=session_id,
            remote_agent_id=remote_agent_id,
            display_name=display_name,
            description=description,
            callback_url=callback_url,
        )
        try:
            await register_to_router(
                router_base_url=router_base_url,
                api_key=api_key,
                payload=payload,
            )
        except RuntimeError as exc:
            print(f"[echo] registration failed: {exc}", file=sys.stderr)
            raise
        print(
            f"[echo] registered sessionId={session_id} "
            f"remoteAgentId={remote_agent_id} -> Router {router_base_url}"
        )

        yield

        # Offline on shutdown (best-effort, never raises).
        await offline_from_router(
            router_base_url=router_base_url,
            api_key=api_key,
            session_id=session_id,
        )
        print(f"[echo] offline sent for sessionId={session_id}")

    app = FastAPI(title="App Session Echo Server (dev)", lifespan=_lifespan)

    @app.post("/api/app-session/messages")
    async def echo_messages(request: Request) -> JSONResponse:
        """Echo endpoint matching the attached-session callback v1 contract."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400, content={"error": "invalid JSON body"}
            )
        if not isinstance(body, dict):
            return JSONResponse(
                status_code=400, content={"error": "body must be a JSON object"}
            )

        content = str(body.get("content", "")).strip()
        client_message_id = str(body.get("clientMessageId", "")).strip()
        if not content:
            return JSONResponse(
                status_code=400, content={"error": "content is required"}
            )

        reply = build_echo_reply(content, client_message_id or "unknown")
        print(
            f"[echo] received clientMessageId={client_message_id} "
            f"content={content!r} -> replying"
        )
        return JSONResponse(reply)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "sessionId": session_id}

    return app


# ── CLI ────────────────────────────────────────────────────────────


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dev-only fake attached session callback server.",
    )
    parser.add_argument(
        "--router",
        default="http://127.0.0.1:8899",
        help="PC Router base URL (default: http://127.0.0.1:8899)",
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="Listen host (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port", type=int, default=9010, help="Listen port (default: 9010)"
    )
    parser.add_argument(
        "--session-id",
        default="echo-session-1",
        help="Session id to register (default: echo-session-1)",
    )
    parser.add_argument(
        "--remote-agent-id",
        default="echo-session-1",
        help="remoteAgentId to claim (default: echo-session-1)",
    )
    parser.add_argument(
        "--display-name",
        default="Echo Session",
        help="Display name shown in App (default: Echo Session)",
    )
    parser.add_argument(
        "--description",
        default=DEFAULT_DESCRIPTION,
        help="Description shown in App",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    api_key = os.getenv("CHAT_API_KEY", "")
    if not api_key:
        print(
            "[echo] CHAT_API_KEY env var is not set; cannot authenticate to Router.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    callback_url = f"http://{args.host}:{args.port}"
    app = create_app(
        router_base_url=args.router,
        api_key=api_key,
        session_id=args.session_id,
        remote_agent_id=args.remote_agent_id,
        display_name=args.display_name,
        description=args.description,
        callback_url=callback_url,
    )

    print(
        f"[echo] starting echo server on {args.host}:{args.port} "
        f"(router={args.router} sessionId={args.session_id})"
    )
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    except KeyboardInterrupt:
        # lifespan shutdown handles offline; just exit cleanly.
        pass


if __name__ == "__main__":
    main()
