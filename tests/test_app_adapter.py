"""Tests for the AsPocket app adapter endpoints (/api/*)."""

from __future__ import annotations

import json
import os
import time

import pytest
from fastapi import HTTPException


# ──────────────────────────────────────────────────────────────────
# Unit tests for helper functions (no FastAPI TestClient needed)
# ──────────────────────────────────────────────────────────────────


def test_iso_from_ts():
    from main import _iso_from_ts

    result = _iso_from_ts(1718539200.0)  # 2024-06-16T12:00:00 UTC
    assert "2024-06-16" in result
    assert result.endswith("Z")


def test_iso_from_ts_zero_epoch():
    from main import _iso_from_ts

    result = _iso_from_ts(0.0)
    assert result.endswith("Z")


def test_iso_from_ts_negative_graceful():
    from main import _iso_from_ts

    # Should not raise — returns current UTC time instead
    result = _iso_from_ts(-1e20)
    assert result.endswith("Z")


# ──────────────────────────────────────────────────────────────────
# Auth tests (same pattern as test_chat_security.py)
# ──────────────────────────────────────────────────────────────────


def test_api_profile_requires_auth(monkeypatch):
    """GET /api/profile returns 401 without valid token."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    class Request:
        headers = {}

    with pytest.raises(HTTPException) as exc:
        from main import _api_config, _require_chat_auth

        api_cfg = _api_config()
        _require_chat_auth(Request(), api_cfg)
    assert exc.value.status_code == 401


def test_api_profile_with_valid_token(monkeypatch):
    """GET /api/profile passes auth with correct Bearer token."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    class Request:
        headers = {"authorization": "Bearer secret"}

    from main import _api_config, _require_chat_auth

    api_cfg = _api_config()
    _require_chat_auth(Request(), api_cfg)  # should not raise


# ──────────────────────────────────────────────────────────────────
# MemoryStore app_messages tests
# ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_insert_and_find_app_message():
    from memory.store import MemoryStore

    store = MemoryStore(db_path=":memory:")
    try:
        remote_id = "test-rm-001"
        await store.insert_app_message(
            remote_id=remote_id,
            client_message_id="cm-001",
            role="user",
            content="Hello",
            source_turn_id="turn-1",
        )
        found = await store.find_app_message_by_client_message_id("cm-001")
        assert found is not None
        assert found["id"] == remote_id
        assert found["content"] == "Hello"
        assert found["role"] == "user"
        assert found["remote_agent_id"] == "default"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_find_app_message_not_found():
    from memory.store import MemoryStore

    store = MemoryStore(db_path=":memory:")
    try:
        found = await store.find_app_message_by_client_message_id("nonexistent")
        assert found is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_find_app_agent_replies_by_turn():
    from memory.store import MemoryStore

    store = MemoryStore(db_path=":memory:")
    try:
        await store.insert_app_message(
            remote_id="agent-1",
            client_message_id=None,
            role="agent",
            content="Reply 1",
            source_turn_id="turn-1",
            sequence=1,
        )
        await store.insert_app_message(
            remote_id="agent-2",
            client_message_id=None,
            role="agent",
            content="Reply 2",
            source_turn_id="turn-1",
            sequence=2,
        )
        replies = await store.find_app_agent_replies_by_turn_id("turn-1")
        assert len(replies) == 2
        assert replies[0]["content"] == "Reply 1"
        assert replies[1]["content"] == "Reply 2"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_get_app_messages_empty():
    from memory.store import MemoryStore

    store = MemoryStore(db_path=":memory:")
    try:
        messages, cursor, has_more = await store.get_app_messages_after_cursor(
            cursor=None, limit=50,
        )
        assert messages == []
        assert cursor is None
        assert has_more is False
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_get_app_messages_pagination():
    from memory.store import MemoryStore

    store = MemoryStore(db_path=":memory:")
    try:
        for i in range(5):
            await store.insert_app_message(
                remote_id=f"msg-{i:03d}",
                client_message_id=f"cm-{i:03d}" if i % 2 == 0 else None,
                role="user" if i % 2 == 0 else "agent",
                content=f"Message {i}",
                source_turn_id="turn-1",
                sequence=i,
            )

        # First page: 3 items
        messages, cursor, has_more = await store.get_app_messages_after_cursor(
            cursor=None, limit=3,
        )
        assert len(messages) == 3
        assert has_more is True
        assert cursor == messages[-1]["id"]

        # Second page: remaining 2 items
        messages2, cursor2, has_more2 = await store.get_app_messages_after_cursor(
            cursor=cursor, limit=3,
        )
        assert len(messages2) == 2
        assert has_more2 is False
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_app_message_idempotency_conflict():
    """clientMessageId unique constraint prevents duplicate user messages."""
    from memory.store import MemoryStore

    store = MemoryStore(db_path=":memory:")
    try:
        await store.insert_app_message(
            remote_id="rm-1",
            client_message_id="cm-same",
            role="user",
            content="Hello",
            source_turn_id="turn-1",
        )
        # Second insert with same client_message_id should fail
        with pytest.raises(Exception):
            await store.insert_app_message(
                remote_id="rm-2",
                client_message_id="cm-same",
                role="user",
                content="Different",
                source_turn_id="turn-2",
            )
    finally:
        await store.close()


# ──────────────────────────────────────────────────────────────────
# App messages remote_agent_id isolation tests
# ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_insert_default_remote_agent_id():
    """Insert without remote_agent_id defaults to 'default'."""
    from memory.store import MemoryStore

    store = MemoryStore(db_path=":memory:")
    try:
        await store.insert_app_message(
            remote_id="rm-1",
            client_message_id="cm-1",
            role="user",
            content="hello",
            source_turn_id="turn-1",
        )
        found = await store.find_app_message_by_client_message_id(
            "cm-1", remote_agent_id="default",
        )
        assert found is not None
        assert found["remote_agent_id"] == "default"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_same_client_msg_id_different_agents():
    """Same clientMessageId can be inserted in different remote_agent_ids."""
    from memory.store import MemoryStore

    store = MemoryStore(db_path=":memory:")
    try:
        await store.insert_app_message(
            remote_id="rm-default",
            client_message_id="cm-same",
            role="user",
            content="hello from default",
            source_turn_id="turn-1",
            remote_agent_id="default",
        )
        await store.insert_app_message(
            remote_id="rm-coder",
            client_message_id="cm-same",
            role="user",
            content="hello from coder",
            source_turn_id="turn-2",
            remote_agent_id="coder",
        )

        found_default = await store.find_app_message_by_client_message_id(
            "cm-same", remote_agent_id="default",
        )
        found_coder = await store.find_app_message_by_client_message_id(
            "cm-same", remote_agent_id="coder",
        )
        assert found_default is not None
        assert found_coder is not None
        assert found_default["content"] == "hello from default"
        assert found_coder["content"] == "hello from coder"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_same_client_msg_id_same_agent_conflict():
    """Same clientMessageId in same remote_agent_id still conflicts."""
    from memory.store import MemoryStore

    store = MemoryStore(db_path=":memory:")
    try:
        await store.insert_app_message(
            remote_id="rm-1",
            client_message_id="cm-dup",
            role="user",
            content="first",
            source_turn_id="turn-1",
            remote_agent_id="default",
        )
        with pytest.raises(Exception):
            await store.insert_app_message(
                remote_id="rm-2",
                client_message_id="cm-dup",
                role="user",
                content="second",
                source_turn_id="turn-2",
                remote_agent_id="default",
            )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_find_app_message_does_not_cross_agent():
    """find_app_message_by_client_message_id scoped to remote_agent_id."""
    from memory.store import MemoryStore

    store = MemoryStore(db_path=":memory:")
    try:
        await store.insert_app_message(
            remote_id="rm-coder",
            client_message_id="cm-cross",
            role="user",
            content="coder msg",
            source_turn_id="turn-1",
            remote_agent_id="coder",
        )
        # Looking in "default" should NOT find coder's message
        found = await store.find_app_message_by_client_message_id(
            "cm-cross", remote_agent_id="default",
        )
        assert found is None
        # Looking in "coder" should find it
        found2 = await store.find_app_message_by_client_message_id(
            "cm-cross", remote_agent_id="coder",
        )
        assert found2 is not None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_find_agent_replies_does_not_cross_agent():
    """find_app_agent_replies_by_turn_id scoped to remote_agent_id."""
    from memory.store import MemoryStore

    store = MemoryStore(db_path=":memory:")
    try:
        # Insert replies under "default"
        await store.insert_app_message(
            remote_id="agent-def",
            client_message_id=None,
            role="agent",
            content="default reply",
            source_turn_id="turn-same",
            sequence=1,
            remote_agent_id="default",
        )
        # Insert replies under "coder" with same turn_id
        await store.insert_app_message(
            remote_id="agent-cod",
            client_message_id=None,
            role="agent",
            content="coder reply",
            source_turn_id="turn-same",
            sequence=1,
            remote_agent_id="coder",
        )

        replies_default = await store.find_app_agent_replies_by_turn_id(
            "turn-same", remote_agent_id="default",
        )
        replies_coder = await store.find_app_agent_replies_by_turn_id(
            "turn-same", remote_agent_id="coder",
        )
        assert len(replies_default) == 1
        assert replies_default[0]["content"] == "default reply"
        assert len(replies_coder) == 1
        assert replies_coder[0]["content"] == "coder reply"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_get_messages_scoped_to_remote_agent():
    """get_app_messages_after_cursor only returns messages for given agent."""
    from memory.store import MemoryStore

    store = MemoryStore(db_path=":memory:")
    try:
        await store.insert_app_message(
            remote_id="rm-def",
            client_message_id="cm-def",
            role="user",
            content="default msg",
            source_turn_id="turn-1",
            remote_agent_id="default",
        )
        await store.insert_app_message(
            remote_id="rm-coder",
            client_message_id="cm-coder",
            role="user",
            content="coder msg",
            source_turn_id="turn-2",
            remote_agent_id="coder",
        )

        msgs_default, _, _ = await store.get_app_messages_after_cursor(
            remote_agent_id="default",
        )
        msgs_coder, _, _ = await store.get_app_messages_after_cursor(
            remote_agent_id="coder",
        )

        assert len(msgs_default) == 1
        assert msgs_default[0]["content"] == "default msg"
        assert len(msgs_coder) == 1
        assert msgs_coder[0]["content"] == "coder msg"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_get_messages_cursor_from_different_agent_treated_as_none():
    """Cursor from a different remote_agent_id is treated as None."""
    from memory.store import MemoryStore

    store = MemoryStore(db_path=":memory:")
    try:
        await store.insert_app_message(
            remote_id="rm-def-1",
            client_message_id="cm-1",
            role="user",
            content="default first",
            source_turn_id="turn-1",
            remote_agent_id="default",
        )
        await store.insert_app_message(
            remote_id="rm-def-2",
            client_message_id="cm-2",
            role="user",
            content="default second",
            source_turn_id="turn-2",
            remote_agent_id="default",
        )
        await store.insert_app_message(
            remote_id="rm-coder",
            client_message_id="cm-3",
            role="user",
            content="coder only",
            source_turn_id="turn-3",
            remote_agent_id="coder",
        )

        # Use coder's cursor to query default — should return from start of default
        msgs, _, _ = await store.get_app_messages_after_cursor(
            cursor="rm-coder", remote_agent_id="default",
        )
        assert len(msgs) == 2  # both default messages
        contents = {m["content"] for m in msgs}
        assert contents == {"default first", "default second"}
    finally:
        await store.close()


