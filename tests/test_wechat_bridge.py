import pytest
from bridge.wechat_poller import WeChatBridge


def test_bounded_reply_delay_clamps_model_delay():
    bridge = WeChatBridge(token="token", max_reply_delay=3.0)

    assert bridge._bounded_reply_delay(12) == 3.0
    assert bridge._bounded_reply_delay(-2) == 0.0
    assert bridge._bounded_reply_delay("bad") == 0.0


def test_bounded_reply_delay_can_disable_upper_bound():
    bridge = WeChatBridge(token="token", max_reply_delay=0)

    assert bridge._bounded_reply_delay(12) == 12.0


# ================================================================
# HTTP error logging tests
# ================================================================


@pytest.mark.asyncio
async def test_flush_buffer_logs_http_status_and_body_on_error(monkeypatch):
    """When /chat returns non-2xx, the error log includes status_code and response body."""
    import logging
    import httpx
    from unittest.mock import AsyncMock, MagicMock

    bridge = WeChatBridge(token="token", chat_api_key="secret")

    # Build a fake HTTPStatusError with status_code and response.text
    fake_response = MagicMock()
    fake_response.status_code = 503
    fake_response.text = '{"detail": "chat_auth_key_not_configured"}'

    # Make the async-with's __aenter__ return a mock whose .post raises
    async def fake_post(*args, **kwargs):
        raise httpx.HTTPStatusError(
            "Server error",
            request=MagicMock(),
            response=fake_response,
        )

    class _FakeClientCtx:
        """Simulates `async with httpx.AsyncClient(...) as client:`."""
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            client = MagicMock()
            client.post = fake_post
            return client

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClientCtx)

    # Mock _send_reply to avoid actual iLink calls
    bridge._send_reply = AsyncMock()

    # Pre-populate buffer so _flush_buffer actually makes the HTTP call
    bridge._buffers["wxid_test"] = [{"text": "hello", "context_token": ""}]

    # Capture logs
    import io
    log_stream = io.StringIO()
    handler = logging.StreamHandler(log_stream)
    handler.setLevel(logging.ERROR)
    logger = logging.getLogger("goal.bridge")
    logger.addHandler(handler)
    try:
        await bridge._flush_buffer("wxid_test")
    finally:
        logger.removeHandler(handler)

    log_output = log_stream.getvalue()
    assert "503" in log_output, f"Expected status_code in log, got: {log_output}"
    assert "chat_auth_key_not_configured" in log_output, (
        f"Expected response body in log, got: {log_output}"
    )


def test_wechat_bridge_stores_chat_api_key():
    """WeChatBridge accepts and stores chat_api_key."""
    bridge = WeChatBridge(token="token", chat_api_key="my-secret-key")
    assert bridge.chat_api_key == "my-secret-key"


def test_wechat_bridge_default_chat_api_key_empty():
    """WeChatBridge defaults to empty chat_api_key when not provided."""
    bridge = WeChatBridge(token="token")
    assert bridge.chat_api_key == ""
