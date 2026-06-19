#!/usr/bin/env python
"""Dev-only Claude Code mock attached-session callback server.

Standalone FastAPI app that pretends to be a Claude Code coding session.
It exercises the full App ↔ PC Router ↔ attached-session callback chain
without a real Claude Code process.

Behaviour
---------
1. Listens on 127.0.0.1:9011 for ``POST /api/app-session/messages``.
2. On startup registers with the PC Router as an attached session.
3. Replies with mock code-assistant responses based on content keywords.
4. On shutdown (Ctrl+C), best-effort calls the Router's offline endpoint.

This is a **mock** — it does NOT connect to Claude Code, execute shell
commands, or modify files.

Usage
-----
    python tools/claude_code_mock_adapter.py \
        --router http://127.0.0.1:8899 \
        --host 127.0.0.1 \
        --port 9011 \
        --session-id claude-code-mock-1 \
        --remote-agent-id claude-code-mock-1 \
        --workspace D:\\projects\\goal

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

SOURCE_LABEL = "claude-code-mock"


# ── Pure helpers (testable without a server) ───────────────────────


def _reply_template(
    client_message_id: str,
    content: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a single agent reply entry with the v1 contract fields."""
    if now is None:
        now = datetime.now(timezone.utc)
    return {
        "content": content,
        "remoteId": f"claude-mock-{client_message_id}",
        "createdAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def build_claude_mock_reply(
    content: str,
    client_message_id: str,
    *,
    workspace_name: str = "goal",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a v1 callback response with a mock code-assistant reply.

    Response logic (keyword-based):

    - "status" / "状态" / "进度" → mock status report
    - "test" / "测试" → test-intent reply
    - "fix" / "修" / "bug" → fix-intent reply
    - default → "Claude Code Mock received: <content>"

    *now* may be supplied for deterministic tests; defaults to current UTC.
    """
    lower = content.lower()

    if any(kw in lower for kw in ("status", "状态", "进度")):
        reply_text = (
            f"Current mock status: session active, workspace={workspace_name}, "
            f"no real tasks running. This is a mock adapter — "
            "no Claude Code process is connected."
        )
    elif any(kw in lower for kw in ("test", "测试")):
        reply_text = (
            "我会先检查测试范围，然后运行相关测试。"
            "当前是 mock adapter，没有真实执行命令。"
        )
    elif any(kw in lower for kw in ("fix", "修", "bug")):
        reply_text = (
            "我会定位问题、最小化修改并验证。"
            "当前是 mock adapter，没有修改文件。"
        )
    else:
        reply_text = f"Claude Code Mock received: {content}"

    return {
        "agentReplies": [
            _reply_template(client_message_id, reply_text, now=now),
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
        "sourceLabel": SOURCE_LABEL,
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
            resp = await client.post(
                url, json=payload, headers=_auth_headers(api_key),
            )
    except httpx.ConnectError as exc:
        raise RuntimeError(
            f"Cannot connect to PC Router at {url}. "
            f"Is the Router running? ({exc})"
        ) from exc
    except httpx.RequestError as exc:
        raise RuntimeError(
            f"Network error registering to {url}: {exc}"
        ) from exc

    if resp.status_code < 200 or resp.status_code >= 300:
        body = resp.text[:500] if resp.text else ""
        raise RuntimeError(
            f"Router registration failed: HTTP {resp.status_code} from {url}"
            + (f" body={body}" if body else "")
        )
    try:
        return resp.json()
    except Exception as exc:
        raise RuntimeError(
            f"Router returned non-JSON response: {exc}"
        ) from exc


async def offline_from_router(
    *,
    router_base_url: str,
    api_key: str,
    session_id: str,
) -> None:
    """Best-effort POST to the Router's offline endpoint. Never raises."""
    url = (
        f"{router_base_url.rstrip('/')}"
        f"/api/app-agent-sessions/{session_id}/offline"
    )
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, headers=_auth_headers(api_key))
        if resp.status_code < 200 or resp.status_code >= 300:
            print(
                f"[claude-mock] offline returned HTTP {resp.status_code} "
                f"(non-fatal)",
                file=sys.stderr,
            )
    except Exception as exc:
        print(
            f"[claude-mock] offline failed (non-fatal): {exc}",
            file=sys.stderr,
        )


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
    workspace_name: str = "goal",
) -> FastAPI:
    """Build the Claude Code mock FastAPI app."""

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
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
            print(
                f"[claude-mock] registration failed: {exc}",
                file=sys.stderr,
            )
            raise
        print(
            f"[claude-mock] registered sessionId={session_id} "
            f"remoteAgentId={remote_agent_id} -> Router {router_base_url}"
        )

        yield

        await offline_from_router(
            router_base_url=router_base_url,
            api_key=api_key,
            session_id=session_id,
        )
        print(f"[claude-mock] offline sent for sessionId={session_id}")

    app = FastAPI(
        title="Claude Code Mock Adapter (dev)",
        lifespan=_lifespan,
    )

    @app.post("/api/app-session/messages")
    async def handle_messages(request: Request) -> JSONResponse:
        """Callback endpoint matching the v1 contract."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400, content={"error": "invalid JSON body"},
            )
        if not isinstance(body, dict):
            return JSONResponse(
                status_code=400,
                content={"error": "body must be a JSON object"},
            )

        content = str(body.get("content", "")).strip()
        client_message_id = str(body.get("clientMessageId", "")).strip()
        if not content:
            return JSONResponse(
                status_code=400, content={"error": "content is required"},
            )

        reply = build_claude_mock_reply(
            content,
            client_message_id or "unknown",
            workspace_name=workspace_name,
        )
        print(
            f"[claude-mock] received clientMessageId={client_message_id} "
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
        description="Dev-only Claude Code mock attached-session server.",
    )
    parser.add_argument(
        "--router",
        default="http://127.0.0.1:8899",
        help="PC Router base URL (default: http://127.0.0.1:8899)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Listen host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=9011,
        help="Listen port (default: 9011)",
    )
    parser.add_argument(
        "--session-id",
        default="claude-code-mock-1",
        help="Session id to register (default: claude-code-mock-1)",
    )
    parser.add_argument(
        "--remote-agent-id",
        default="claude-code-mock-1",
        help="remoteAgentId to claim (default: claude-code-mock-1)",
    )
    parser.add_argument(
        "--workspace",
        default=os.getcwd(),
        help="Workspace path for display name / description",
    )
    parser.add_argument(
        "--display-name",
        default="",
        help="Override display name (default: derived from workspace)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    api_key = os.getenv("CHAT_API_KEY", "")
    if not api_key:
        print(
            "[claude-mock] CHAT_API_KEY env var is not set; "
            "cannot authenticate to Router.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    workspace_path = os.path.abspath(args.workspace or os.getcwd())
    workspace_name = os.path.basename(workspace_path) or "work"

    display_name = args.display_name or f"Claude Code Mock - {workspace_name}"
    description = f"Mock coding session in {workspace_path}"

    callback_url = f"http://{args.host}:{args.port}"
    app = create_app(
        router_base_url=args.router,
        api_key=api_key,
        session_id=args.session_id,
        remote_agent_id=args.remote_agent_id,
        display_name=display_name,
        description=description,
        callback_url=callback_url,
        workspace_name=workspace_name,
    )

    print(
        f"[claude-mock] starting mock adapter on {args.host}:{args.port} "
        f"(router={args.router} sessionId={args.session_id} "
        f"workspace={workspace_path})"
    )
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()