# ──────────────────────────────────────────────────────────────────
# Endpoint isolation tests (remote_agent_id scoping via real endpoints)
# ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_messages_coder_does_not_return_default_history(monkeypatch):
    """GET /api/messages?remoteAgentId=coder returns empty when coder has no history."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import cfg

    cfg["app"] = {
        "contact_invitations": [
            {"id": "pc-agent-default", "remoteAgentId": "default"},
            {"id": "pc-agent-coder", "remoteAgentId": "coder"},
        ]
    }

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

    from main import api_get_messages, memory_store

    # Use unique id to avoid collision with previous test runs
    cid = f"cm-def-pre-{_uuid.uuid4().hex[:8]}"
    await memory_store.insert_app_message(
        remote_id=f"rm-def-pre-{_uuid.uuid4().hex[:8]}",
        client_message_id=cid,
        role="user",
        content="default history",
        source_turn_id="turn-def",
        remote_agent_id="default",
    )

    result = await api_get_messages(FakeRequest(), remoteAgentId="coder")
    assert "messages" in result
    assert isinstance(result["messages"], list)
    # coder should see its own (empty) history, not default's
    for m in result["messages"]:
        assert m.get("content") != "default history"


@pytest.mark.asyncio
async def test_post_messages_coder_idempotency_isolated(monkeypatch):
    """POST with same clientMessageId in default and coder doesn't collide."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import cfg

    cfg["app"] = {
        "contact_invitations": [
            {"id": "pc-agent-default", "remoteAgentId": "default"},
            {"id": "pc-agent-coder", "remoteAgentId": "coder"},
        ]
    }

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        def __init__(self, client_msg_id, content, remote_agent_id=None):
            self._client_msg_id = client_msg_id
            self._content = content
            self._remote = remote_agent_id

        async def json(self):
            body = {
                "clientMessageId": self._client_msg_id,
                "content": self._content,
            }
            if self._remote is not None:
                body["remoteAgentId"] = self._remote
            return body

    from main import api_send_message

    cid = f"cm-isolated-{_uuid.uuid4().hex[:8]}"

    # First: insert under "default"
    result1 = await api_send_message(
        FakeRequest(cid, "hello default", "default")
    )
    assert "userMessage" in result1

    # Second: same clientMessageId under "coder" — should NOT collide
    result2 = await api_send_message(
        FakeRequest(cid, "hello coder", "coder")
    )
    assert "userMessage" in result2


# ──────────────────────────────────────────────────────────────────
# /api/contact-invitations tests
# ──────────────────────────────────────────────────────────────────


class _FakeAppRequest:
    """Minimal stand-in for fastapi.Request used by app adapter endpoints.

    Only the attributes the endpoints touch are implemented: ``headers``
    (dict-like, supports ``.get``) and ``base_url`` (passed through ``str()``).
    """

    def __init__(self, headers=None, base_url="http://testserver/"):
        self.headers = headers if headers is not None else {}
        self.base_url = base_url


def test_contact_invitations_requires_auth(monkeypatch):
    """GET /api/contact-invitations returns 401 without valid token."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    with pytest.raises(HTTPException) as exc:
        from main import _api_config, _require_chat_auth

        api_cfg = _api_config()
        _require_chat_auth(_FakeAppRequest(headers={}), api_cfg)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_contact_invitations_returns_envelope(monkeypatch):
    """Authenticated GET /api/contact-invitations returns invitations envelope."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")
    monkeypatch.setenv("COMPUTERNAME", "MY-PC")

    from main import api_contact_invitations, cfg

    # Ensure no leftover multi-invitation config from other tests
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    result = await api_contact_invitations(
        _FakeAppRequest(
            headers={"authorization": "Bearer secret"},
            base_url="http://10.0.2.2:8000/",
        )
    )

    assert "invitations" in result
    assert isinstance(result["invitations"], list)
    assert len(result["invitations"]) == 1


@pytest.mark.asyncio
async def test_contact_invitations_has_required_fields(monkeypatch):
    """Invitation envelope contains all required fields with correct values."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")
    monkeypatch.setenv("COMPUTERNAME", "MY-PC")

    from main import api_contact_invitations

    result = await api_contact_invitations(
        _FakeAppRequest(
            headers={"authorization": "Bearer secret"},
            base_url="http://10.0.2.2:8000/",
        )
    )
    inv = result["invitations"][0]

    required_fields = {
        "id", "remoteAgentId", "displayName", "avatarUri", "description",
        "agentType", "sourceLabel", "endpoint", "createdAt", "expiresAt",
    }
    assert required_fields.issubset(inv.keys())
    assert inv["remoteAgentId"] == "default"
    assert inv["avatarUri"] is None
    assert inv["expiresAt"] is None
    assert inv["description"] == "Personal assistant on this PC"
    assert inv["agentType"] == "pc-agent"
    assert inv["sourceLabel"] == "MY-PC"
    assert isinstance(inv["displayName"], str) and inv["displayName"]
    assert inv["createdAt"].endswith("Z")


@pytest.mark.asyncio
async def test_contact_invitations_endpoint_strips_trailing_slash(monkeypatch):
    """endpoint equals request base url without trailing slash."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import api_contact_invitations

    result = await api_contact_invitations(
        _FakeAppRequest(
            headers={"authorization": "Bearer secret"},
            base_url="http://10.0.2.2:8000/",
        )
    )
    assert result["invitations"][0]["endpoint"] == "http://10.0.2.2:8000"


