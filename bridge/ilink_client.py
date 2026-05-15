"""iLink Bot API client — ported from emoRebot TypeScript.

Base URL: https://ilinkai.weixin.qq.com
7 HTTP endpoints for WeChat bot operations.
"""

import json
import os
import time
import uuid
import base64
import hashlib
from typing import Optional

import httpx

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
BOT_TYPE = "3"
LONG_POLL_TIMEOUT_MS = 35_000


def _random_wechat_uin() -> str:
    raw = os.urandom(4)
    num = int.from_bytes(raw, "big")
    return base64.b64encode(str(num).encode()).decode()


def _build_base_info() -> dict:
    return {"channel_version": "0.1.0"}


def _random_hex(n: int = 4) -> str:
    return os.urandom(n).hex()


async def _api_fetch(
    endpoint: str,
    token: str = "",
    body: str = "",
    timeout_ms: int = 10_000,
    label: str = "",
    method: str = "POST",
    base_url: str = "",
    extra_headers: dict | None = None,
) -> str:
    base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    url = f"{base}/{endpoint}"
    is_get = method == "GET"

    headers = {
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": _random_wechat_uin(),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if not is_get:
        headers["Content-Type"] = "application/json"
        if body:
            headers["Content-Length"] = str(len(body.encode("utf-8")))
    if extra_headers:
        headers.update(extra_headers)

    async with httpx.AsyncClient(timeout=timeout_ms / 1000) as client:
        if is_get:
            resp = await client.get(url, headers=headers)
        else:
            resp = await client.post(url, headers=headers, content=body)
        resp.raise_for_status()
        return resp.text


# ─── QR Login ────────────────────────────────────────────────


async def get_qr_code(base_url: str = "") -> dict:
    text = await _api_fetch(
        endpoint=f"ilink/bot/get_bot_qrcode?bot_type={BOT_TYPE}",
        timeout_ms=10_000,
        label="getQRCode",
        method="GET",
        base_url=base_url,
    )
    return json.loads(text)


async def poll_qr_status(qrcode: str, base_url: str = "") -> dict:
    try:
        text = await _api_fetch(
            endpoint=f"ilink/bot/get_qrcode_status?qrcode={qrcode}",
            timeout_ms=LONG_POLL_TIMEOUT_MS,
            label="pollQRStatus",
            method="GET",
            base_url=base_url,
            extra_headers={"iLink-App-ClientVersion": "1"},
        )
        return json.loads(text)
    except httpx.TimeoutException:
        return {"status": "wait"}
    except Exception:
        return {"status": "wait"}


# ─── Messages ────────────────────────────────────────────────


async def get_updates(
    token: str,
    buf: str,
    base_url: str = "",
    timeout_ms: int = LONG_POLL_TIMEOUT_MS,
) -> dict:
    body = json.dumps({
        "get_updates_buf": buf,
        "base_info": _build_base_info(),
    })
    try:
        text = await _api_fetch(
            endpoint="ilink/bot/getupdates",
            body=body,
            token=token,
            timeout_ms=timeout_ms,
            label="getUpdates",
            base_url=base_url,
        )
        return json.loads(text)
    except (httpx.TimeoutException, Exception):
        return {"ret": 0, "msgs": [], "get_updates_buf": buf}


async def send_message(
    token: str,
    to: str,
    text: str,
    context_token: str,
    base_url: str = "",
) -> str:
    client_id = f"goal-{_random_hex(4)}"
    msg = {
        "from_user_id": "",
        "to_user_id": to,
        "client_id": client_id,
        "message_type": 2,
        "message_state": 2,
        "item_list": [{"type": 1, "text_item": {"text": text}}],
        "context_token": context_token,
    }
    body = json.dumps({"msg": msg, "base_info": _build_base_info()})
    await _api_fetch(
        endpoint="ilink/bot/sendmessage",
        body=body,
        token=token,
        timeout_ms=10_000,
        label="sendMessage",
        base_url=base_url,
    )
    return client_id


# ─── Config ──────────────────────────────────────────────────


async def get_config(
    token: str,
    user_id: str,
    context_token: str = "",
    base_url: str = "",
) -> dict:
    payload: dict = {"user_id": user_id, "base_info": _build_base_info()}
    if context_token:
        payload["context_token"] = context_token
    body = json.dumps(payload)
    text = await _api_fetch(
        endpoint="ilink/bot/getconfig",
        body=body,
        token=token,
        timeout_ms=10_000,
        label="getConfig",
        base_url=base_url,
    )
    return json.loads(text)
