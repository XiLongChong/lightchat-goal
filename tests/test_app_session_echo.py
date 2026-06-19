"""Lightweight unit tests for the dev echo server's pure helpers.

These tests exercise the payload/response construction helpers only — no real
HTTP server is started, so there is no port usage and no flakiness.
"""

from __future__ import annotations

from datetime import datetime, timezone

from tools.app_session_echo_server import (
    build_echo_reply,
    build_registration_payload,
)


def test_build_echo_reply_structure_and_content():
    """build_echo_reply returns a single reply echoing the content."""
    now = datetime(2026, 6, 19, 12, 0, 2, tzinfo=timezone.utc)
    reply = build_echo_reply("hello", "cm-001", now=now)

    assert reply["agentReplies"]
    assert len(reply["agentReplies"]) == 1
    entry = reply["agentReplies"][0]
    assert entry["content"] == "Echo from attached session: hello"
    assert entry["remoteId"] == "echo-cm-001"
    assert entry["createdAt"] == "2026-06-19T12:00:02Z"


def test_build_echo_reply_default_now_is_utc_z():
    """Without an explicit now, createdAt is an ISO Z string."""
    reply = build_echo_reply("hi", "cm-002")
    entry = reply["agentReplies"][0]
    assert entry["createdAt"].endswith("Z")
    # Rough ISO 8601 shape: YYYY-MM-DDTHH:MM:SSZ
    assert len(entry["createdAt"]) == 20


def test_build_echo_reply_preserves_arbitrary_content():
    """Content with special characters is echoed verbatim."""
    reply = build_echo_reply("你好，世界！\nnewline", "cm-3")
    assert reply["agentReplies"][0]["content"] == (
        "Echo from attached session: 你好，世界！\nnewline"
    )


def test_build_echo_reply_remote_id_uses_client_message_id():
    """remoteId is echo-<clientMessageId>."""
    reply = build_echo_reply("x", "cm-abc-123")
    assert reply["agentReplies"][0]["remoteId"] == "echo-cm-abc-123"


def test_build_registration_payload_has_all_fields():
    """Registration payload matches the Router's register endpoint contract."""
    payload = build_registration_payload(
        session_id="echo-session-1",
        remote_agent_id="echo-session-1",
        display_name="Echo Session",
        description="Dev attached session for App callback testing",
        callback_url="http://127.0.0.1:9010",
    )
    assert payload == {
        "sessionId": "echo-session-1",
        "remoteAgentId": "echo-session-1",
        "displayName": "Echo Session",
        "description": "Dev attached session for App callback testing",
        "callbackUrl": "http://127.0.0.1:9010",
    }


def test_build_registration_payload_uses_provided_values():
    """Custom values are passed through unchanged."""
    payload = build_registration_payload(
        session_id="custom-session",
        remote_agent_id="custom-agent",
        display_name="Custom Display",
        description="custom desc",
        callback_url="http://10.0.0.5:9999",
    )
    assert payload["sessionId"] == "custom-session"
    assert payload["remoteAgentId"] == "custom-agent"
    assert payload["displayName"] == "Custom Display"
    assert payload["description"] == "custom desc"
    assert payload["callbackUrl"] == "http://10.0.0.5:9999"