@pytest.mark.asyncio
async def test_contact_invitations_id_is_stable(monkeypatch):
    """Invitation id is the stable pc-agent-default for app dedupe."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import api_contact_invitations

    result = await api_contact_invitations(
        _FakeAppRequest(
            headers={"authorization": "Bearer secret"},
            base_url="http://testserver/",
        )
    )
    assert result["invitations"][0]["id"] == "pc-agent-default"


# ──────────────────────────────────────────────────────────────────
# build_app_contact_invitation helper tests
# ──────────────────────────────────────────────────────────────────


def test_build_contact_invitation_stable_id():
    """Helper always returns pc-agent-default id for app dedupe."""
    from main import build_app_contact_invitation

    inv = build_app_contact_invitation(
        _FakeAppRequest(base_url="http://10.0.2.2:8000/"),
    )
    assert inv["id"] == "pc-agent-default"
    assert inv["remoteAgentId"] == "default"


def test_build_contact_invitation_uses_request_base_url():
    """endpoint equals request base_url with trailing slash stripped."""
    from main import build_app_contact_invitation

    inv = build_app_contact_invitation(
        _FakeAppRequest(base_url="http://10.0.2.2:8000/"),
    )
    assert inv["endpoint"] == "http://10.0.2.2:8000"


def test_build_contact_invitation_uses_request_base_url_no_trailing_slash():
    """endpoint matches base_url when no trailing slash to strip."""
    from main import build_app_contact_invitation

    inv = build_app_contact_invitation(
        _FakeAppRequest(base_url="http://192.168.1.100:8899"),
    )
    assert inv["endpoint"] == "http://192.168.1.100:8899"


def test_build_contact_invitation_uses_cfg_agent_name(monkeypatch):
    """displayName comes from config agent.name when set."""
    from main import build_app_contact_invitation, cfg

    monkeypatch.setitem(cfg.setdefault("agent", {}), "name", "My Test Agent")

    inv = build_app_contact_invitation(
        _FakeAppRequest(base_url="http://testserver/"),
    )
    assert inv["displayName"] == "My Test Agent"


def test_build_contact_invitation_fallback_display_name(monkeypatch):
    """displayName falls back to 'PC Agent' when cfg agent.name is missing."""
    from main import build_app_contact_invitation, cfg

    cfg.setdefault("agent", {}).pop("name", None)

    inv = build_app_contact_invitation(
        _FakeAppRequest(base_url="http://testserver/"),
    )
    assert inv["displayName"] == "PC Agent"


def test_build_contact_invitation_fallback_source_label(monkeypatch):
    """sourceLabel falls back to 'PC Agent' when COMPUTERNAME env is unset."""
    monkeypatch.delenv("COMPUTERNAME", raising=False)

    from main import build_app_contact_invitation

    inv = build_app_contact_invitation(
        _FakeAppRequest(base_url="http://testserver/"),
    )
    assert inv["sourceLabel"] == "PC Agent"


def test_build_contact_invitation_uses_computername_env(monkeypatch):
    """sourceLabel uses COMPUTERNAME env var when set."""
    monkeypatch.setenv("COMPUTERNAME", "DESKTOP-XYZ")

    from main import build_app_contact_invitation

    inv = build_app_contact_invitation(
        _FakeAppRequest(base_url="http://testserver/"),
    )
    assert inv["sourceLabel"] == "DESKTOP-XYZ"


def test_build_contact_invitation_created_at_ends_with_z():
    """createdAt is ISO 8601 UTC with Z suffix."""
    from main import build_app_contact_invitation

    inv = build_app_contact_invitation(
        _FakeAppRequest(base_url="http://testserver/"),
    )
    assert inv["createdAt"].endswith("Z")
    # Basic ISO format check: "2026-06-19T..."
    assert "T" in inv["createdAt"]


def test_build_contact_invitation_fixed_fields():
    """avatarUri, expiresAt, description, agentType are fixed."""
    from main import build_app_contact_invitation

    inv = build_app_contact_invitation(
        _FakeAppRequest(base_url="http://testserver/"),
    )
    assert inv["avatarUri"] is None
    assert inv["expiresAt"] is None
    assert inv["description"] == "Personal assistant on this PC"
    assert inv["agentType"] == "pc-agent"
    assert inv["remoteAgentId"] == "default"


# ──────────────────────────────────────────────────────────────────
# build_app_contact_invitations (config-driven multi-invitation) tests
# ──────────────────────────────────────────────────────────────────


def test_build_invitations_no_config_returns_default():
    """Without app.contact_invitations config, returns single default."""
    from main import build_app_contact_invitations, cfg

    cfg.setdefault("app", {}).pop("contact_invitations", None)

    invs = build_app_contact_invitations(
        _FakeAppRequest(base_url="http://10.0.2.2:8000/"),
    )
    assert len(invs) == 1
    assert invs[0]["id"] == "pc-agent-default"
    assert invs[0]["remoteAgentId"] == "default"


def test_build_invitations_with_config_returns_multiple(monkeypatch):
    """With app.contact_invitations config, returns one per entry."""
    from main import build_app_contact_invitations, cfg

    cfg["app"] = {
        "contact_invitations": [
            {
                "id": "pc-agent-default",
                "remoteAgentId": "default",
                "displayName": "Yu",
                "description": "Connected PC agent",
            },
            {
                "id": "pc-agent-coder",
                "remoteAgentId": "coder",
                "displayName": "Coder",
                "description": "Coding assistant on this PC",
            },
        ]
    }

    invs = build_app_contact_invitations(
        _FakeAppRequest(base_url="http://10.0.2.2:8000/"),
    )
    assert len(invs) == 2

    assert invs[0]["id"] == "pc-agent-default"
    assert invs[0]["remoteAgentId"] == "default"
    assert invs[0]["displayName"] == "Yu"
    assert invs[0]["description"] == "Connected PC agent"

    assert invs[1]["id"] == "pc-agent-coder"
    assert invs[1]["remoteAgentId"] == "coder"
    assert invs[1]["displayName"] == "Coder"
    assert invs[1]["description"] == "Coding assistant on this PC"


def test_build_invitations_endpoint_always_from_request(monkeypatch):
    """endpoint is always from request.base_url, never from config."""
    from main import build_app_contact_invitations, cfg

    cfg["app"] = {
        "contact_invitations": [
            {
                "id": "pc-agent-default",
                "displayName": "Test",
            },
        ]
    }

    invs = build_app_contact_invitations(
        _FakeAppRequest(base_url="http://192.168.1.100:8899/"),
    )
    assert invs[0]["endpoint"] == "http://192.168.1.100:8899"


def test_build_invitations_source_label_from_env(monkeypatch):
    """sourceLabel from COMPUTERNAME env, shared across all invitations."""
    monkeypatch.setenv("COMPUTERNAME", "DESKTOP-XYZ")

    from main import build_app_contact_invitations, cfg

    cfg["app"] = {
        "contact_invitations": [
            {"id": "a", "displayName": "A"},
            {"id": "b", "displayName": "B"},
        ]
    }

    invs = build_app_contact_invitations(
        _FakeAppRequest(base_url="http://testserver/"),
    )
    assert invs[0]["sourceLabel"] == "DESKTOP-XYZ"
    assert invs[1]["sourceLabel"] == "DESKTOP-XYZ"


def test_build_invitations_created_at_ends_with_z(monkeypatch):
    """createdAt is ISO 8601 UTC Z for every invitation."""
    from main import build_app_contact_invitations, cfg

    cfg["app"] = {
        "contact_invitations": [
            {"id": "a", "displayName": "A"},
            {"id": "b", "displayName": "B"},
        ]
    }

    invs = build_app_contact_invitations(
        _FakeAppRequest(base_url="http://testserver/"),
    )
    for inv in invs:
        assert inv["createdAt"].endswith("Z")
        assert "T" in inv["createdAt"]


def test_build_invitations_fallback_display_name(monkeypatch):
    """displayName falls back to cfg agent.name when entry has no displayName."""
    from main import build_app_contact_invitations, cfg

    cfg["agent"] = {"name": "Fallback Agent"}
    cfg["app"] = {
        "contact_invitations": [
            {"id": "pc-agent-default"},
        ]
    }

    invs = build_app_contact_invitations(
        _FakeAppRequest(base_url="http://testserver/"),
    )
    assert invs[0]["displayName"] == "Fallback Agent"


def test_build_invitations_fallback_description(monkeypatch):
    """description falls back to default when entry has no description."""
    from main import build_app_contact_invitations, cfg

    cfg["app"] = {
        "contact_invitations": [
            {"id": "pc-agent-default"},
        ]
    }

    invs = build_app_contact_invitations(
        _FakeAppRequest(base_url="http://testserver/"),
    )
    assert invs[0]["description"] == "Personal assistant on this PC"


def test_build_invitations_fallback_remote_agent_id(monkeypatch):
    """remoteAgentId falls back to 'default' when entry has no remoteAgentId."""
    from main import build_app_contact_invitations, cfg

    cfg["app"] = {
        "contact_invitations": [
            {"id": "pc-agent-custom"},
        ]
    }

    invs = build_app_contact_invitations(
        _FakeAppRequest(base_url="http://testserver/"),
    )
    assert invs[0]["remoteAgentId"] == "default"


def test_build_invitations_fallback_id(monkeypatch):
    """id falls back to 'pc-agent-default' when entry has no id."""
    from main import build_app_contact_invitations, cfg

    cfg["app"] = {
        "contact_invitations": [
            {"displayName": "NoId"},
        ]
    }

    invs = build_app_contact_invitations(
        _FakeAppRequest(base_url="http://testserver/"),
    )
    assert invs[0]["id"] == "pc-agent-default"


def test_build_invitations_fixed_fields_per_entry(monkeypatch):
    """avatarUri, expiresAt, agentType are fixed for every invitation."""
    from main import build_app_contact_invitations, cfg

    cfg["app"] = {
        "contact_invitations": [
            {"id": "a", "displayName": "A"},
            {"id": "b", "displayName": "B"},
        ]
    }

    invs = build_app_contact_invitations(
        _FakeAppRequest(base_url="http://testserver/"),
    )
    for inv in invs:
        assert inv["avatarUri"] is None
        assert inv["expiresAt"] is None
        assert inv["agentType"] == "pc-agent"


# ──────────────────────────────────────────────────────────────────
# normalize_remote_agent_id tests
# ──────────────────────────────────────────────────────────────────


def test_normalize_none_returns_default():
    from main import normalize_remote_agent_id

    assert normalize_remote_agent_id(None) == "default"


def test_normalize_empty_returns_default():
    from main import normalize_remote_agent_id

    assert normalize_remote_agent_id("") == "default"


def test_normalize_whitespace_returns_default():
    from main import normalize_remote_agent_id

    assert normalize_remote_agent_id("   ") == "default"


def test_normalize_preserves_valid():
    from main import normalize_remote_agent_id

    assert normalize_remote_agent_id("coder") == "coder"


def test_normalize_strips_whitespace():
    from main import normalize_remote_agent_id

    assert normalize_remote_agent_id("  coder  ") == "coder"


# ──────────────────────────────────────────────────────────────────
# get_known_app_remote_agent_ids tests
# ──────────────────────────────────────────────────────────────────


def test_known_ids_always_includes_default():
    from main import get_known_app_remote_agent_ids, cfg

    cfg.setdefault("app", {}).pop("contact_invitations", None)
    ids = get_known_app_remote_agent_ids()
    assert "default" in ids


def test_known_ids_includes_configured_agents(monkeypatch):
    from main import get_known_app_remote_agent_ids, cfg

    cfg["app"] = {
        "contact_invitations": [
            {"id": "a", "remoteAgentId": "default"},
            {"id": "b", "remoteAgentId": "coder"},
        ]
    }
    ids = get_known_app_remote_agent_ids()
    assert ids == {"default", "coder"}


def test_known_ids_excludes_empty_remote_agent_id(monkeypatch):
    from main import get_known_app_remote_agent_ids, cfg

    cfg["app"] = {
        "contact_invitations": [
            {"id": "a", "remoteAgentId": ""},
        ]
    }
    ids = get_known_app_remote_agent_ids()
    assert ids == {"default"}


# ──────────────────────────────────────────────────────────────────
# resolve_app_remote_agent tests
# ──────────────────────────────────────────────────────────────────


def test_resolve_default_succeeds():
    from main import resolve_app_remote_agent

    resolved = resolve_app_remote_agent("default")
    assert resolved["remoteAgentId"] == "default"
    assert resolved["target"] == "default"


def test_resolve_coder_succeeds_when_configured(monkeypatch):
    from main import resolve_app_remote_agent, cfg

    cfg["app"] = {
        "contact_invitations": [
            {"id": "a", "remoteAgentId": "default"},
            {"id": "b", "remoteAgentId": "coder"},
        ]
    }
    resolved = resolve_app_remote_agent("coder")
    assert resolved["remoteAgentId"] == "coder"
    assert resolved["target"] == "default"


def test_resolve_unknown_raises_400():
    from main import resolve_app_remote_agent

    with pytest.raises(HTTPException) as exc:
        resolve_app_remote_agent("unknown-agent")
    assert exc.value.status_code == 400
    assert "unknown remoteAgentId" in exc.value.detail
    assert "unknown-agent" in exc.value.detail


# ──────────────────────────────────────────────────────────────────
# Invitation remoteAgentId ↔ resolver consistency
# ──────────────────────────────────────────────────────────────────


def test_invitation_remote_agent_ids_are_known(monkeypatch):
    """Every remoteAgentId in contact-invitations is accepted by resolver."""
    from main import (
        build_app_contact_invitations,
        get_known_app_remote_agent_ids,
        resolve_app_remote_agent,
        cfg,
    )

    cfg["app"] = {
        "contact_invitations": [
            {"id": "a", "remoteAgentId": "default"},
            {"id": "b", "remoteAgentId": "coder"},
        ]
    }

    known = get_known_app_remote_agent_ids()
    invs = build_app_contact_invitations(
        _FakeAppRequest(base_url="http://testserver/"),
    )
    for inv in invs:
        rid = inv["remoteAgentId"]
        assert rid in known, f"invitation remoteAgentId {rid!r} not in known set"
        # resolver must not raise
        resolved = resolve_app_remote_agent(rid)
        assert resolved["remoteAgentId"] == rid


# ──────────────────────────────────────────────────────────────────
# Endpoint integration tests for remoteAgentId
# ──────────────────────────────────────────────────────────────────


def test_auth_before_remote_agent_id_validation(monkeypatch):
    """401 is returned before remoteAgentId is validated — no info leak."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    class FakeRequest:
        headers = {}

        async def json(self):
            return {"clientMessageId": "cm-1", "content": "hi", "remoteAgentId": "evil"}

    from main import _require_chat_auth, _api_config

    api_cfg = _api_config()
    with pytest.raises(HTTPException) as exc:
        _require_chat_auth(FakeRequest(), api_cfg)
    assert exc.value.status_code == 401
    # Unknown remoteAgentId error (400) must NOT be raised before auth


@pytest.mark.asyncio
async def test_post_messages_no_remote_agent_id_succeeds(monkeypatch):
    """POST /api/messages without remoteAgentId defaults to 'default'."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {"clientMessageId": "cm-1", "content": "hello"}

    from main import api_send_message

    # Should succeed (agent runtime will produce a reply — or empty)
    # Just verify it doesn't 400 on missing remoteAgentId
    result = await api_send_message(FakeRequest())
    assert "userMessage" in result


@pytest.mark.asyncio
async def test_post_messages_remote_agent_id_default_succeeds(monkeypatch):
    """POST /api/messages with remoteAgentId=default succeeds."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {
                "clientMessageId": "cm-2",
                "content": "hello",
                "remoteAgentId": "default",
            }

    from main import api_send_message

    result = await api_send_message(FakeRequest())
    assert "userMessage" in result


@pytest.mark.asyncio
async def test_post_messages_unknown_remote_agent_id_returns_400(monkeypatch):
    """POST /api/messages with unknown remoteAgentId returns 400."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {
                "clientMessageId": "cm-3",
                "content": "hello",
                "remoteAgentId": "evil-agent",
            }

    from main import api_send_message

    with pytest.raises(HTTPException) as exc:
        await api_send_message(FakeRequest())
    assert exc.value.status_code == 400
    assert "unknown remoteAgentId" in exc.value.detail
    assert "evil-agent" in exc.value.detail


@pytest.mark.asyncio
async def test_post_messages_coder_succeeds_when_configured(monkeypatch):
    """POST with remoteAgentId=coder succeeds when coder is in config."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import cfg

    cfg["app"] = {
        "contact_invitations": [
            {"id": "pc-agent-default", "remoteAgentId": "default"},
            {"id": "pc-agent-coder", "remoteAgentId": "coder"},
        ]
    }

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {
                "clientMessageId": "cm-coder",
                "content": "write some code",
                "remoteAgentId": "coder",
            }

    from main import api_send_message

    result = await api_send_message(FakeRequest())
    assert "userMessage" in result
    # Still uses current single runtime — just validates routing passes


@pytest.mark.asyncio
async def test_get_messages_no_remote_agent_id_succeeds(monkeypatch):
    """GET /api/messages without remoteAgentId succeeds."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

    from main import api_get_messages

    result = await api_get_messages(FakeRequest())
    assert "messages" in result
    assert isinstance(result["messages"], list)


@pytest.mark.asyncio
async def test_get_messages_unknown_remote_agent_id_returns_400(monkeypatch):
    """GET /api/messages with unknown remoteAgentId returns 400."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

    from main import api_get_messages

    with pytest.raises(HTTPException) as exc:
        await api_get_messages(FakeRequest(), remoteAgentId="evil-agent")
    assert exc.value.status_code == 400
    assert "unknown remoteAgentId" in exc.value.detail


@pytest.mark.asyncio
async def test_get_messages_coder_succeeds_when_configured(monkeypatch):
    """GET /api/messages with remoteAgentId=coder succeeds when in config."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import cfg

    cfg["app"] = {
        "contact_invitations": [
            {"id": "pc-agent-default", "remoteAgentId": "default"},
            {"id": "pc-agent-coder", "remoteAgentId": "coder"},
        ]
    }

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

    from main import api_get_messages

    result = await api_get_messages(FakeRequest(), remoteAgentId="coder")
    assert "messages" in result
    assert isinstance(result["messages"], list)


# ================================================================
# AppAgentSessionRegistry tests
# ================================================================


@pytest.mark.asyncio
async def test_session_registry_register_creates_online_session():
    from runtime.app_agent_session_registry import AppAgentSessionRegistry

    reg = AppAgentSessionRegistry()
    sess = await reg.register(
        session_id="s1",
        remote_agent_id="s1",
        display_name="Test Session",
        description="A test session",
        callback_url="http://127.0.0.1:9010",
    )
    assert sess["sessionId"] == "s1"
    assert sess["remoteAgentId"] == "s1"
    assert sess["displayName"] == "Test Session"
    assert sess["status"] == "online"
    assert sess["callbackUrl"] == "http://127.0.0.1:9010"
    assert sess["createdAt"].endswith("Z")
    assert sess["lastSeenAt"].endswith("Z")
    assert sess["expiresAt"] is None


@pytest.mark.asyncio
async def test_session_registry_repeat_register_updates_metadata():
    from runtime.app_agent_session_registry import AppAgentSessionRegistry

    reg = AppAgentSessionRegistry()
    await reg.register(
        session_id="s1",
        remote_agent_id="s1",
        display_name="Old Name",
    )
    sess2 = await reg.register(
        session_id="s1",
        remote_agent_id="s1",
        display_name="New Name",
        description="Updated",
    )
    assert sess2["displayName"] == "New Name"
    assert sess2["description"] == "Updated"
    assert sess2["status"] == "online"


@pytest.mark.asyncio
async def test_session_registry_same_remote_agent_id_conflict():
    from runtime.app_agent_session_registry import AppAgentSessionRegistry

    reg = AppAgentSessionRegistry()
    await reg.register(
        session_id="s1",
        remote_agent_id="same-rid",
        display_name="S1",
    )
    with pytest.raises(ValueError) as exc:
        await reg.register(
            session_id="s2",
            remote_agent_id="same-rid",
            display_name="S2",
        )
    assert "same-rid" in str(exc.value)
    assert "s1" in str(exc.value)


@pytest.mark.asyncio
async def test_session_registry_heartbeat_updates_last_seen():
    from runtime.app_agent_session_registry import AppAgentSessionRegistry

    reg = AppAgentSessionRegistry()
    await reg.register(
        session_id="s1",
        remote_agent_id="s1",
        display_name="S1",
    )
    sess = await reg.heartbeat("s1")
    assert sess["status"] == "online"
    assert sess["lastSeenAt"].endswith("Z")


@pytest.mark.asyncio
async def test_session_registry_heartbeat_missing_returns_none():
    from runtime.app_agent_session_registry import AppAgentSessionRegistry

    reg = AppAgentSessionRegistry()
    result = await reg.heartbeat("nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_session_registry_offline_marks_offline():
    from runtime.app_agent_session_registry import AppAgentSessionRegistry

    reg = AppAgentSessionRegistry()
    await reg.register(
        session_id="s1",
        remote_agent_id="s1",
        display_name="S1",
    )
    sess = await reg.offline("s1")
    assert sess["status"] == "offline"


@pytest.mark.asyncio
async def test_session_registry_offline_missing_returns_none():
    from runtime.app_agent_session_registry import AppAgentSessionRegistry

    reg = AppAgentSessionRegistry()
    result = await reg.offline("nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_session_registry_list_returns_sessions():
    from runtime.app_agent_session_registry import AppAgentSessionRegistry

    reg = AppAgentSessionRegistry()
    await reg.register(
        session_id="s1",
        remote_agent_id="s1",
        display_name="S1",
    )
    await reg.register(
        session_id="s2",
        remote_agent_id="s2",
        display_name="S2",
    )
    all_sessions = await reg.list_all()
    assert len(all_sessions) == 2


@pytest.mark.asyncio
async def test_session_registry_list_online_excludes_offline():
    from runtime.app_agent_session_registry import AppAgentSessionRegistry

    reg = AppAgentSessionRegistry()
    await reg.register(
        session_id="s1",
        remote_agent_id="s1",
        display_name="S1",
    )
    await reg.register(
        session_id="s2",
        remote_agent_id="s2",
        display_name="S2",
    )
    await reg.offline("s2")
    online = await reg.list_online()
    assert len(online) == 1
    assert online[0]["sessionId"] == "s1"


def test_session_registry_known_remote_agent_ids():
    """known_remote_agent_ids is sync and includes registered sessions."""
    from runtime.app_agent_session_registry import AppAgentSessionRegistry

    reg = AppAgentSessionRegistry()
    reg._sessions["s1"] = {
        "sessionId": "s1",
        "remoteAgentId": "cc-1",
        "displayName": "CC1",
        "description": "desc",
        "status": "online",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": "2026-01-01T00:00:00Z",
        "expiresAt": None,
        "callbackUrl": None,
        "sourceLabel": None,
    }
    ids = reg.known_remote_agent_ids()
    assert "cc-1" in ids


# ================================================================
# Invitations + session registry integration tests
# ================================================================


@pytest.mark.asyncio
async def test_invitations_includes_online_session(monkeypatch):
    """Online session appears in /api/contact-invitations."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")
    monkeypatch.setenv("COMPUTERNAME", "MY-PC")

    from main import cfg, app_agent_session_registry, api_contact_invitations

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    sid = f"cc-{_uuid.uuid4().hex[:8]}"
    await app_agent_session_registry.register(
        session_id=sid,
        remote_agent_id=sid,
        display_name="My Claude Code",
        description="Working on goal",
        callback_url="http://127.0.0.1:9010",
    )

    result = await api_contact_invitations(
        _FakeAppRequest(
            headers={"authorization": "Bearer secret"},
            base_url="http://10.0.2.2:8000/",
        )
    )

    invitations = result["invitations"]
    session_invs = [i for i in invitations if i["agentType"] == "attached-session"]
    assert len(session_invs) == 1
    sinv = session_invs[0]
    assert sinv["id"] == f"session-{sid}"
    assert sinv["remoteAgentId"] == sid
    assert sinv["displayName"] == "My Claude Code"
    assert sinv["description"] == "Working on goal"
    assert sinv["endpoint"] == "http://10.0.2.2:8000"
    assert sinv["avatarUri"] is None

    # Default still present
    pc_invs = [i for i in invitations if i["agentType"] == "pc-agent"]
    assert len(pc_invs) >= 1


@pytest.mark.asyncio
async def test_invitations_excludes_offline_session(monkeypatch):
    """Offline session does NOT appear in invitations."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import cfg, app_agent_session_registry, api_contact_invitations

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    sid = f"cc-{_uuid.uuid4().hex[:8]}"
    await app_agent_session_registry.register(
        session_id=sid,
        remote_agent_id=sid,
        display_name="Offline CC",
    )
    await app_agent_session_registry.offline(sid)

    result = await api_contact_invitations(
        _FakeAppRequest(
            headers={"authorization": "Bearer secret"},
            base_url="http://testserver/",
        )
    )

    invitations = result["invitations"]
    session_invs = [i for i in invitations if i["agentType"] == "attached-session"]
    assert len(session_invs) == 0


@pytest.mark.asyncio
async def test_invitations_session_remote_agent_id_conflict_uses_session(monkeypatch):
    """When config and session share remoteAgentId, session wins."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import cfg, app_agent_session_registry, api_contact_invitations

    app_agent_session_registry._sessions.clear()

    rid = f"shared-{_uuid.uuid4().hex[:8]}"
    sid = f"cc-{_uuid.uuid4().hex[:8]}"

    await app_agent_session_registry.register(
        session_id=sid,
        remote_agent_id=rid,
        display_name="Session Version",
        description="From session",
    )

    cfg["app"] = {
        "contact_invitations": [
            {"id": "config-entry", "remoteAgentId": rid, "displayName": "Config Version"},
        ]
    }

    result = await api_contact_invitations(
        _FakeAppRequest(
            headers={"authorization": "Bearer secret"},
            base_url="http://testserver/",
        )
    )

    invitations = result["invitations"]
    session_invs = [i for i in invitations if i["agentType"] == "attached-session"]
    assert len(session_invs) == 1
    assert session_invs[0]["displayName"] == "Session Version"
    config_matches = [i for i in invitations if i["remoteAgentId"] == rid and i["agentType"] == "pc-agent"]
    assert len(config_matches) == 0


# ================================================================
# Resolver + session registry integration tests
# ================================================================


def test_resolver_known_includes_session_remote_agent_id():
    from main import get_known_app_remote_agent_ids, cfg, app_agent_session_registry

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    app_agent_session_registry._sessions["s1"] = {
        "sessionId": "s1",
        "remoteAgentId": "cc-test",
        "displayName": "CC",
        "description": "desc",
        "status": "online",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": "2026-01-01T00:00:00Z",
        "expiresAt": None,
        "callbackUrl": None,
        "sourceLabel": None,
    }

    ids = get_known_app_remote_agent_ids()
    assert "cc-test" in ids
    assert "default" in ids


def test_resolve_session_online_returns_attached_session():
    from main import resolve_app_remote_agent, app_agent_session_registry

    app_agent_session_registry._sessions.clear()
    app_agent_session_registry._sessions["s1"] = {
        "sessionId": "s1",
        "remoteAgentId": "cc-online",
        "displayName": "CC",
        "description": "desc",
        "status": "online",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": "2026-01-01T00:00:00Z",
        "expiresAt": None,
        "callbackUrl": "http://127.0.0.1:9010",
        "sourceLabel": None,
    }

    resolved = resolve_app_remote_agent("cc-online")
    assert resolved["target"] == "attached_session"
    assert resolved["sessionId"] == "s1"
    assert resolved["status"] == "online"
    assert resolved["callbackUrl"] == "http://127.0.0.1:9010"


def test_resolve_session_offline_still_succeeds():
    from main import resolve_app_remote_agent, app_agent_session_registry

    app_agent_session_registry._sessions.clear()
    app_agent_session_registry._sessions["s1"] = {
        "sessionId": "s1",
        "remoteAgentId": "cc-offline",
        "displayName": "CC",
        "description": "desc",
        "status": "offline",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": "2026-01-01T00:00:00Z",
        "expiresAt": None,
        "callbackUrl": "http://127.0.0.1:9010",
        "sourceLabel": None,
    }

    resolved = resolve_app_remote_agent("cc-offline")
    assert resolved["target"] == "attached_session"
    assert resolved["status"] == "offline"


def test_resolve_unknown_still_400():
    from main import resolve_app_remote_agent, app_agent_session_registry

    app_agent_session_registry._sessions.clear()

    with pytest.raises(HTTPException) as exc:
        resolve_app_remote_agent("unknown-xyz")
    assert exc.value.status_code == 400


# ================================================================
# Endpoint tests: session registry API
# ================================================================


@pytest.mark.asyncio
async def test_register_session_endpoint_creates_session(monkeypatch):
    """POST /api/app-agent-sessions/register creates an online session."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    sid = f"cc-{_uuid.uuid4().hex[:8]}"

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {
                "sessionId": sid,
                "displayName": "Test CC",
                "description": "Testing",
                "callbackUrl": "http://127.0.0.1:9010",
            }

    from main import api_register_app_agent_session

    result = await api_register_app_agent_session(FakeRequest())
    assert "session" in result
    s = result["session"]
    assert s["sessionId"] == sid
    assert s["remoteAgentId"] == sid
    assert s["status"] == "online"
    assert s["displayName"] == "Test CC"


@pytest.mark.asyncio
async def test_register_session_endpoint_409_conflict(monkeypatch):
    """Same remoteAgentId different sessionId returns 409."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    app_agent_session_registry._sessions["s1"] = {
        "sessionId": "s1",
        "remoteAgentId": "rid-dup",
        "displayName": "S1",
        "description": "desc",
        "status": "online",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": "2026-01-01T00:00:00Z",
        "expiresAt": None,
        "callbackUrl": None,
        "sourceLabel": None,
    }

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {
                "sessionId": "s2",
                "remoteAgentId": "rid-dup",
                "displayName": "S2",
            }

    from main import api_register_app_agent_session

    with pytest.raises(HTTPException) as exc:
        await api_register_app_agent_session(FakeRequest())
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_register_session_endpoint_requires_auth(monkeypatch):
    """Unauthenticated register returns 401 before body validation."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    class FakeRequest:
        headers = {}
        base_url = "http://testserver/"

    from main import _require_chat_auth, _api_config

    api_cfg = _api_config()
    with pytest.raises(HTTPException) as exc:
        _require_chat_auth(FakeRequest(), api_cfg)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_heartbeat_endpoint_updates_session(monkeypatch):
    """POST /api/app-agent-sessions/{id}/heartbeat updates lastSeenAt."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    app_agent_session_registry._sessions["s1"] = {
        "sessionId": "s1",
        "remoteAgentId": "s1",
        "displayName": "S1",
        "description": "desc",
        "status": "offline",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": "2026-01-01T00:00:00Z",
        "expiresAt": None,
        "callbackUrl": None,
        "sourceLabel": None,
    }

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

    from main import api_app_agent_session_heartbeat

    result = await api_app_agent_session_heartbeat("s1", FakeRequest())
    assert result["session"]["status"] == "online"
    assert result["session"]["lastSeenAt"] > "2026-01-01"


@pytest.mark.asyncio
async def test_heartbeat_endpoint_missing_returns_404(monkeypatch):
    """Heartbeat for unknown session returns 404."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

    from main import api_app_agent_session_heartbeat

    with pytest.raises(HTTPException) as exc:
        await api_app_agent_session_heartbeat("nonexistent", FakeRequest())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_offline_endpoint_marks_offline(monkeypatch):
    """POST /api/app-agent-sessions/{id}/offline marks offline."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    app_agent_session_registry._sessions["s1"] = {
        "sessionId": "s1",
        "remoteAgentId": "s1",
        "displayName": "S1",
        "description": "desc",
        "status": "online",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": "2026-01-01T00:00:00Z",
        "expiresAt": None,
        "callbackUrl": None,
        "sourceLabel": None,
    }

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

    from main import api_app_agent_session_offline

    result = await api_app_agent_session_offline("s1", FakeRequest())
    assert result["session"]["status"] == "offline"


@pytest.mark.asyncio
async def test_list_sessions_endpoint(monkeypatch):
    """GET /api/app-agent-sessions returns list."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    app_agent_session_registry._sessions["s1"] = {
        "sessionId": "s1",
        "remoteAgentId": "s1",
        "displayName": "S1",
        "description": "desc",
        "status": "online",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": "2026-01-01T00:00:00Z",
        "expiresAt": None,
        "callbackUrl": None,
        "sourceLabel": None,
    }

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

    from main import api_list_app_agent_sessions

    result = await api_list_app_agent_sessions(FakeRequest())
    assert len(result["sessions"]) == 1


# ================================================================
# POST /api/messages — attached session routing guards
# ================================================================


@pytest.mark.asyncio
async def test_post_messages_attached_session_no_callback_returns_501(monkeypatch):
    """POST with attached session without callbackUrl returns 501, no insert."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"cc-nocb-{_uuid.uuid4().hex[:8]}"
    app_agent_session_registry._sessions["s1"] = {
        "sessionId": "s1",
        "remoteAgentId": rid,
        "displayName": "No CB",
        "description": "desc",
        "status": "online",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": "2026-01-01T00:00:00Z",
        "expiresAt": None,
        "callbackUrl": None,
        "sourceLabel": None,
    }

    cid = f"cm-nocb-{_uuid.uuid4().hex[:8]}"

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {
                "clientMessageId": cid,
                "content": "hello",
                "remoteAgentId": rid,
            }

    from main import api_send_message

    with pytest.raises(HTTPException) as exc:
        await api_send_message(FakeRequest())
    assert exc.value.status_code == 501

    from main import memory_store
    found = await memory_store.find_app_message_by_client_message_id(
        cid, remote_agent_id=rid,
    )
    assert found is None


@pytest.mark.asyncio
async def test_post_messages_attached_session_offline_returns_503(monkeypatch):
    """POST with offline attached session returns 503, no insert."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"cc-off-{_uuid.uuid4().hex[:8]}"
    app_agent_session_registry._sessions["s1"] = {
        "sessionId": "s1",
        "remoteAgentId": rid,
        "displayName": "Offline CC",
        "description": "desc",
        "status": "offline",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": "2026-01-01T00:00:00Z",
        "expiresAt": None,
        "callbackUrl": "http://127.0.0.1:9010",
        "sourceLabel": None,
    }

    cid = f"cm-off-{_uuid.uuid4().hex[:8]}"

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {
                "clientMessageId": cid,
                "content": "hello",
                "remoteAgentId": rid,
            }

    from main import api_send_message

    with pytest.raises(HTTPException) as exc:
        await api_send_message(FakeRequest())
    assert exc.value.status_code == 503

    from main import memory_store
    found = await memory_store.find_app_message_by_client_message_id(
        cid, remote_agent_id=rid,
    )
    assert found is None


@pytest.mark.asyncio
async def test_post_messages_default_still_works(monkeypatch):
    """POST /api/messages with default remoteAgentId still works."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {
                "clientMessageId": "cm-default-ok",
                "content": "hello default",
            }

    from main import api_send_message

    result = await api_send_message(FakeRequest())
    assert "userMessage" in result


# ================================================================
# GET /api/messages — attached session history
# ================================================================


@pytest.mark.asyncio
async def test_get_messages_attached_session_offline_still_reads_history(monkeypatch):
    """GET /api/messages works for offline attached session (reads history)."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"cc-hist-{_uuid.uuid4().hex[:8]}"
    app_agent_session_registry._sessions["s1"] = {
        "sessionId": "s1",
        "remoteAgentId": rid,
        "displayName": "History CC",
        "description": "desc",
        "status": "offline",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": "2026-01-01T00:00:00Z",
        "expiresAt": None,
        "callbackUrl": "http://127.0.0.1:9010",
        "sourceLabel": None,
    }

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

    from main import api_get_messages

    result = await api_get_messages(FakeRequest(), remoteAgentId=rid)
    assert "messages" in result
    assert isinstance(result["messages"], list)


@pytest.mark.asyncio
async def test_get_messages_unknown_still_400(monkeypatch):
    """GET /api/messages with unknown remoteAgentId still returns 400."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

    from main import api_get_messages

    with pytest.raises(HTTPException) as exc:
        await api_get_messages(FakeRequest(), remoteAgentId="unknown-xyz")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_auth_before_session_validation(monkeypatch):
    """401 returned before session/remoteAgentId validation — no info leak."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    class FakeRequest:
        headers = {}

    from main import _require_chat_auth, _api_config

    api_cfg = _api_config()
    with pytest.raises(HTTPException) as exc:
        _require_chat_auth(FakeRequest(), api_cfg)
    assert exc.value.status_code == 401


# ================================================================
# validate_callback_response tests (v1 contract)
# ================================================================


def test_validate_callback_valid_simple_text():
    from main import validate_callback_response

    result = validate_callback_response({
        "agentReplies": [{"content": "你好，我在。"}],
    })
    assert result["ok"] is True
    assert len(result["replies"]) == 1
    assert result["replies"][0]["content"] == "你好，我在。"


def test_validate_callback_missing_agent_replies():
    from main import validate_callback_response

    result = validate_callback_response({})
    assert result["ok"] is False
    assert "missing agentReplies" in result["error"]


def test_validate_callback_top_level_array_rejected():
    from main import validate_callback_response

    result = validate_callback_response([{"content": "hi"}])
    assert result["ok"] is False
    assert "JSON object" in result["error"]


def test_validate_callback_agent_replies_not_list():
    from main import validate_callback_response

    result = validate_callback_response({"agentReplies": "not-a-list"})
    assert result["ok"] is False
    assert "must be an array" in result["error"]


def test_validate_callback_reply_content_not_string_skipped():
    from main import validate_callback_response

    result = validate_callback_response({
        "agentReplies": [
            {"content": "valid"},
            {"content": 123},
        ],
    })
    assert result["ok"] is True
    assert len(result["replies"]) == 1
    assert result["replies"][0]["content"] == "valid"


def test_validate_callback_empty_content_skipped():
    from main import validate_callback_response

    result = validate_callback_response({
        "agentReplies": [
            {"content": ""},
            {"content": "  "},
            {"content": "valid"},
        ],
    })
    assert result["ok"] is True
    assert len(result["replies"]) == 1
    assert result["replies"][0]["content"] == "valid"


def test_validate_callback_mixed_valid_and_invalid():
    from main import validate_callback_response

    result = validate_callback_response({
        "agentReplies": [
            "not-a-dict",
            {"content": "first"},
            {"no_content": "missing"},
            {"content": "second"},
            {"content": 456},
            {"content": ""},
        ],
    })
    assert result["ok"] is True
    assert len(result["replies"]) == 2
    contents = {r["content"] for r in result["replies"]}
    assert contents == {"first", "second"}


def test_validate_callback_empty_replies_ok():
    from main import validate_callback_response

    result = validate_callback_response({"agentReplies": []})
    assert result["ok"] is True
    assert result["replies"] == []


def test_validate_callback_unknown_fields_ignored():
    from main import validate_callback_response

    result = validate_callback_response({
        "agentReplies": [{"content": "hi", "unknownField": "ignored"}],
        "extraTop": 123,
    })
    assert result["ok"] is True
    assert len(result["replies"]) == 1
    assert result["replies"][0]["content"] == "hi"


def test_validate_callback_optional_fields_preserved():
    from main import validate_callback_response

    result = validate_callback_response({
        "agentReplies": [
            {
                "content": "hi",
                "remoteId": "rm-1",
                "createdAt": "2026-06-19T12:00:02Z",
            },
        ],
    })
    assert result["ok"] is True
    assert result["replies"][0]["remoteId"] == "rm-1"
    assert result["replies"][0]["createdAt"] == "2026-06-19T12:00:02Z"


def test_validate_callback_content_strips_whitespace():
    from main import validate_callback_response

    result = validate_callback_response({
        "agentReplies": [{"content": "  hello world  "}],
    })
    assert result["ok"] is True
    assert result["replies"][0]["content"] == "hello world"


def test_validate_callback_anthropic_content_block_rejected_as_content():
    """Anthropic content blocks {type,text} are NOT valid content — adapter must convert."""
    from main import validate_callback_response

    result = validate_callback_response({
        "agentReplies": [
            {"content": {"type": "text", "text": "hello"}},
        ],
    })
    assert result["ok"] is True
    # content is a dict, not a string → skipped
    assert result["replies"] == []


def test_validate_callback_string_not_object_rejected():
    from main import validate_callback_response

    result = validate_callback_response("just a string")
    assert result["ok"] is False
    assert "JSON object" in result["error"]


def test_validate_callback_multiple_replies():
    from main import validate_callback_response

    result = validate_callback_response({
        "agentReplies": [
            {"content": "第一条"},
            {"content": "第二条"},
            {"content": "第三条"},
        ],
    })
    assert result["ok"] is True
    assert len(result["replies"]) == 3


# ================================================================
# Callback URL builder tests
# ================================================================


def test_build_session_callback_url_with_trailing_slash():
    from main import _build_session_callback_url

    url = _build_session_callback_url("http://127.0.0.1:9010/")
    assert url == "http://127.0.0.1:9010/api/app-session/messages"


def test_build_session_callback_url_without_trailing_slash():
    from main import _build_session_callback_url

    url = _build_session_callback_url("http://127.0.0.1:9010")
    assert url == "http://127.0.0.1:9010/api/app-session/messages"


# ================================================================
# Callback forwarding — success path tests
# ================================================================


@pytest.mark.asyncio
async def test_callback_success_returns_user_message_and_replies(monkeypatch):
    """Attached session callback success returns userMessage + agentReplies."""
    import uuid as _uuid
    import httpx
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"cc-ok-{_uuid.uuid4().hex[:8]}"
    app_agent_session_registry._sessions["s1"] = {
        "sessionId": "s1",
        "remoteAgentId": rid,
        "displayName": "OK CC",
        "description": "desc",
        "status": "online",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": "2026-01-01T00:00:00Z",
        "expiresAt": None,
        "callbackUrl": "http://127.0.0.1:19010",
        "sourceLabel": None,
    }

    cid = f"cm-ok-{_uuid.uuid4().hex[:8]}"

    # Mock httpx.AsyncClient to return a valid callback response
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "agentReplies": [
            {"content": "你好，我在。", "remoteId": f"session-rm-{_uuid.uuid4().hex[:8]}"},
            {"content": "有什么可以帮你的？"},
        ],
    }

    class _FakeClientCtx:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            client = MagicMock()
            client.post = AsyncMock(return_value=fake_resp)
            return client

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClientCtx)

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {
                "clientMessageId": cid,
                "content": "你好",
                "remoteAgentId": rid,
            }

    from main import api_send_message

    result = await api_send_message(FakeRequest())
    assert "userMessage" in result
    assert result["userMessage"]["clientMessageId"] == cid
    assert result["userMessage"]["status"] == "sent"
    assert len(result["agentReplies"]) == 2
    assert result["agentReplies"][0]["content"] == "你好，我在。"
    assert result["agentReplies"][1]["content"] == "有什么可以帮你的？"


@pytest.mark.asyncio
async def test_callback_success_writes_messages_under_remote_agent_id(monkeypatch):
    """Callback success persists user + agent messages under the correct remote_agent_id."""
    import uuid as _uuid
    import httpx
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg, memory_store

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"cc-write-{_uuid.uuid4().hex[:8]}"
    app_agent_session_registry._sessions["s1"] = {
        "sessionId": "s1",
        "remoteAgentId": rid,
        "displayName": "Write CC",
        "description": "desc",
        "status": "online",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": "2026-01-01T00:00:00Z",
        "expiresAt": None,
        "callbackUrl": "http://127.0.0.1:19010",
        "sourceLabel": None,
    }

    cid = f"cm-write-{_uuid.uuid4().hex[:8]}"

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "agentReplies": [{"content": "stored reply"}],
    }

    class _FakeClientCtx:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            client = MagicMock()
            client.post = AsyncMock(return_value=fake_resp)
            return client

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClientCtx)

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {
                "clientMessageId": cid,
                "content": "hello",
                "remoteAgentId": rid,
            }

    from main import api_send_message

    result = await api_send_message(FakeRequest())
    assert len(result["agentReplies"]) == 1

    # Verify storage
    found = await memory_store.find_app_message_by_client_message_id(
        cid, remote_agent_id=rid,
    )
    assert found is not None
    assert found["remote_agent_id"] == rid
    assert found["content"] == "hello"

    # Verify GET returns these messages
    msgs, _, _ = await memory_store.get_app_messages_after_cursor(
        remote_agent_id=rid,
    )
    assert len(msgs) >= 2  # user + agent


@pytest.mark.asyncio
async def test_callback_receives_correct_payload(monkeypatch):
    """Callback POST body includes remoteAgentId, sessionId, clientMessageId, content, createdAt."""
    import uuid as _uuid
    import httpx
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"cc-payload-{_uuid.uuid4().hex[:8]}"
    app_agent_session_registry._sessions["s1"] = {
        "sessionId": "s1",
        "remoteAgentId": rid,
        "displayName": "Payload CC",
        "description": "desc",
        "status": "online",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": "2026-01-01T00:00:00Z",
        "expiresAt": None,
        "callbackUrl": "http://127.0.0.1:19010",
        "sourceLabel": None,
    }

    cid = f"cm-payload-{_uuid.uuid4().hex[:8]}"

    captured_payload = {}

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "agentReplies": [{"content": "got it"}],
    }

    class _FakeClientCtx:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            client = MagicMock()

            async def _capture_post(url, json=None, **kw):
                nonlocal captured_payload
                captured_payload["url"] = url
                captured_payload["json"] = json
                return fake_resp

            client.post = _capture_post
            return client

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClientCtx)

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {
                "clientMessageId": cid,
                "content": "hello session",
                "remoteAgentId": rid,
            }

    from main import api_send_message

    await api_send_message(FakeRequest())

    assert captured_payload["url"] == "http://127.0.0.1:19010/api/app-session/messages"
    assert captured_payload["json"]["remoteAgentId"] == rid
    assert captured_payload["json"]["sessionId"] == "s1"
    assert captured_payload["json"]["clientMessageId"] == cid
    assert captured_payload["json"]["content"] == "hello session"
    assert captured_payload["json"]["createdAt"].endswith("Z")


@pytest.mark.asyncio
async def test_callback_empty_reply_content_skipped(monkeypatch):
    """Empty/whitespace reply content is skipped."""
    import uuid as _uuid
    import httpx
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"cc-empty-{_uuid.uuid4().hex[:8]}"
    app_agent_session_registry._sessions["s1"] = {
        "sessionId": "s1",
        "remoteAgentId": rid,
        "displayName": "Empty CC",
        "description": "desc",
        "status": "online",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": "2026-01-01T00:00:00Z",
        "expiresAt": None,
        "callbackUrl": "http://127.0.0.1:19010",
        "sourceLabel": None,
    }

    cid = f"cm-empty-{_uuid.uuid4().hex[:8]}"

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "agentReplies": [
            {"content": ""},
            {"content": "  "},
            {"content": "valid reply"},
        ],
    }

    class _FakeClientCtx:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            client = MagicMock()
            client.post = AsyncMock(return_value=fake_resp)
            return client

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClientCtx)

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {
                "clientMessageId": cid,
                "content": "hi",
                "remoteAgentId": rid,
            }

    from main import api_send_message

    result = await api_send_message(FakeRequest())
    assert len(result["agentReplies"]) == 1
    assert result["agentReplies"][0]["content"] == "valid reply"


@pytest.mark.asyncio
async def test_callback_missing_remote_id_gets_generated(monkeypatch):
    """Missing reply remoteId gets a server-generated app_rm_agent_xxx."""
    import uuid as _uuid
    import httpx
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"cc-genid-{_uuid.uuid4().hex[:8]}"
    app_agent_session_registry._sessions["s1"] = {
        "sessionId": "s1",
        "remoteAgentId": rid,
        "displayName": "GenId CC",
        "description": "desc",
        "status": "online",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": "2026-01-01T00:00:00Z",
        "expiresAt": None,
        "callbackUrl": "http://127.0.0.1:19010",
        "sourceLabel": None,
    }

    cid = f"cm-genid-{_uuid.uuid4().hex[:8]}"

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "agentReplies": [{"content": "no remoteId"}],
    }

    class _FakeClientCtx:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            client = MagicMock()
            client.post = AsyncMock(return_value=fake_resp)
            return client

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClientCtx)

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {
                "clientMessageId": cid,
                "content": "hi",
                "remoteAgentId": rid,
            }

    from main import api_send_message

    result = await api_send_message(FakeRequest())
    assert len(result["agentReplies"]) == 1
    assert result["agentReplies"][0]["remoteId"].startswith("app_rm_agent_")
    assert result["agentReplies"][0]["content"] == "no remoteId"


@pytest.mark.asyncio
async def test_callback_missing_created_at_gets_server_timestamp(monkeypatch):
    """Missing reply createdAt gets server current UTC Z time."""
    import uuid as _uuid
    import httpx
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"cc-nots-{_uuid.uuid4().hex[:8]}"
    app_agent_session_registry._sessions["s1"] = {
        "sessionId": "s1",
        "remoteAgentId": rid,
        "displayName": "NoTs CC",
        "description": "desc",
        "status": "online",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": "2026-01-01T00:00:00Z",
        "expiresAt": None,
        "callbackUrl": "http://127.0.0.1:19010",
        "sourceLabel": None,
    }

    cid = f"cm-nots-{_uuid.uuid4().hex[:8]}"

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "agentReplies": [{"content": "no timestamp"}],
    }

    class _FakeClientCtx:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            client = MagicMock()
            client.post = AsyncMock(return_value=fake_resp)
            return client

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClientCtx)

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {
                "clientMessageId": cid,
                "content": "hi",
                "remoteAgentId": rid,
            }

    from main import api_send_message

    result = await api_send_message(FakeRequest())
    assert len(result["agentReplies"]) == 1
    assert result["agentReplies"][0]["createdAt"].endswith("Z")


# ================================================================
# Callback forwarding — failure path tests
# ================================================================


@pytest.mark.asyncio
async def test_callback_non_2xx_returns_502_no_insert(monkeypatch):
    """Callback returning non-2xx → 502, no app_messages inserted."""
    import uuid as _uuid
    import httpx
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg, memory_store

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"cc-502-{_uuid.uuid4().hex[:8]}"
    app_agent_session_registry._sessions["s1"] = {
        "sessionId": "s1",
        "remoteAgentId": rid,
        "displayName": "Fail CC",
        "description": "desc",
        "status": "online",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": "2026-01-01T00:00:00Z",
        "expiresAt": None,
        "callbackUrl": "http://127.0.0.1:19010",
        "sourceLabel": None,
    }

    cid = f"cm-502-{_uuid.uuid4().hex[:8]}"

    fake_resp = MagicMock()
    fake_resp.status_code = 500
    fake_resp.text = "Internal Server Error"

    class _FakeClientCtx:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            client = MagicMock()
            client.post = AsyncMock(return_value=fake_resp)
            return client

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClientCtx)

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {
                "clientMessageId": cid,
                "content": "hello",
                "remoteAgentId": rid,
            }

    from main import api_send_message

    with pytest.raises(HTTPException) as exc:
        await api_send_message(FakeRequest())
    assert exc.value.status_code == 502

    # Verify no message was inserted
    found = await memory_store.find_app_message_by_client_message_id(
        cid, remote_agent_id=rid,
    )
    assert found is None


@pytest.mark.asyncio
async def test_callback_timeout_returns_504_no_insert(monkeypatch):
    """Callback timeout → 504, no app_messages inserted."""
    import uuid as _uuid
    import httpx
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg, memory_store

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"cc-timeout-{_uuid.uuid4().hex[:8]}"
    app_agent_session_registry._sessions["s1"] = {
        "sessionId": "s1",
        "remoteAgentId": rid,
        "displayName": "Timeout CC",
        "description": "desc",
        "status": "online",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": "2026-01-01T00:00:00Z",
        "expiresAt": None,
        "callbackUrl": "http://127.0.0.1:19010",
        "sourceLabel": None,
    }

    cid = f"cm-timeout-{_uuid.uuid4().hex[:8]}"

    class _FakeClientCtx:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            client = MagicMock()

            async def _raise_timeout(*args, **kwargs):
                raise httpx.TimeoutException("timed out")

            client.post = _raise_timeout
            return client

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClientCtx)

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {
                "clientMessageId": cid,
                "content": "hello",
                "remoteAgentId": rid,
            }

    from main import api_send_message

    with pytest.raises(HTTPException) as exc:
        await api_send_message(FakeRequest())
    assert exc.value.status_code == 504

    found = await memory_store.find_app_message_by_client_message_id(
        cid, remote_agent_id=rid,
    )
    assert found is None


@pytest.mark.asyncio
async def test_callback_network_error_returns_502_no_insert(monkeypatch):
    """Callback network error → 502, no app_messages inserted."""
    import uuid as _uuid
    import httpx
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg, memory_store

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"cc-net-{_uuid.uuid4().hex[:8]}"
    app_agent_session_registry._sessions["s1"] = {
        "sessionId": "s1",
        "remoteAgentId": rid,
        "displayName": "NetErr CC",
        "description": "desc",
        "status": "online",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": "2026-01-01T00:00:00Z",
        "expiresAt": None,
        "callbackUrl": "http://127.0.0.1:19010",
        "sourceLabel": None,
    }

    cid = f"cm-net-{_uuid.uuid4().hex[:8]}"

    class _FakeClientCtx:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            client = MagicMock()

            async def _raise_connect_error(*args, **kwargs):
                raise OSError("Connection refused")

            client.post = _raise_connect_error
            return client

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClientCtx)

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {
                "clientMessageId": cid,
                "content": "hello",
                "remoteAgentId": rid,
            }

    from main import api_send_message

    with pytest.raises(HTTPException) as exc:
        await api_send_message(FakeRequest())
    assert exc.value.status_code == 502

    found = await memory_store.find_app_message_by_client_message_id(
        cid, remote_agent_id=rid,
    )
    assert found is None


@pytest.mark.asyncio
async def test_callback_invalid_json_returns_502_no_insert(monkeypatch):
    """Callback returns non-JSON body → 502, no app_messages inserted."""
    import uuid as _uuid
    import httpx
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg, memory_store

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"cc-badj-{_uuid.uuid4().hex[:8]}"
    app_agent_session_registry._sessions["s1"] = {
        "sessionId": "s1",
        "remoteAgentId": rid,
        "displayName": "BadJson CC",
        "description": "desc",
        "status": "online",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": "2026-01-01T00:00:00Z",
        "expiresAt": None,
        "callbackUrl": "http://127.0.0.1:19010",
        "sourceLabel": None,
    }

    cid = f"cm-badj-{_uuid.uuid4().hex[:8]}"

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.text = "not json at all"
    fake_resp.json.side_effect = ValueError("not JSON")

    class _FakeClientCtx:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            client = MagicMock()
            client.post = AsyncMock(return_value=fake_resp)
            return client

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClientCtx)

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {
                "clientMessageId": cid,
                "content": "hello",
                "remoteAgentId": rid,
            }

    from main import api_send_message

    with pytest.raises(HTTPException) as exc:
        await api_send_message(FakeRequest())
    assert exc.value.status_code == 502

    found = await memory_store.find_app_message_by_client_message_id(
        cid, remote_agent_id=rid,
    )
    assert found is None


@pytest.mark.asyncio
async def test_callback_missing_agent_replies_returns_502_no_insert(monkeypatch):
    """Callback response without agentReplies list → 502, no insert."""
    import uuid as _uuid
    import httpx
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg, memory_store

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"cc-norep-{_uuid.uuid4().hex[:8]}"
    app_agent_session_registry._sessions["s1"] = {
        "sessionId": "s1",
        "remoteAgentId": rid,
        "displayName": "NoRep CC",
        "description": "desc",
        "status": "online",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": "2026-01-01T00:00:00Z",
        "expiresAt": None,
        "callbackUrl": "http://127.0.0.1:19010",
        "sourceLabel": None,
    }

    cid = f"cm-norep-{_uuid.uuid4().hex[:8]}"

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {}  # no agentReplies

    class _FakeClientCtx:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            client = MagicMock()
            client.post = AsyncMock(return_value=fake_resp)
            return client

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClientCtx)

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {
                "clientMessageId": cid,
                "content": "hello",
                "remoteAgentId": rid,
            }

    from main import api_send_message

    with pytest.raises(HTTPException) as exc:
        await api_send_message(FakeRequest())
    assert exc.value.status_code == 502

    found = await memory_store.find_app_message_by_client_message_id(
        cid, remote_agent_id=rid,
    )
    assert found is None


# ================================================================
# Callback forwarding — idempotency tests
# ================================================================


@pytest.mark.asyncio
async def test_callback_idempotency_second_post_returns_stored(monkeypatch):
    """Second POST with same remoteAgentId + clientMessageId returns stored, no re-call."""
    import uuid as _uuid
    import httpx
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"cc-idem-{_uuid.uuid4().hex[:8]}"
    app_agent_session_registry._sessions["s1"] = {
        "sessionId": "s1",
        "remoteAgentId": rid,
        "displayName": "Idem CC",
        "description": "desc",
        "status": "online",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": "2026-01-01T00:00:00Z",
        "expiresAt": None,
        "callbackUrl": "http://127.0.0.1:19010",
        "sourceLabel": None,
    }

    cid = f"cm-idem-{_uuid.uuid4().hex[:8]}"
    call_count = 0

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "agentReplies": [{"content": "first reply"}],
    }

    class _FakeClientCtx:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            client = MagicMock()

            async def _counted_post(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                return fake_resp

            client.post = _counted_post
            return client

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClientCtx)

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        def __init__(self, cid, content, rid):
            self._cid = cid
            self._content = content
            self._rid = rid

        async def json(self):
            return {
                "clientMessageId": self._cid,
                "content": self._content,
                "remoteAgentId": self._rid,
            }

    from main import api_send_message

    # First call
    result1 = await api_send_message(FakeRequest(cid, "hello", rid))
    assert result1["agentReplies"][0]["content"] == "first reply"
    assert call_count == 1

    # Second call — same cid, same rid
    result2 = await api_send_message(FakeRequest(cid, "hello", rid))
    assert result2["agentReplies"][0]["content"] == "first reply"
    assert call_count == 1  # callback NOT called again


@pytest.mark.asyncio
async def test_callback_idempotency_different_rid_calls_different_callback(monkeypatch):
    """Same clientMessageId under different remoteAgentId calls different callback."""
    import uuid as _uuid
    import httpx
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid1 = f"cc-rid1-{_uuid.uuid4().hex[:8]}"
    rid2 = f"cc-rid2-{_uuid.uuid4().hex[:8]}"

    app_agent_session_registry._sessions["s1"] = {
        "sessionId": "s1",
        "remoteAgentId": rid1,
        "displayName": "R1",
        "description": "desc",
        "status": "online",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": "2026-01-01T00:00:00Z",
        "expiresAt": None,
        "callbackUrl": "http://127.0.0.1:19010",
        "sourceLabel": None,
    }
    app_agent_session_registry._sessions["s2"] = {
        "sessionId": "s2",
        "remoteAgentId": rid2,
        "displayName": "R2",
        "description": "desc",
        "status": "online",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": "2026-01-01T00:00:00Z",
        "expiresAt": None,
        "callbackUrl": "http://127.0.0.1:19011",
        "sourceLabel": None,
    }

    cid = f"cm-cross-{_uuid.uuid4().hex[:8]}"
    call_urls = []

    class _FakeClientCtx:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            client = MagicMock()

            async def _tracked_post(url, *args, **kwargs):
                call_urls.append(url)
                resp = MagicMock()
                resp.status_code = 200
                resp.json.return_value = {
                    "agentReplies": [{"content": f"reply from {url}"}],
                }
                return resp

            client.post = _tracked_post
            return client

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClientCtx)

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        def __init__(self, cid, content, rid):
            self._cid = cid
            self._content = content
            self._rid = rid

        async def json(self):
            return {
                "clientMessageId": self._cid,
                "content": self._content,
                "remoteAgentId": self._rid,
            }

    from main import api_send_message

    result1 = await api_send_message(FakeRequest(cid, "hello r1", rid1))
    result2 = await api_send_message(FakeRequest(cid, "hello r2", rid2))

    assert "userMessage" in result1
    assert "userMessage" in result2
    assert len(call_urls) == 2  # both callbacks fired


# ================================================================
# Regression — existing behavior preserved
# ================================================================


@pytest.mark.asyncio
async def test_default_post_still_works_with_callback_code_present(monkeypatch):
    """POST /api/messages with default remoteAgentId still works."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {
                "clientMessageId": "cm-regress-default",
                "content": "hello default",
            }

    from main import api_send_message

    result = await api_send_message(FakeRequest())
    assert "userMessage" in result


@pytest.mark.asyncio
async def test_offline_still_503(monkeypatch):
    """Offline attached session still returns 503 (unchanged)."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"cc-offreg-{_uuid.uuid4().hex[:8]}"
    app_agent_session_registry._sessions["s1"] = {
        "sessionId": "s1",
        "remoteAgentId": rid,
        "displayName": "Off CC",
        "description": "desc",
        "status": "offline",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": "2026-01-01T00:00:00Z",
        "expiresAt": None,
        "callbackUrl": "http://127.0.0.1:19010",
        "sourceLabel": None,
    }

    cid = f"cm-offreg-{_uuid.uuid4().hex[:8]}"

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {
                "clientMessageId": cid,
                "content": "hello",
                "remoteAgentId": rid,
            }

    from main import api_send_message

    with pytest.raises(HTTPException) as exc:
        await api_send_message(FakeRequest())
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_no_callback_still_501(monkeypatch):
    """Attached session without callbackUrl still returns 501 (unchanged)."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"cc-nocbreg-{_uuid.uuid4().hex[:8]}"
    app_agent_session_registry._sessions["s1"] = {
        "sessionId": "s1",
        "remoteAgentId": rid,
        "displayName": "NoCB CC",
        "description": "desc",
        "status": "online",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": "2026-01-01T00:00:00Z",
        "expiresAt": None,
        "callbackUrl": None,
        "sourceLabel": None,
    }

    cid = f"cm-nocbreg-{_uuid.uuid4().hex[:8]}"

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {
                "clientMessageId": cid,
                "content": "hello",
                "remoteAgentId": rid,
            }

    from main import api_send_message

    with pytest.raises(HTTPException) as exc:
        await api_send_message(FakeRequest())
    assert exc.value.status_code == 501


@pytest.mark.asyncio
async def test_auth_before_remote_agent_id_validation_still_holds(monkeypatch):
    """401 returned before remoteAgentId validation — no info leak."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    class FakeRequest:
        headers = {}

    from main import _require_chat_auth, _api_config

    api_cfg = _api_config()
    with pytest.raises(HTTPException) as exc:
        _require_chat_auth(FakeRequest(), api_cfg)
    assert exc.value.status_code == 401
