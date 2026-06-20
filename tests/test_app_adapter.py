"""Tests for the AsPocket app adapter endpoints (/api/*)."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException


def _fresh_last_seen() -> str:
    """A current UTC ``...Z`` timestamp for fixture ``lastSeenAt``.

    The registry's stale-TTL treats sessions whose ``lastSeenAt`` is older
    than 90s as offline.  Fixtures that want an *online* session must use a
    fresh timestamp — the historical ``2026-01-01T00:00:00Z`` would now be
    stale.  Tests that want a stale session set ``lastSeenAt`` explicitly.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
async def test_app_messages_ordering_stable_when_created_at_ties():
    """user/reply written with the same created_at must keep user before reply.

    Regression guard for a flaky history-sort failure: when the user message
    and its agent reply share an identical ``created_at`` (e.g. written within
    one ``time.time()`` tick on the claude_channel path), ordering by
    ``created_at`` alone leaves the tie to SQLite's non-deterministic internal
    order.  Tying the break on the lexicographic ``id`` (``app_rm_agent_*`` <
    ``app_rm_user_*``) sorts the reply *before* the user, flipping the history.

    The composite key ``created_at, source_turn_id, sequence, id`` keeps the
    user (sequence 0) ahead of its reply (sequence >= 1) regardless of id.
    """
    from memory.store import MemoryStore

    store = MemoryStore(db_path=":memory:")
    try:
        tied_ts = 1_750_000_000.0  # identical timestamp for both rows
        await store.insert_app_message(
            remote_id="app_rm_user_aaaaaaaaaaaa",
            client_message_id="cm-tie",
            role="user",
            content="hi hist",
            source_turn_id="turn-tie",
            sequence=0,
            remote_agent_id="default",
            created_at=tied_ts,
        )
        await store.insert_app_message(
            remote_id="app_rm_agent_bbbbbbbbbbbb",
            client_message_id=None,
            role="agent",
            content="reply hist",
            source_turn_id="turn-tie",
            sequence=1,
            remote_agent_id="default",
            created_at=tied_ts,
        )

        messages, _, _ = await store.get_app_messages_after_cursor(
            remote_agent_id="default",
        )
        assert [m["role"] for m in messages] == ["user", "agent"]
        assert messages[0]["content"] == "hi hist"
        assert messages[1]["content"] == "reply hist"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_app_messages_cursor_no_drop_or_dup_on_created_at_tie():
    """Cursor pagination across tied ``created_at`` must not drop/duplicate rows.

    With several rows sharing the same ``created_at`` and the page boundary
    falling in the middle of that tie group, a cursor that only keys on
    ``created_at`` (or on ``id`` alone) can either skip rows or repeat them.
    The composite cursor ``(created_at, source_turn_id, sequence, id)`` keeps
    the page walk stable.
    """
    from memory.store import MemoryStore

    store = MemoryStore(db_path=":memory:")
    try:
        tied_ts = 1_750_000_000.0
        # Five rows sharing one timestamp, distinct sequence/turn within.
        for seq in range(5):
            await store.insert_app_message(
                remote_id=f"app_rm_row_{seq}",
                client_message_id=None,
                role="user",
                content=f"row {seq}",
                source_turn_id="turn-tie",
                sequence=seq,
                remote_agent_id="default",
                created_at=tied_ts,
            )

        seen: list[str] = []
        cursor = None
        while True:
            page, cursor, has_more = await store.get_app_messages_after_cursor(
                cursor=cursor, limit=2, remote_agent_id="default",
            )
            seen.extend(m["id"] for m in page)
            if not has_more:
                break

        assert seen == [f"app_rm_row_{seq}" for seq in range(5)]
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
        "lastSeenAt": _fresh_last_seen(),
        "expiresAt": None,
        "callbackUrl": None,
        "sourceLabel": None,
    }
    ids = reg.known_remote_agent_ids()
    assert "cc-1" in ids


# ================================================================
# Stale-TTL detection tests
# ================================================================


def _stale_session_dict(remote_agent_id="stale-1", status="online"):
    """Build a session dict whose lastSeenAt is far past the stale TTL."""
    from runtime.app_agent_session_registry import AppAgentSessionRegistry

    reg = AppAgentSessionRegistry()
    reg._sessions["stale-s1"] = {
        "sessionId": "stale-s1",
        "remoteAgentId": remote_agent_id,
        "displayName": "Stale",
        "description": "desc",
        "kind": "claude_channel",
        "status": status,
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": "2026-01-01T00:00:00Z",  # far past the 90s TTL
        "expiresAt": None,
        "callbackUrl": None,
        "sourceLabel": None,
    }
    return reg


@pytest.mark.asyncio
async def test_stale_session_list_shows_offline():
    """list_all reports a stale (lastSeenAt > TTL) session as offline."""
    reg = _stale_session_dict()
    sessions = await reg.list_all()
    assert len(sessions) == 1
    assert sessions[0]["status"] == "offline"


@pytest.mark.asyncio
async def test_stale_session_get_shows_offline():
    """get reports a stale session as offline."""
    reg = _stale_session_dict()
    sess = await reg.get("stale-s1")
    assert sess is not None
    assert sess["status"] == "offline"


@pytest.mark.asyncio
async def test_stale_session_excluded_from_list_online():
    """A stale session is not returned by list_online."""
    reg = _stale_session_dict()
    online = await reg.list_online()
    assert online == []


def test_stale_session_find_by_remote_agent_id_shows_offline():
    """find_by_remote_agent_id reports a stale session as offline."""
    reg = _stale_session_dict(remote_agent_id="stale-rid")
    sess = reg.find_by_remote_agent_id("stale-rid")
    assert sess is not None
    assert sess["status"] == "offline"


def test_stale_session_still_known_not_unknown():
    """A stale session remains a known remoteAgentId (no 400 unknown)."""
    from main import resolve_app_remote_agent, app_agent_session_registry

    app_agent_session_registry._sessions.clear()
    app_agent_session_registry._sessions["stale-s1"] = {
        "sessionId": "stale-s1",
        "remoteAgentId": "stale-known",
        "displayName": "Stale",
        "description": "desc",
        "kind": "claude_channel",
        "status": "online",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": "2026-01-01T00:00:00Z",
        "expiresAt": None,
        "callbackUrl": None,
        "sourceLabel": None,
    }
    # Known → resolver does not raise 400; status resolves as offline.
    resolved = resolve_app_remote_agent("stale-known")
    assert resolved["target"] == "claude_channel"
    assert resolved["status"] == "offline"


@pytest.mark.asyncio
async def test_heartbeat_keeps_session_online_within_ttl():
    """A fresh heartbeat keeps a session online across reads."""
    from runtime.app_agent_session_registry import AppAgentSessionRegistry

    reg = AppAgentSessionRegistry()
    await reg.register(
        session_id="s1",
        remote_agent_id="s1",
        display_name="S1",
        kind="claude_channel",
    )
    await reg.heartbeat("s1")
    sess = await reg.get("s1")
    assert sess["status"] == "online"
    listed = await reg.list_online()
    assert len(listed) == 1


@pytest.mark.asyncio
async def test_register_initial_status_online():
    """A freshly registered session is online (not stale)."""
    from runtime.app_agent_session_registry import AppAgentSessionRegistry

    reg = AppAgentSessionRegistry()
    await reg.register(
        session_id="s1",
        remote_agent_id="s1",
        display_name="S1",
        kind="claude_channel",
    )
    sess = await reg.get("s1")
    assert sess["status"] == "online"


@pytest.mark.asyncio
async def test_stale_session_does_not_mutate_stored_status():
    """Effective-status is computed on read; stored status is left intact.

    This keeps the registry's lazy semantics non-destructive: a heartbeat
    arriving later can flip a stale session back online without an
    explicit status write.
    """
    reg = _stale_session_dict()
    await reg.get("stale-s1")  # triggers effective-status computation
    stored = reg._sessions["stale-s1"]
    assert stored["status"] == "online"  # storage unchanged
    assert stored["lastSeenAt"] == "2026-01-01T00:00:00Z"


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
        "lastSeenAt": _fresh_last_seen(),
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
        "lastSeenAt": _fresh_last_seen(),
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
        "lastSeenAt": _fresh_last_seen(),
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
        "lastSeenAt": _fresh_last_seen(),
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
        "lastSeenAt": _fresh_last_seen(),
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
        "lastSeenAt": _fresh_last_seen(),
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
        "lastSeenAt": _fresh_last_seen(),
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
        "lastSeenAt": _fresh_last_seen(),
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
        "lastSeenAt": _fresh_last_seen(),
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
        "lastSeenAt": _fresh_last_seen(),
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
        "lastSeenAt": _fresh_last_seen(),
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
        "lastSeenAt": _fresh_last_seen(),
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
        "lastSeenAt": _fresh_last_seen(),
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
        "lastSeenAt": _fresh_last_seen(),
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
        "lastSeenAt": _fresh_last_seen(),
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
        "lastSeenAt": _fresh_last_seen(),
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
        "lastSeenAt": _fresh_last_seen(),
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
        "lastSeenAt": _fresh_last_seen(),
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
        "lastSeenAt": _fresh_last_seen(),
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
        "lastSeenAt": _fresh_last_seen(),
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
        "lastSeenAt": _fresh_last_seen(),
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
        "lastSeenAt": _fresh_last_seen(),
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
        "lastSeenAt": _fresh_last_seen(),
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
        "lastSeenAt": _fresh_last_seen(),
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
        "lastSeenAt": _fresh_last_seen(),
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
        "lastSeenAt": _fresh_last_seen(),
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


# ================================================================
# Attached-session reply id isolation & atomic persistence
# ================================================================


def _make_attached_session(rid, callback_url="http://127.0.0.1:19010"):
    """Helper: build a session dict for app_agent_session_registry."""
    return {
        "sessionId": f"s-{rid}",
        "remoteAgentId": rid,
        "displayName": rid,
        "description": "desc",
        "status": "online",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": _fresh_last_seen(),
        "expiresAt": None,
        "callbackUrl": callback_url,
        "sourceLabel": None,
    }


def _patch_httpx_callback(monkeypatch, json_return_value):
    """Patch httpx.AsyncClient to return *json_return_value* with status 200."""
    import httpx
    from unittest.mock import AsyncMock, MagicMock

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = json_return_value

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


@pytest.mark.asyncio
async def test_callback_two_sessions_same_session_remote_id_both_succeed(monkeypatch):
    """Two different remoteAgentId callbacks both returning remoteId='msg-1' both succeed.

    The Router generates its own app_messages.id for each reply, so the
    same session-side remoteId from different sessions must not collide.
    """
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid1 = f"cc-a-{_uuid.uuid4().hex[:8]}"
    rid2 = f"cc-b-{_uuid.uuid4().hex[:8]}"
    app_agent_session_registry._sessions[f"s-{rid1}"] = _make_attached_session(rid1)
    app_agent_session_registry._sessions[f"s-{rid2}"] = _make_attached_session(rid2, "http://127.0.0.1:19011")

    _patch_httpx_callback(monkeypatch, {
        "agentReplies": [{"content": "reply", "remoteId": "msg-1"}],
    })

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        def __init__(self, cid, rid):
            self._cid = cid
            self._rid = rid

        async def json(self):
            return {"clientMessageId": self._cid, "content": "hi", "remoteAgentId": self._rid}

    from main import api_send_message

    result1 = await api_send_message(FakeRequest(f"cm-a-{_uuid.uuid4().hex[:8]}", rid1))
    result2 = await api_send_message(FakeRequest(f"cm-b-{_uuid.uuid4().hex[:8]}", rid2))

    assert "userMessage" in result1
    assert "userMessage" in result2
    # Both replies have Router-generated ids (not "msg-1")
    assert result1["agentReplies"][0]["remoteId"].startswith("app_rm_agent_")
    assert result2["agentReplies"][0]["remoteId"].startswith("app_rm_agent_")
    assert result1["agentReplies"][0]["remoteId"] != result2["agentReplies"][0]["remoteId"]
    # session remoteId preserved in sourceToolCallId
    assert result1["agentReplies"][0]["sourceToolCallId"] == "msg-1"
    assert result2["agentReplies"][0]["sourceToolCallId"] == "msg-1"


@pytest.mark.asyncio
async def test_callback_stored_ids_are_router_generated_and_distinct(monkeypatch):
    """Stored app_messages ids are Router-generated and distinct across replies."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg, memory_store

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"cc-dist-{_uuid.uuid4().hex[:8]}"
    app_agent_session_registry._sessions[f"s-{rid}"] = _make_attached_session(rid)

    _patch_httpx_callback(monkeypatch, {
        "agentReplies": [
            {"content": "reply 1", "remoteId": "session-rm-1"},
            {"content": "reply 2", "remoteId": "session-rm-2"},
            {"content": "reply 3"},
        ],
    })

    cid = f"cm-dist-{_uuid.uuid4().hex[:8]}"

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {"clientMessageId": cid, "content": "hi", "remoteAgentId": rid}

    from main import api_send_message

    result = await api_send_message(FakeRequest())
    assert len(result["agentReplies"]) == 3

    # All returned remoteIds are Router-generated
    for r in result["agentReplies"]:
        assert r["remoteId"].startswith("app_rm_agent_")
    # Distinct
    ids = [r["remoteId"] for r in result["agentReplies"]]
    assert len(set(ids)) == 3

    # Verify storage: user message + 3 agent replies, all with Router ids
    msgs, _, _ = await memory_store.get_app_messages_after_cursor(remote_agent_id=rid)
    assert len(msgs) == 4  # 1 user + 3 agent
    agent_rows = [m for m in msgs if m["role"] == "agent"]
    assert len(agent_rows) == 3
    for m in agent_rows:
        assert m["id"].startswith("app_rm_agent_")
    stored_ids = [m["id"] for m in agent_rows]
    assert len(set(stored_ids)) == 3
    # None of the stored ids equal the session remoteIds
    assert "session-rm-1" not in stored_ids
    assert "session-rm-2" not in stored_ids


@pytest.mark.asyncio
async def test_callback_source_tool_call_id_stores_session_remote_id(monkeypatch):
    """source_tool_call_id stores the session callback remoteId for tracing."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg, memory_store

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"cc-stc-{_uuid.uuid4().hex[:8]}"
    app_agent_session_registry._sessions[f"s-{rid}"] = _make_attached_session(rid)

    _patch_httpx_callback(monkeypatch, {
        "agentReplies": [
            {"content": "with id", "remoteId": "session-xyz"},
            {"content": "without id"},
        ],
    })

    cid = f"cm-stc-{_uuid.uuid4().hex[:8]}"

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {"clientMessageId": cid, "content": "hi", "remoteAgentId": rid}

    from main import api_send_message

    result = await api_send_message(FakeRequest())
    assert result["agentReplies"][0]["sourceToolCallId"] == "session-xyz"
    assert result["agentReplies"][1]["sourceToolCallId"] is None

    # Verify DB persistence
    found = await memory_store.find_app_message_by_client_message_id(cid, remote_agent_id=rid)
    assert found is not None
    turn_id = found["source_turn_id"]
    replies = await memory_store.find_app_agent_replies_by_turn_id(turn_id, remote_agent_id=rid)
    assert len(replies) == 2
    assert replies[0]["source_tool_call_id"] == "session-xyz"
    assert replies[1]["source_tool_call_id"] == ""


@pytest.mark.asyncio
async def test_callback_idempotent_second_post_returns_router_remote_id(monkeypatch):
    """Idempotent second POST returns stored Router remoteId, not session remoteId.

    The callback is NOT called a second time.  The returned remoteId is
    the Router-generated id from the first call; sourceToolCallId still
    carries the session remoteId.
    """
    import uuid as _uuid
    import httpx
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"cc-idem2-{_uuid.uuid4().hex[:8]}"
    app_agent_session_registry._sessions[f"s-{rid}"] = _make_attached_session(rid)

    cid = f"cm-idem2-{_uuid.uuid4().hex[:8]}"
    call_count = 0

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "agentReplies": [{"content": "first reply", "remoteId": "session-abc"}],
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
    assert call_count == 1
    first_remote_id = result1["agentReplies"][0]["remoteId"]
    assert first_remote_id.startswith("app_rm_agent_")
    assert first_remote_id != "session-abc"
    assert result1["agentReplies"][0]["sourceToolCallId"] == "session-abc"

    # Second call — same cid + rid → idempotent, no callback
    result2 = await api_send_message(FakeRequest(cid, "hello", rid))
    assert call_count == 1  # callback NOT called again
    second_remote_id = result2["agentReplies"][0]["remoteId"]
    assert second_remote_id == first_remote_id  # stored Router remoteId
    assert second_remote_id != "session-abc"
    assert result2["agentReplies"][0]["sourceToolCallId"] == "session-abc"
    assert result2["agentReplies"][0]["content"] == "first reply"


@pytest.mark.asyncio
async def test_insert_app_turn_messages_rolls_back_on_reply_failure():
    """If any reply insert fails, user message is also rolled back."""
    from memory.store import MemoryStore

    store = MemoryStore(db_path=":memory:")
    try:
        # Two replies share the same id → second insert raises
        # PRIMARY KEY conflict; the entire transaction must roll back.
        with pytest.raises(Exception):
            await store.insert_app_turn_messages(
                user_message={
                    "remote_id": "app_rm_user_rollback",
                    "client_message_id": "cm-rollback",
                    "content": "hello",
                    "source_turn_id": "turn-rollback",
                    "remote_agent_id": "default",
                },
                agent_replies=[
                    {
                        "remote_id": "app_rm_agent_dup",
                        "content": "reply 1",
                        "source_turn_id": "turn-rollback",
                        "sequence": 1,
                        "remote_agent_id": "default",
                    },
                    {
                        "remote_id": "app_rm_agent_dup",  # duplicate → conflict
                        "content": "reply 2",
                        "source_turn_id": "turn-rollback",
                        "sequence": 2,
                        "remote_agent_id": "default",
                    },
                ],
            )
        # User message must NOT be persisted
        found = await store.find_app_message_by_client_message_id("cm-rollback")
        assert found is None
        # No replies for this turn
        replies = await store.find_app_agent_replies_by_turn_id("turn-rollback")
        assert replies == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_insert_app_turn_messages_persists_user_and_replies_atomically():
    """Successful insert_app_turn_messages writes user + replies together."""
    from memory.store import MemoryStore

    store = MemoryStore(db_path=":memory:")
    try:
        await store.insert_app_turn_messages(
            user_message={
                "remote_id": "app_rm_user_ok",
                "client_message_id": "cm-ok",
                "content": "hello",
                "source_turn_id": "turn-ok",
                "remote_agent_id": "default",
                "created_at": 1718539200.0,
            },
            agent_replies=[
                {
                    "remote_id": "app_rm_agent_1",
                    "content": "reply 1",
                    "source_turn_id": "turn-ok",
                    "sequence": 1,
                    "remote_agent_id": "default",
                    "source_tool_call_id": "session-rm-1",
                    "created_at": 1718539202.0,
                },
                {
                    "remote_id": "app_rm_agent_2",
                    "content": "reply 2",
                    "source_turn_id": "turn-ok",
                    "sequence": 2,
                    "remote_agent_id": "default",
                },
            ],
        )
        found = await store.find_app_message_by_client_message_id("cm-ok")
        assert found is not None
        assert found["content"] == "hello"
        assert found["created_at"] == 1718539200.0

        replies = await store.find_app_agent_replies_by_turn_id("turn-ok")
        assert len(replies) == 2
        assert replies[0]["source_tool_call_id"] == "session-rm-1"
        assert replies[0]["created_at"] == 1718539202.0
        assert replies[1]["source_tool_call_id"] == ""
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_callback_persists_callback_created_at(monkeypatch):
    """Callback reply createdAt is persisted (not dropped)."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg, memory_store

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"cc-cts-{_uuid.uuid4().hex[:8]}"
    app_agent_session_registry._sessions[f"s-{rid}"] = _make_attached_session(rid)

    _patch_httpx_callback(monkeypatch, {
        "agentReplies": [
            {"content": "timed", "createdAt": "2026-06-19T12:00:02Z"},
        ],
    })

    cid = f"cm-cts-{_uuid.uuid4().hex[:8]}"

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {"clientMessageId": cid, "content": "hi", "remoteAgentId": rid}

    from main import api_send_message

    result = await api_send_message(FakeRequest())
    assert result["agentReplies"][0]["createdAt"] == "2026-06-19T12:00:02Z"

    # Verify DB has the parsed timestamp
    found = await memory_store.find_app_message_by_client_message_id(cid, remote_agent_id=rid)
    assert found is not None
    replies = await memory_store.find_app_agent_replies_by_turn_id(
        found["source_turn_id"], remote_agent_id=rid,
    )
    assert len(replies) == 1
    # 2026-06-19T12:00:02Z parsed as Unix timestamp
    from datetime import datetime as _dt, timezone as _tz
    expected_ts = _dt.fromisoformat("2026-06-19T12:00:02+00:00").timestamp()
    assert replies[0]["created_at"] == expected_ts


@pytest.mark.asyncio
async def test_default_internal_path_unaffected_by_callback_changes(monkeypatch):
    """Default internal agent path still works and uses insert_app_message."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {
                "clientMessageId": "cm-default-unaffected",
                "content": "hello default",
            }

    from main import api_send_message

    result = await api_send_message(FakeRequest())
    assert "userMessage" in result
    assert result["userMessage"]["remoteId"].startswith("app_rm_user_")
    assert result["userMessage"]["status"] == "sent"


# ================================================================
# attached session callbackUrl localhost validation (SSRF)
# ================================================================


def test_validate_callback_url_accepts_127_ipv4():
    from main import validate_attached_session_callback_url

    assert validate_attached_session_callback_url("http://127.0.0.1:9010") == "http://127.0.0.1:9010"


def test_validate_callback_url_accepts_localhost():
    from main import validate_attached_session_callback_url

    assert validate_attached_session_callback_url("http://localhost:9010") == "http://localhost:9010"


def test_validate_callback_url_strips_trailing_slash():
    from main import validate_attached_session_callback_url

    assert validate_attached_session_callback_url("http://localhost:9010/") == "http://localhost:9010"
    assert validate_attached_session_callback_url("http://127.0.0.1:9010/path") == "http://127.0.0.1:9010"


def test_validate_callback_url_accepts_ipv6_loopback():
    from main import validate_attached_session_callback_url

    # Bracketed IPv6 loopback
    assert validate_attached_session_callback_url("http://[::1]:9010") == "http://[::1]:9010"
    # Without port
    assert validate_attached_session_callback_url("http://[::1]") == "http://[::1]"


def test_validate_callback_url_empty_returns_empty():
    from main import validate_attached_session_callback_url

    assert validate_attached_session_callback_url("") == ""
    assert validate_attached_session_callback_url("   ") == ""
    assert validate_attached_session_callback_url(None) == ""


def test_validate_callback_url_rejects_0000():
    from main import validate_attached_session_callback_url

    with pytest.raises(ValueError):
        validate_attached_session_callback_url("http://0.0.0.0:9010")


def test_validate_callback_url_rejects_private_lan_192():
    from main import validate_attached_session_callback_url

    with pytest.raises(ValueError):
        validate_attached_session_callback_url("http://192.168.1.10:9010")


def test_validate_callback_url_rejects_private_10():
    from main import validate_attached_session_callback_url

    with pytest.raises(ValueError):
        validate_attached_session_callback_url("http://10.0.0.1:9010")


def test_validate_callback_url_rejects_private_172():
    from main import validate_attached_session_callback_url

    with pytest.raises(ValueError):
        validate_attached_session_callback_url("http://172.16.0.1:9010")


def test_validate_callback_url_rejects_public_hostname():
    from main import validate_attached_session_callback_url

    with pytest.raises(ValueError):
        validate_attached_session_callback_url("http://example.com")


def test_validate_callback_url_rejects_file_scheme():
    from main import validate_attached_session_callback_url

    with pytest.raises(ValueError):
        validate_attached_session_callback_url("file:///etc/passwd")


def test_validate_callback_url_rejects_javascript_scheme():
    from main import validate_attached_session_callback_url

    with pytest.raises(ValueError):
        validate_attached_session_callback_url("javascript:alert(1)")


def test_validate_callback_url_rejects_https():
    """https is intentionally not allowed for local dev callbacks."""
    from main import validate_attached_session_callback_url

    with pytest.raises(ValueError):
        validate_attached_session_callback_url("https://127.0.0.1:9010")


def test_validate_callback_url_rejects_credentials():
    from main import validate_attached_session_callback_url

    with pytest.raises(ValueError):
        validate_attached_session_callback_url("http://user:pass@127.0.0.1:9010")


def test_validate_callback_url_rejects_no_hostname():
    from main import validate_attached_session_callback_url

    with pytest.raises(ValueError):
        validate_attached_session_callback_url("http:///path")


@pytest.mark.asyncio
async def test_register_unauthorized_returns_401_before_callback_validation(monkeypatch):
    """401 returned before callbackUrl validation — no info leak."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    class FakeRequest:
        headers = {}
        base_url = "http://testserver/"

        async def json(self):
            # Even with an obviously invalid callbackUrl, auth runs first.
            return {
                "sessionId": "s1",
                "callbackUrl": "http://evil.example.com",
            }

    from main import api_register_app_agent_session

    with pytest.raises(HTTPException) as exc:
        await api_register_app_agent_session(FakeRequest())
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_register_with_invalid_callback_url_returns_400_when_authorized(monkeypatch):
    """Authorized register with invalid callbackUrl → 400."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {
                "sessionId": "s-bad-cb",
                "remoteAgentId": "s-bad-cb",
                "callbackUrl": "http://192.168.1.10:9010",
            }

    from main import api_register_app_agent_session

    with pytest.raises(HTTPException) as exc:
        await api_register_app_agent_session(FakeRequest())
    assert exc.value.status_code == 400
    assert "callbackUrl" in exc.value.detail
    # Session must not be registered
    assert await app_agent_session_registry.list_all() == []


@pytest.mark.asyncio
async def test_register_with_valid_localhost_callback_url_succeeds(monkeypatch):
    """Authorized register with valid localhost callbackUrl succeeds."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {
                "sessionId": "s-good-cb",
                "remoteAgentId": "s-good-cb",
                "callbackUrl": "http://127.0.0.1:9010/",
            }

    from main import api_register_app_agent_session

    result = await api_register_app_agent_session(FakeRequest())
    # Normalised: trailing slash stripped
    assert result["session"]["callbackUrl"] == "http://127.0.0.1:9010"


@pytest.mark.asyncio
async def test_register_with_no_callback_url_succeeds(monkeypatch):
    """Authorized register with empty callbackUrl succeeds (501 on send)."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {
                "sessionId": "s-no-cb",
                "remoteAgentId": "s-no-cb",
            }

    from main import api_register_app_agent_session

    result = await api_register_app_agent_session(FakeRequest())
    assert result["session"]["callbackUrl"] is None


# ================================================================
# Claude channel — register kind=claude_channel
# ================================================================


@pytest.mark.asyncio
async def test_register_kind_claude_channel_succeeds_without_callback(monkeypatch):
    """register with kind=claude_channel succeeds without callbackUrl."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    sid = f"chan-{_uuid.uuid4().hex[:8]}"

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {
                "sessionId": sid,
                "remoteAgentId": sid,
                "displayName": "Claude Channel",
                "kind": "claude_channel",
            }

    from main import api_register_app_agent_session

    result = await api_register_app_agent_session(FakeRequest())
    s = result["session"]
    assert s["kind"] == "claude_channel"
    assert s["callbackUrl"] is None
    assert s["status"] == "online"


@pytest.mark.asyncio
async def test_register_default_kind_is_attached_session(monkeypatch):
    """register without kind defaults to attached_session."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    sid = f"att-{_uuid.uuid4().hex[:8]}"

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {"sessionId": sid, "remoteAgentId": sid}

    from main import api_register_app_agent_session

    result = await api_register_app_agent_session(FakeRequest())
    assert result["session"]["kind"] == "attached_session"


@pytest.mark.asyncio
async def test_register_unknown_kind_falls_back_to_attached_session(monkeypatch):
    """register with unknown kind falls back to attached_session."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    sid = f"unk-{_uuid.uuid4().hex[:8]}"

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {"sessionId": sid, "remoteAgentId": sid, "kind": "unknown_kind"}

    from main import api_register_app_agent_session

    result = await api_register_app_agent_session(FakeRequest())
    assert result["session"]["kind"] == "attached_session"


# ================================================================
# Claude channel — contact-invitations agentType
# ================================================================


@pytest.mark.asyncio
async def test_contact_invitations_claude_channel_shows_correct_agent_type(monkeypatch):
    """Online claude_channel session shows agentType=claude-code-channel."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import cfg, app_agent_session_registry, api_contact_invitations

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    sid = f"chan-{_uuid.uuid4().hex[:8]}"
    await app_agent_session_registry.register(
        session_id=sid,
        remote_agent_id=sid,
        display_name="Claude Channel",
        description="Channel session",
        kind="claude_channel",
    )

    result = await api_contact_invitations(
        _FakeAppRequest(
            headers={"authorization": "Bearer secret"},
            base_url="http://testserver/",
        )
    )

    chan_invs = [i for i in result["invitations"] if i["agentType"] == "claude-code-channel"]
    assert len(chan_invs) == 1
    assert chan_invs[0]["remoteAgentId"] == sid
    assert chan_invs[0]["displayName"] == "Claude Channel"


@pytest.mark.asyncio
async def test_contact_invitations_attached_session_still_shows_attached_session(monkeypatch):
    """attached_session kind still shows agentType=attached-session."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import cfg, app_agent_session_registry, api_contact_invitations

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    sid = f"att-{_uuid.uuid4().hex[:8]}"
    await app_agent_session_registry.register(
        session_id=sid,
        remote_agent_id=sid,
        display_name="Attached",
        callback_url="http://127.0.0.1:9010",
    )

    result = await api_contact_invitations(
        _FakeAppRequest(
            headers={"authorization": "Bearer secret"},
            base_url="http://testserver/",
        )
    )

    att_invs = [i for i in result["invitations"] if i["agentType"] == "attached-session"]
    assert len(att_invs) == 1
    assert att_invs[0]["remoteAgentId"] == sid


# ================================================================
# Claude channel — resolve_app_remote_agent
# ================================================================


def test_resolve_claude_channel_returns_correct_target():
    """resolve_app_remote_agent returns target=claude_channel for channel sessions."""
    from main import resolve_app_remote_agent, app_agent_session_registry

    app_agent_session_registry._sessions.clear()
    app_agent_session_registry._sessions["s1"] = {
        "sessionId": "s1",
        "remoteAgentId": "chan-resolve",
        "displayName": "Chan",
        "description": "desc",
        "kind": "claude_channel",
        "status": "online",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": _fresh_last_seen(),
        "expiresAt": None,
        "callbackUrl": None,
        "sourceLabel": None,
    }

    resolved = resolve_app_remote_agent("chan-resolve")
    assert resolved["target"] == "claude_channel"
    assert resolved["kind"] == "claude_channel"
    assert resolved["sessionId"] == "s1"


# ================================================================
# Claude channel — POST /api/messages inbox path
# ================================================================


def _make_claude_channel_session(rid, sid=None, status="online"):
    """Helper: build a claude_channel session dict for the registry."""
    return {
        "sessionId": sid or f"s-{rid}",
        "remoteAgentId": rid,
        "displayName": rid,
        "description": "claude channel",
        "kind": "claude_channel",
        "status": status,
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": _fresh_last_seen(),
        "expiresAt": None,
        "callbackUrl": None,
        "sourceLabel": None,
    }


def _make_stale_claude_channel_session(rid, sid=None):
    """Helper: a claude_channel session whose lastSeenAt is past the TTL.

    Stored status is still ``"online"`` — the stale-TTL detection is what
    flips the effective status to offline on read.
    """
    sess = _make_claude_channel_session(rid, sid=sid, status="online")
    sess["lastSeenAt"] = "2026-01-01T00:00:00Z"
    return sess


@pytest.mark.asyncio
async def test_post_messages_claude_channel_returns_sent_and_empty_replies(monkeypatch):
    """POST to claude_channel returns userMessage.status=sent, agentReplies=[]."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg, channel_pending_inbox

    app_agent_session_registry._sessions.clear()
    channel_pending_inbox._messages.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"chan-{_uuid.uuid4().hex[:8]}"
    app_agent_session_registry._sessions[f"s-{rid}"] = _make_claude_channel_session(rid)

    cid = f"cm-chan-{_uuid.uuid4().hex[:8]}"

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {"clientMessageId": cid, "content": "hi", "remoteAgentId": rid}

    from main import api_send_message

    result = await api_send_message(FakeRequest())
    assert result["userMessage"]["status"] == "sent"
    assert result["userMessage"]["clientMessageId"] == cid
    assert result["agentReplies"] == []


@pytest.mark.asyncio
async def test_post_messages_stale_claude_channel_returns_503_no_pending(monkeypatch):
    """POST to a stale claude_channel returns 503 and writes no pending delivery."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg, channel_pending_inbox, memory_store

    app_agent_session_registry._sessions.clear()
    channel_pending_inbox._messages.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"chan-stale-{_uuid.uuid4().hex[:8]}"
    sid = f"s-{rid}"
    # Stored status online but lastSeenAt far past the TTL → effective offline.
    app_agent_session_registry._sessions[sid] = _make_stale_claude_channel_session(rid, sid)

    cid = f"cm-chan-stale-{_uuid.uuid4().hex[:8]}"

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {"clientMessageId": cid, "content": "hi", "remoteAgentId": rid}

    from main import api_send_message

    with pytest.raises(HTTPException) as exc:
        await api_send_message(FakeRequest())
    assert exc.value.status_code == 503

    # No user message persisted, no pending delivery enqueued.
    found = await memory_store.find_app_message_by_client_message_id(cid, remote_agent_id=rid)
    assert found is None
    pending = await channel_pending_inbox.list_pending(sid, limit=10)
    assert pending == []


@pytest.mark.asyncio
async def test_post_messages_claude_channel_retry_after_stale_returns_existing(monkeypatch):
    """A retried clientMessageId accepted while online still returns its
    existing result after the channel goes stale — idempotency beats 503."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg, channel_pending_inbox

    app_agent_session_registry._sessions.clear()
    channel_pending_inbox._messages.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"chan-retry-{_uuid.uuid4().hex[:8]}"
    sid = f"s-{rid}"
    # First accepted while online (fresh lastSeenAt).
    app_agent_session_registry._sessions[sid] = _make_claude_channel_session(rid, sid)

    cid = f"cm-chan-retry-{_uuid.uuid4().hex[:8]}"

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {"clientMessageId": cid, "content": "hi", "remoteAgentId": rid}

    from main import api_send_message

    first = await api_send_message(FakeRequest())
    assert first["userMessage"]["status"] == "sent"

    # Channel goes stale (lastSeenAt ages past TTL).
    app_agent_session_registry._sessions[sid]["lastSeenAt"] = "2026-01-01T00:00:00Z"

    second = await api_send_message(FakeRequest())
    assert second["userMessage"]["status"] == "sent"
    assert second["userMessage"]["clientMessageId"] == cid


@pytest.mark.asyncio
async def test_get_messages_stale_claude_channel_still_reads_history(monkeypatch):
    """GET /api/messages still reads history for a stale/offline channel."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg, memory_store

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"chan-hist-stale-{_uuid.uuid4().hex[:8]}"
    sid = f"s-{rid}"
    app_agent_session_registry._sessions[sid] = _make_stale_claude_channel_session(rid, sid)

    # Seed some history under this remote_agent_id.
    await memory_store.insert_app_message(
        remote_id=f"rm-{_uuid.uuid4().hex[:8]}",
        client_message_id=f"cm-seed-{_uuid.uuid4().hex[:8]}",
        role="user",
        content="past msg",
        source_turn_id="turn-seed",
        remote_agent_id=rid,
    )

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

    from main import api_get_messages

    result = await api_get_messages(FakeRequest(), remoteAgentId=rid)
    assert "messages" in result
    contents = [m.get("content") for m in result["messages"]]
    assert "past msg" in contents


@pytest.mark.asyncio
async def test_post_messages_claude_channel_writes_user_app_message(monkeypatch):
    """POST to claude_channel persists the user app_message."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg, memory_store, channel_pending_inbox

    app_agent_session_registry._sessions.clear()
    channel_pending_inbox._messages.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"chan-wm-{_uuid.uuid4().hex[:8]}"
    app_agent_session_registry._sessions[f"s-{rid}"] = _make_claude_channel_session(rid)

    cid = f"cm-chan-wm-{_uuid.uuid4().hex[:8]}"

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {"clientMessageId": cid, "content": "hello chan", "remoteAgentId": rid}

    from main import api_send_message

    await api_send_message(FakeRequest())

    found = await memory_store.find_app_message_by_client_message_id(cid, remote_agent_id=rid)
    assert found is not None
    assert found["role"] == "user"
    assert found["content"] == "hello chan"
    assert found["remote_agent_id"] == rid


@pytest.mark.asyncio
async def test_post_messages_claude_channel_creates_pending_delivery(monkeypatch):
    """POST to claude_channel creates one pending delivery in the inbox."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg, channel_pending_inbox

    app_agent_session_registry._sessions.clear()
    channel_pending_inbox._messages.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"chan-pd-{_uuid.uuid4().hex[:8]}"
    sid = f"s-{rid}"
    app_agent_session_registry._sessions[sid] = _make_claude_channel_session(rid, sid)

    cid = f"cm-pd-{_uuid.uuid4().hex[:8]}"

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {"clientMessageId": cid, "content": "queue me", "remoteAgentId": rid}

    from main import api_send_message

    await api_send_message(FakeRequest())

    pending = await channel_pending_inbox.list_pending(sid)
    assert len(pending) == 1
    assert pending[0]["clientMessageId"] == cid
    assert pending[0]["content"] == "queue me"
    assert pending[0]["remoteAgentId"] == rid
    assert pending[0]["sessionId"] == sid
    assert pending[0]["deliveryId"]


@pytest.mark.asyncio
async def test_post_messages_claude_channel_duplicate_does_not_create_duplicate_pending(monkeypatch):
    """Duplicate POST with same remoteAgentId + clientMessageId doesn't duplicate pending."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg, channel_pending_inbox

    app_agent_session_registry._sessions.clear()
    channel_pending_inbox._messages.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"chan-dup-{_uuid.uuid4().hex[:8]}"
    sid = f"s-{rid}"
    app_agent_session_registry._sessions[sid] = _make_claude_channel_session(rid, sid)

    cid = f"cm-dup-{_uuid.uuid4().hex[:8]}"

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {"clientMessageId": cid, "content": "once", "remoteAgentId": rid}

    from main import api_send_message

    result1 = await api_send_message(FakeRequest())
    result2 = await api_send_message(FakeRequest())

    # Both return the same userMessage
    assert result1["userMessage"]["remoteId"] == result2["userMessage"]["remoteId"]
    assert result2["agentReplies"] == []

    pending = await channel_pending_inbox.list_pending(sid)
    assert len(pending) == 1


# ================================================================
# Claude channel — poll pending messages
# ================================================================


@pytest.mark.asyncio
async def test_poll_pending_returns_message_for_correct_session(monkeypatch):
    """poll returns pending messages only for the requested session."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg, channel_pending_inbox

    app_agent_session_registry._sessions.clear()
    channel_pending_inbox._messages.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid1 = f"chan-poll1-{_uuid.uuid4().hex[:8]}"
    rid2 = f"chan-poll2-{_uuid.uuid4().hex[:8]}"
    sid1 = f"s-{rid1}"
    sid2 = f"s-{rid2}"
    app_agent_session_registry._sessions[sid1] = _make_claude_channel_session(rid1, sid1)
    app_agent_session_registry._sessions[sid2] = _make_claude_channel_session(rid2, sid2)

    await channel_pending_inbox.add(
        remote_agent_id=rid1, session_id=sid1,
        client_message_id="cm-1", content="msg1", created_at="2026-06-20T00:00:00Z",
    )
    await channel_pending_inbox.add(
        remote_agent_id=rid2, session_id=sid2,
        client_message_id="cm-2", content="msg2", created_at="2026-06-20T00:00:00Z",
    )

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

    from main import api_channel_poll_pending

    result1 = await api_channel_poll_pending(sid1, FakeRequest())
    assert len(result1["messages"]) == 1
    assert result1["messages"][0]["clientMessageId"] == "cm-1"
    assert result1["messages"][0]["content"] == "msg1"
    assert result1["messages"][0]["sessionId"] == sid1
    assert result1["messages"][0]["deliveryId"]

    result2 = await api_channel_poll_pending(sid2, FakeRequest())
    assert len(result2["messages"]) == 1
    assert result2["messages"][0]["clientMessageId"] == "cm-2"


@pytest.mark.asyncio
async def test_poll_pending_unknown_session_returns_404(monkeypatch):
    """poll on unknown session returns 404."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

    from main import api_channel_poll_pending

    with pytest.raises(HTTPException) as exc:
        await api_channel_poll_pending("nonexistent", FakeRequest())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_poll_pending_non_channel_session_returns_400(monkeypatch):
    """poll on attached_session returns 400."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    sid = f"att-{_uuid.uuid4().hex[:8]}"
    app_agent_session_registry._sessions[sid] = {
        "sessionId": sid,
        "remoteAgentId": sid,
        "displayName": "Attached",
        "description": "desc",
        "kind": "attached_session",
        "status": "online",
        "createdAt": "2026-01-01T00:00:00Z",
        "lastSeenAt": _fresh_last_seen(),
        "expiresAt": None,
        "callbackUrl": "http://127.0.0.1:9010",
        "sourceLabel": None,
    }

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

    from main import api_channel_poll_pending

    with pytest.raises(HTTPException) as exc:
        await api_channel_poll_pending(sid, FakeRequest())
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_poll_pending_requires_auth(monkeypatch):
    """poll requires auth before session validation."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    class FakeRequest:
        headers = {}
        base_url = "http://testserver/"

    from main import _require_chat_auth, _api_config

    api_cfg = _api_config()
    with pytest.raises(HTTPException) as exc:
        _require_chat_auth(FakeRequest(), api_cfg)
    assert exc.value.status_code == 401


# ================================================================
# Claude channel — ack pending messages
# ================================================================


@pytest.mark.asyncio
async def test_ack_suppresses_pending_from_later_polls(monkeypatch):
    """ack removes the message from later polls."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg, channel_pending_inbox

    app_agent_session_registry._sessions.clear()
    channel_pending_inbox._messages.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"chan-ack-{_uuid.uuid4().hex[:8]}"
    sid = f"s-{rid}"
    app_agent_session_registry._sessions[sid] = _make_claude_channel_session(rid, sid)

    msg = await channel_pending_inbox.add(
        remote_agent_id=rid, session_id=sid,
        client_message_id="cm-ack", content="ack me", created_at="2026-06-20T00:00:00Z",
    )
    delivery_id = msg["deliveryId"]

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

    from main import api_channel_ack_pending, api_channel_poll_pending

    # Before ack: poll returns the message
    before = await api_channel_poll_pending(sid, FakeRequest())
    assert len(before["messages"]) == 1

    # Ack
    ack_result = await api_channel_ack_pending(sid, delivery_id, FakeRequest())
    assert ack_result["acked"]["deliveryId"] == delivery_id
    assert ack_result["acked"]["status"] == "acked"

    # After ack: poll returns empty
    after = await api_channel_poll_pending(sid, FakeRequest())
    assert len(after["messages"]) == 0


@pytest.mark.asyncio
async def test_ack_unknown_delivery_returns_404(monkeypatch):
    """ack on unknown deliveryId returns 404."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg

    app_agent_session_registry._sessions.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"chan-ack404-{_uuid.uuid4().hex[:8]}"
    sid = f"s-{rid}"
    app_agent_session_registry._sessions[sid] = _make_claude_channel_session(rid, sid)

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

    from main import api_channel_ack_pending

    with pytest.raises(HTTPException) as exc:
        await api_channel_ack_pending(sid, "unknown-delivery", FakeRequest())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_ack_requires_auth(monkeypatch):
    """ack requires auth before validation."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    class FakeRequest:
        headers = {}
        base_url = "http://testserver/"

    from main import _require_chat_auth, _api_config

    api_cfg = _api_config()
    with pytest.raises(HTTPException) as exc:
        _require_chat_auth(FakeRequest(), api_cfg)
    assert exc.value.status_code == 401


# ================================================================
# Claude channel — post replies
# ================================================================


@pytest.mark.asyncio
async def test_reply_writes_agent_message_under_same_turn(monkeypatch):
    """reply writes agent message under same remoteAgentId/source_turn_id."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg, memory_store, channel_pending_inbox

    app_agent_session_registry._sessions.clear()
    channel_pending_inbox._messages.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"chan-rep-{_uuid.uuid4().hex[:8]}"
    sid = f"s-{rid}"
    app_agent_session_registry._sessions[sid] = _make_claude_channel_session(rid, sid)

    cid = f"cm-rep-{_uuid.uuid4().hex[:8]}"

    # First: POST a user message to create the turn
    class PostRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {"clientMessageId": cid, "content": "hello", "remoteAgentId": rid}

    from main import api_send_message

    post_result = await api_send_message(PostRequest())
    user_turn_id = None
    found = await memory_store.find_app_message_by_client_message_id(cid, remote_agent_id=rid)
    assert found is not None
    user_turn_id = found["source_turn_id"]

    # Then: post a reply
    class ReplyRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {
                "clientMessageId": cid,
                "content": "Claude Code reply",
                "sourceToolCallId": "aspocket_reply_1",
            }

    from main import api_channel_post_reply

    reply_result = await api_channel_post_reply(sid, ReplyRequest())
    assert reply_result["reply"]["content"] == "Claude Code reply"
    assert reply_result["reply"]["remoteId"].startswith("app_rm_agent_")
    assert reply_result["reply"]["sourceToolCallId"] == "aspocket_reply_1"

    # Verify DB: agent reply under same turn_id + remote_agent_id
    replies = await memory_store.find_app_agent_replies_by_turn_id(
        user_turn_id, remote_agent_id=rid,
    )
    assert len(replies) == 1
    assert replies[0]["content"] == "Claude Code reply"
    assert replies[0]["source_tool_call_id"] == "aspocket_reply_1"
    assert replies[0]["remote_agent_id"] == rid


@pytest.mark.asyncio
async def test_reply_unknown_client_message_id_returns_404(monkeypatch):
    """reply with unknown clientMessageId returns 404."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg, channel_pending_inbox

    app_agent_session_registry._sessions.clear()
    channel_pending_inbox._messages.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"chan-rep404-{_uuid.uuid4().hex[:8]}"
    sid = f"s-{rid}"
    app_agent_session_registry._sessions[sid] = _make_claude_channel_session(rid, sid)

    class ReplyRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {"clientMessageId": "never-existed", "content": "reply"}

    from main import api_channel_post_reply

    with pytest.raises(HTTPException) as exc:
        await api_channel_post_reply(sid, ReplyRequest())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_reply_empty_content_returns_400(monkeypatch):
    """reply with empty content returns 400."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg, channel_pending_inbox

    app_agent_session_registry._sessions.clear()
    channel_pending_inbox._messages.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"chan-rep400-{_uuid.uuid4().hex[:8]}"
    sid = f"s-{rid}"
    app_agent_session_registry._sessions[sid] = _make_claude_channel_session(rid, sid)

    class ReplyRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {"clientMessageId": "cm-x", "content": "   "}

    from main import api_channel_post_reply

    with pytest.raises(HTTPException) as exc:
        await api_channel_post_reply(sid, ReplyRequest())
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_reply_offline_session_returns_503(monkeypatch):
    """reply on offline claude_channel session returns 503."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg, channel_pending_inbox

    app_agent_session_registry._sessions.clear()
    channel_pending_inbox._messages.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"chan-rep503-{_uuid.uuid4().hex[:8]}"
    sid = f"s-{rid}"
    app_agent_session_registry._sessions[sid] = _make_claude_channel_session(rid, sid, status="offline")

    class ReplyRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {"clientMessageId": "cm-x", "content": "reply"}

    from main import api_channel_post_reply

    with pytest.raises(HTTPException) as exc:
        await api_channel_post_reply(sid, ReplyRequest())
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_reply_requires_auth(monkeypatch):
    """reply requires auth before validation."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    class FakeRequest:
        headers = {}
        base_url = "http://testserver/"

    from main import _require_chat_auth, _api_config

    api_cfg = _api_config()
    with pytest.raises(HTTPException) as exc:
        _require_chat_auth(FakeRequest(), api_cfg)
    assert exc.value.status_code == 401


# ================================================================
# Claude channel — GET /api/messages history includes replies
# ================================================================


@pytest.mark.asyncio
async def test_get_history_returns_user_and_reply(monkeypatch):
    """GET /api/messages returns user message + channel reply."""
    import uuid as _uuid

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg, channel_pending_inbox

    app_agent_session_registry._sessions.clear()
    channel_pending_inbox._messages.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"chan-hist-{_uuid.uuid4().hex[:8]}"
    sid = f"s-{rid}"
    app_agent_session_registry._sessions[sid] = _make_claude_channel_session(rid, sid)

    cid = f"cm-hist-{_uuid.uuid4().hex[:8]}"

    class PostRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {"clientMessageId": cid, "content": "hi hist", "remoteAgentId": rid}

    from main import api_send_message, api_channel_post_reply, api_get_messages

    await api_send_message(PostRequest())

    class ReplyRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {"clientMessageId": cid, "content": "reply hist"}

    await api_channel_post_reply(sid, ReplyRequest())

    class GetRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

    result = await api_get_messages(GetRequest(), remoteAgentId=rid)
    msgs = result["messages"]
    assert len(msgs) == 2
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "agent"]
    assert msgs[0]["content"] == "hi hist"
    assert msgs[1]["content"] == "reply hist"


# ================================================================
# Claude channel — ChannelPendingInbox unit tests
# ================================================================


@pytest.mark.asyncio
async def test_inbox_add_is_idempotent_by_session_and_client_message_id():
    from runtime.channel_pending_inbox import ChannelPendingInbox

    inbox = ChannelPendingInbox()
    msg1 = await inbox.add(
        remote_agent_id="rid", session_id="s1",
        client_message_id="cm-1", content="hello", created_at="2026-06-20T00:00:00Z",
    )
    msg2 = await inbox.add(
        remote_agent_id="rid", session_id="s1",
        client_message_id="cm-1", content="hello", created_at="2026-06-20T00:00:00Z",
    )
    assert msg1["deliveryId"] == msg2["deliveryId"]
    pending = await inbox.list_pending("s1")
    assert len(pending) == 1


@pytest.mark.asyncio
async def test_inbox_list_pending_filters_by_session():
    from runtime.channel_pending_inbox import ChannelPendingInbox

    inbox = ChannelPendingInbox()
    await inbox.add(
        remote_agent_id="r1", session_id="s1",
        client_message_id="cm-1", content="m1", created_at="t",
    )
    await inbox.add(
        remote_agent_id="r2", session_id="s2",
        client_message_id="cm-2", content="m2", created_at="t",
    )
    s1_pending = await inbox.list_pending("s1")
    s2_pending = await inbox.list_pending("s2")
    assert len(s1_pending) == 1
    assert s1_pending[0]["content"] == "m1"
    assert len(s2_pending) == 1
    assert s2_pending[0]["content"] == "m2"


@pytest.mark.asyncio
async def test_inbox_ack_marks_status_acked():
    from runtime.channel_pending_inbox import ChannelPendingInbox

    inbox = ChannelPendingInbox()
    msg = await inbox.add(
        remote_agent_id="r", session_id="s",
        client_message_id="cm", content="x", created_at="t",
    )
    acked = await inbox.ack(msg["deliveryId"])
    assert acked["status"] == "acked"
    pending = await inbox.list_pending("s")
    assert len(pending) == 0  # acked messages are suppressed


@pytest.mark.asyncio
async def test_inbox_ack_unknown_returns_none():
    from runtime.channel_pending_inbox import ChannelPendingInbox

    inbox = ChannelPendingInbox()
    result = await inbox.ack("nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_inbox_ack_is_idempotent():
    from runtime.channel_pending_inbox import ChannelPendingInbox

    inbox = ChannelPendingInbox()
    msg = await inbox.add(
        remote_agent_id="r", session_id="s",
        client_message_id="cm", content="x", created_at="t",
    )
    first = await inbox.ack(msg["deliveryId"])
    second = await inbox.ack(msg["deliveryId"])
    assert first is not None
    assert second is not None
    assert first["deliveryId"] == second["deliveryId"]


@pytest.mark.asyncio
async def test_inbox_list_pending_respects_limit():
    from runtime.channel_pending_inbox import ChannelPendingInbox

    inbox = ChannelPendingInbox()
    for i in range(5):
        await inbox.add(
            remote_agent_id="r", session_id="s",
            client_message_id=f"cm-{i}", content=f"m{i}", created_at="t",
        )
    pending = await inbox.list_pending("s", limit=3)
    assert len(pending) == 3


# ================================================================
# Claude channel — regression: default and attached_session unchanged
# ================================================================


@pytest.mark.asyncio
async def test_default_internal_path_still_works_with_channel_code_present(monkeypatch):
    """POST /api/messages with default remoteAgentId still works (internal agent)."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg, channel_pending_inbox

    app_agent_session_registry._sessions.clear()
    channel_pending_inbox._messages.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {
                "clientMessageId": "cm-regress-default-chan",
                "content": "hello default",
            }

    from main import api_send_message

    result = await api_send_message(FakeRequest())
    assert "userMessage" in result


@pytest.mark.asyncio
async def test_attached_session_callback_still_works_with_channel_code_present(monkeypatch):
    """Attached-session callback path still works (claude_channel code doesn't interfere)."""
    import uuid as _uuid
    import httpx
    from unittest.mock import AsyncMock, MagicMock

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg, channel_pending_inbox

    app_agent_session_registry._sessions.clear()
    channel_pending_inbox._messages.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    rid = f"att-cb-{_uuid.uuid4().hex[:8]}"
    app_agent_session_registry._sessions[f"s-{rid}"] = _make_attached_session(rid)

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"agentReplies": [{"content": "cb reply"}]}

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

    cid = f"cm-att-{_uuid.uuid4().hex[:8]}"

    class FakeRequest:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {"clientMessageId": cid, "content": "hi", "remoteAgentId": rid}

    from main import api_send_message

    result = await api_send_message(FakeRequest())
    assert len(result["agentReplies"]) == 1
    assert result["agentReplies"][0]["content"] == "cb reply"

    # Channel inbox should be empty (message went through callback path)
    pending = await channel_pending_inbox.list_pending(f"s-{rid}")
    assert len(pending) == 0


# ================================================================
# Image attachments (Phase 1) — upload / download / message extension
# ================================================================

import asyncio as _asyncio
from pathlib import Path as _Path


def _build_multipart(parts, boundary=None):
    """Build a multipart/form-data body + content-type header for tests.

    *parts*: list of dicts with keys: ``name``, ``filename`` (optional),
    ``content_type`` (optional), ``data`` (bytes or str).
    Returns ``(content_type, body_bytes)``.
    """
    import uuid as _uuid2
    boundary = boundary or f"----GoalTest{_uuid2.uuid4().hex[:8]}"
    b = boundary.encode()
    crlf = b"\r\n"
    chunks = []
    for p in parts:
        chunks.append(b"--" + b + crlf)
        header_lines = [
            f'Content-Disposition: form-data; name="{p["name"]}"'
            + (f'; filename="{p["filename"]}"' if p.get("filename") else ""),
        ]
        if p.get("content_type"):
            header_lines.append(f"Content-Type: {p['content_type']}")
        for h in header_lines:
            chunks.append(h.encode() + crlf)
        chunks.append(crlf)
        data = p["data"]
        if isinstance(data, str):
            data = data.encode("utf-8")
        chunks.append(data + crlf)
    chunks.append(b"--" + b + b"--" + crlf)
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


class _MultipartRequest:
    """Fake FastAPI Request for attachment endpoints."""

    def __init__(self, content_type, body, auth=True):
        self.headers = {"content-type": content_type}
        if auth:
            self.headers["authorization"] = "Bearer secret"
        self.base_url = "http://127.0.0.1:8899/"
        self._body = body

    async def body(self):
        return self._body


@pytest.fixture
def _clean_attachments():
    """Remove test attachments from data/app_attachments/."""
    d = _Path("./data/app_attachments")
    if d.exists():
        for f in d.iterdir():
            if f.is_file() and f.name.startswith("app_att_"):
                f.unlink()
    yield
    if d.exists():
        for f in d.iterdir():
            if f.is_file() and f.name.startswith("app_att_"):
                f.unlink()


# ── test 1: upload image success ────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_image_success_returns_metadata(monkeypatch):
    """POST /api/attachments with a valid image returns metadata + url."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50  # valid PNG header
    ct, body = _build_multipart(
        [{"name": "file", "filename": "cat.png",
          "content_type": "image/png", "data": png}])

    from main import api_upload_attachment
    result = await api_upload_attachment(
        _MultipartRequest(ct, body))
    assert result["type"] == "image"
    assert result["mimeType"] == "image/png"
    assert result["fileName"] == "cat.png"
    assert result["sizeBytes"] == len(png)
    aid = result["attachmentId"]
    assert aid.startswith("app_att_")
    assert result["url"].endswith(f"/api/attachments/{aid}")
    assert result["createdAt"].endswith("Z")

    # File persisted on disk
    from main import memory_store
    meta = await memory_store.get_app_attachment(aid)
    assert meta is not None
    assert meta["mime_type"] == "image/png"
    storage = _Path(meta["storage_path"])
    assert storage.exists()
    assert storage.read_bytes() == png

    # Cleanup
    storage.unlink(missing_ok=True)


# ── test 2: upload non-image returns 400 ────────────────────────────


@pytest.mark.asyncio
async def test_upload_non_image_returns_400(monkeypatch):
    """POST /api/attachments with text/plain returns 400."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    ct, body = _build_multipart(
        [{"name": "file", "filename": "a.txt",
          "content_type": "text/plain", "data": b"hello"}])

    from main import api_upload_attachment
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await api_upload_attachment(_MultipartRequest(ct, body))
    assert exc.value.status_code == 400


# ── test 3: upload over 10 MB returns 413 ───────────────────────────


@pytest.mark.asyncio
async def test_upload_over_10mb_returns_413(monkeypatch):
    """POST /api/attachments with >10 MB file returns 413."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    # 10 MB + 1 byte: triggers file-size guard
    big = b"\x00" * (10 * 1024 * 1024 + 1)  # 10_485_761 bytes
    ct, body = _build_multipart(
        [{"name": "file", "filename": "big.png",
          "content_type": "image/png", "data": big}])

    from main import api_upload_attachment
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await api_upload_attachment(_MultipartRequest(ct, body))
    assert exc.value.status_code == 413


# ── test 4: unauth upload returns 401 and writes no file ────────────


@pytest.mark.asyncio
async def test_upload_unauth_returns_401_no_file(monkeypatch):
    """POST /api/attachments without auth returns 401 before writing."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    ct, body = _build_multipart(
        [{"name": "file", "filename": "nope.png",
          "content_type": "image/png", "data": b"\x89PNG\r\n\x1a\n"}])
    from fastapi import HTTPException

    # Check that no files are written
    d = _Path("./data/app_attachments")
    before = set(d.iterdir()) if d.exists() else set()

    from main import api_upload_attachment

    with pytest.raises(HTTPException) as exc:
        await api_upload_attachment(_MultipartRequest(ct, body, auth=False))
    assert exc.value.status_code == 401

    after = set(d.iterdir()) if d.exists() else set()
    assert after == before  # no new files


# ── test 5: GET attachment success, correct media type ──────────────


@pytest.mark.asyncio
async def test_get_attachment_success(monkeypatch):
    """GET /api/attachments/{id} returns the file with correct media type."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")

    # Upload first
    png = b"\x89PNG\r\n\x1a\n" + b"hello"
    ct, body = _build_multipart(
        [{"name": "file", "filename": "hi.png",
          "content_type": "image/png", "data": png}])
    from main import api_upload_attachment, api_get_attachment

    up = await api_upload_attachment(_MultipartRequest(ct, body))
    aid = up["attachmentId"]

    resp = await api_get_attachment(
        aid, _MultipartRequest(ct, body, auth=True))
    assert resp.media_type == "image/png"
    assert resp.status_code == 200
    # Read back content
    import tempfile
    # FileResponse is a callable (send) — we check the path instead
    fp = _Path(resp.path)
    assert fp.exists()
    assert fp.read_bytes() == png

    # Cleanup
    fp.unlink(missing_ok=True)


# ── test 6: GET missing attachment returns 404 ──────────────────────


@pytest.mark.asyncio
async def test_get_attachment_missing_returns_404(monkeypatch):
    """GET /api/attachments/{id} for unknown id returns 404."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")
    from fastapi import HTTPException
    from main import api_get_attachment

    with pytest.raises(HTTPException) as exc:
        await api_get_attachment(
            "app_att_deadbeef00000000",
            _MultipartRequest("", b"", auth=True))
    assert exc.value.status_code == 404


# ── test 6b: path traversal rejected as 404 ─────────────────────────


@pytest.mark.asyncio
async def test_get_attachment_path_traversal_returns_404(monkeypatch):
    """IDs that don't match the expected pattern are 404, not a traversal."""
    monkeypatch.setenv("CHAT_API_KEY", "secret")
    from fastapi import HTTPException
    from main import api_get_attachment

    for bad_id in ("../../etc/passwd", "\\..\\..\\secret", "not-an-id"):
        with pytest.raises(HTTPException) as exc:
            await api_get_attachment(
                bad_id, _MultipartRequest("", b"", auth=True))
        assert exc.value.status_code == 404


# ── test 7: POST content empty + image attachment succeeds ──────────


@pytest.mark.asyncio
async def test_post_content_empty_image_attachment_succeeds(monkeypatch):
    """POST /api/messages with empty content + valid attachment succeeds."""
    import uuid as _t7

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    # Upload an image first
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
    ct, body = _build_multipart(
        [{"name": "file", "filename": "img.jpg",
          "content_type": "image/jpeg", "data": png}])
    from main import api_upload_attachment, api_send_message

    up = await api_upload_attachment(_MultipartRequest(ct, body))
    aid = up["attachmentId"]

    cid = f"cm-t7-{_t7.uuid4().hex[:8]}"

    class _Req:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://127.0.0.1:8899/"

        async def json(self):
            return {
                "clientMessageId": cid,
                "content": "",
                "attachments": [{
                    "attachmentId": aid,
                    "type": "image",
                    "mimeType": "image/jpeg",
                    "fileName": "img.jpg",
                    "sizeBytes": len(png),
                    "url": f"http://127.0.0.1:8899/api/attachments/{aid}",
                }],
            }

    result = await api_send_message(_Req())
    assert "userMessage" in result
    um = result["userMessage"]
    assert um["clientMessageId"] == cid
    assert um["status"] == "sent"
    assert len(um.get("attachments", [])) == 1
    assert um["attachments"][0]["attachmentId"] == aid

    # Cleanup
    storage = _Path("./data/app_attachments") / f"{aid}.jpg"
    storage.unlink(missing_ok=True)


# ── test 8: POST no content + no attachments returns 400 ────────────


@pytest.mark.asyncio
async def test_post_no_content_no_attachments_returns_400(monkeypatch):
    """POST /api/messages with empty content and no attachments returns 400."""
    import uuid as _t8
    monkeypatch.setenv("CHAT_API_KEY", "secret")
    from fastapi import HTTPException
    from main import api_send_message

    cid = f"cm-t8-{_t8.uuid4().hex[:8]}"

    class _Req:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {"clientMessageId": cid, "content": ""}

    with pytest.raises(HTTPException) as exc:
        await api_send_message(_Req())
    assert exc.value.status_code == 400


# ── test 9: POST unknown attachmentId returns 400 ───────────────────


@pytest.mark.asyncio
async def test_post_unknown_attachment_returns_400(monkeypatch):
    """POST /api/messages with non-existent attachmentId returns 400."""
    import uuid as _t9

    monkeypatch.setenv("CHAT_API_KEY", "secret")
    from fastapi import HTTPException
    from main import api_send_message

    cid = f"cm-t9-{_t9.uuid4().hex[:8]}"

    class _Req:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

        async def json(self):
            return {
                "clientMessageId": cid,
                "content": "hi",
                "attachments": [{
                    "attachmentId": "app_att_deadbeef00000000",
                    "type": "image",
                    "mimeType": "image/png",
                    "fileName": "nope.png",
                    "sizeBytes": 100,
                    "url": "http://x/y",
                }],
            }

    with pytest.raises(HTTPException) as exc:
        await api_send_message(_Req())
    assert exc.value.status_code == 400


# ── test 10: >1 attachment returns 400 ──────────────────────────────


@pytest.mark.asyncio
async def test_post_more_than_one_attachment_returns_400(monkeypatch):
    """POST /api/messages with 2 attachments returns 400 (Phase 1 limit)."""
    import uuid as _t10

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    # Upload 2 images
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
    ct, body = _build_multipart(
        [{"name": "file", "filename": "a.png",
          "content_type": "image/png", "data": png}])
    ct2, body2 = _build_multipart(
        [{"name": "file", "filename": "bb.png",
          "content_type": "image/png", "data": png}])
    from main import api_upload_attachment
    up_a = await api_upload_attachment(_MultipartRequest(ct, body))
    up_b = await api_upload_attachment(_MultipartRequest(ct2, body2))

    from fastapi import HTTPException
    from main import api_send_message
    cid = f"cm-t10-{_t10.uuid4().hex[:8]}"

    class _Req:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://127.0.0.1:8899/"

        async def json(self):
            return {
                "clientMessageId": cid,
                "content": "hi",
                "attachments": [
                    {"attachmentId": up_a["attachmentId"], "type": "image",
                     "mimeType": "image/png", "fileName": "a.png",
                     "sizeBytes": len(png),
                     "url": up_a["url"]},
                    {"attachmentId": up_b["attachmentId"], "type": "image",
                     "mimeType": "image/png", "fileName": "bb.png",
                     "sizeBytes": len(png),
                     "url": up_b["url"]},
                ],
            }

    with pytest.raises(HTTPException) as exc:
        await api_send_message(_Req())
    assert exc.value.status_code == 400

    # Cleanup
    for uid in (up_a["attachmentId"], up_b["attachmentId"]):
        (_Path("./data/app_attachments") / f"{uid}.png").unlink(missing_ok=True)


# ── test 11: GET /api/messages returns attachments metadata ─────────


@pytest.mark.asyncio
async def test_get_messages_returns_attachments(monkeypatch):
    """GET /api/messages returns attachments metadata per message."""
    import uuid as _t11

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    # Upload image
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
    ct, body = _build_multipart(
        [{"name": "file", "filename": "test.jpg",
          "content_type": "image/jpeg", "data": png}])
    from main import api_upload_attachment, api_send_message, api_get_messages
    up = await api_upload_attachment(_MultipartRequest(ct, body))
    aid = up["attachmentId"]

    # POST with attachment
    cid = f"cm-t11-{_t11.uuid4().hex[:8]}"

    class _PostReq:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://127.0.0.1:8899/"

        async def json(self):
            return {
                "clientMessageId": cid,
                "content": "看这张图",
                "attachments": [{
                    "attachmentId": aid,
                    "type": "image",
                    "mimeType": "image/jpeg",
                    "fileName": "test.jpg",
                    "sizeBytes": len(png),
                    "url": up["url"],
                }],
            }

    # Insert a test message directly via store (deterministic, no agent call)
    from main import memory_store
    import time as _time

    user_rid = f"app_rm_user_{_t11.uuid4().hex[:12]}"
    now_ts = _time.time()
    await memory_store.insert_app_message(
        remote_id=user_rid,
        client_message_id=cid,
        role="user",
        content="看这张图",
        source_turn_id=f"app_turn_{_t11.uuid4().hex[:12]}",
        sequence=0,
        remote_agent_id="default",
        created_at=now_ts,
        attachments=[{
            "attachmentId": aid,
            "type": "image",
            "mimeType": "image/jpeg",
            "fileName": "test.jpg",
            "sizeBytes": len(png),
            "url": up["url"],
        }],
    )

    # Verify via direct store lookup
    found = await memory_store.find_app_message_by_client_message_id(cid)
    assert found is not None
    parsed = json.loads(found["attachments_json"])
    assert len(parsed) == 1
    assert parsed[0]["attachmentId"] == aid
    assert parsed[0]["type"] == "image"

    # GET history: any message should have the attachments field ([] or populated)
    class _GetReq:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

    result = await api_get_messages(_GetReq(), limit=5)
    msgs = result["messages"]
    for m in msgs:
        assert "attachments" in m  # all messages carry the field
        assert isinstance(m["attachments"], list)

    # Cleanup
    (_Path("./data/app_attachments") / f"{aid}.jpeg").unlink(missing_ok=True)


# ── test 12: idempotency — same attachments returns existing ────────


@pytest.mark.asyncio
async def test_idempotency_same_attachments_returns_existing(monkeypatch):
    """Same clientMessageId + content + attachments returns cached result."""
    import uuid as _t12

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
    ct, body = _build_multipart(
        [{"name": "file", "filename": "idem.jpg",
          "content_type": "image/jpeg", "data": png}])
    from main import api_upload_attachment, api_send_message
    up = await api_upload_attachment(_MultipartRequest(ct, body))
    aid = up["attachmentId"]

    cid = f"cm-t12-{_t12.uuid4().hex[:8]}"
    attach_payload = [{
        "attachmentId": aid,
        "type": "image",
        "mimeType": "image/jpeg",
        "fileName": "idem.jpg",
        "sizeBytes": len(png),
        "url": up["url"],
    }]

    class _Req:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://127.0.0.1:8899/"

        async def json(self):
            return {
                "clientMessageId": cid,
                "content": "test",
                "attachments": attach_payload,
            }

    r1 = await api_send_message(_Req())
    r2 = await api_send_message(_Req())

    assert r1["userMessage"]["remoteId"] == r2["userMessage"]["remoteId"]
    assert r1["userMessage"]["clientMessageId"] == cid

    # Cleanup
    (_Path("./data/app_attachments") / f"{aid}.jpeg").unlink(missing_ok=True)


# ── test 13: idempotency — different attachments returns 409 ────────


@pytest.mark.asyncio
async def test_idempotency_different_attachments_returns_409(monkeypatch):
    """Same clientMessageId + different attachments returns 409."""
    import uuid as _t13

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    png_a = b"\x89PNG\r\n\x1a\n" + b"A" * 20
    png_b = b"\x89PNG\r\n\x1a\n" + b"B" * 20
    ct, body = _build_multipart(
        [{"name": "file", "filename": "imgA.jpg",
          "content_type": "image/jpeg", "data": png_a}])
    from main import api_upload_attachment, api_send_message
    up_a = await api_upload_attachment(_MultipartRequest(ct, body))
    # Upload second image with a different name
    ct2, body2 = _build_multipart(
        [{"name": "file", "filename": "imgB.jpg",
          "content_type": "image/jpeg", "data": png_b}])
    up_b = await api_upload_attachment(
        _MultipartRequest(ct2, body2))

    cid = f"cm-t13-{_t13.uuid4().hex[:8]}"

    # First post with imgA
    class _ReqA:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://127.0.0.1:8899/"

        async def json(self):
            return {
                "clientMessageId": cid,
                "content": "diff",
                "attachments": [{
                    "attachmentId": up_a["attachmentId"],
                    "type": "image",
                    "mimeType": "image/jpeg",
                    "fileName": "imgA.jpg",
                    "sizeBytes": len(png_a),
                    "url": up_a["url"],
                }],
            }

    r1 = await api_send_message(_ReqA())
    assert "userMessage" in r1

    # Second post with same cid but imgB → 409
    class _ReqB:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://127.0.0.1:8899/"

        async def json(self):
            return {
                "clientMessageId": cid,
                "content": "diff",
                "attachments": [{
                    "attachmentId": up_b["attachmentId"],
                    "type": "image",
                    "mimeType": "image/jpeg",
                    "fileName": "imgB.jpg",
                    "sizeBytes": len(png_b),
                    "url": up_b["url"],
                }],
            }

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await api_send_message(_ReqB())
    assert exc.value.status_code == 409

    # Cleanup
    for uid in (up_a["attachmentId"], up_b["attachmentId"]):
        (_Path("./data/app_attachments") / f"{uid}.jpeg").unlink(missing_ok=True)


# ── test 14: claude_channel pending inbox saves attachments ─────────


@pytest.mark.asyncio
async def test_claude_channel_pending_attachments(monkeypatch):
    """Claude channel pending inbox stores and returns attachments."""
    import uuid as _t14

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg, channel_pending_inbox

    app_agent_session_registry._sessions.clear()
    channel_pending_inbox._messages.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    # Upload an image
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
    ct, body = _build_multipart(
        [{"name": "file", "filename": "chan.jpg",
          "content_type": "image/jpeg", "data": png}])
    from main import api_upload_attachment
    up = await api_upload_attachment(_MultipartRequest(ct, body))
    aid = up["attachmentId"]

    # Register claude_channel session
    rid = f"chan-att-{_t14.uuid4().hex[:8]}"
    sid = f"s-{rid}"
    app_agent_session_registry._sessions[sid] = _make_claude_channel_session(rid, sid)

    cid = f"cm-t14-{_t14.uuid4().hex[:8]}"

    class _PostReq:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://127.0.0.1:8899/"

        async def json(self):
            return {
                "clientMessageId": cid,
                "content": "channel message",
                "remoteAgentId": rid,
                "attachments": [{
                    "attachmentId": aid,
                    "type": "image",
                    "mimeType": "image/jpeg",
                    "fileName": "chan.jpg",
                    "sizeBytes": len(png),
                    "url": up["url"],
                }],
            }

    from main import api_send_message, api_channel_poll_pending

    await api_send_message(_PostReq())

    class _PollReq:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://testserver/"

    poll = await api_channel_poll_pending(sid, _PollReq())
    assert len(poll["messages"]) == 1
    pending = poll["messages"][0]
    assert pending["clientMessageId"] == cid
    attachments = pending.get("attachments", [])
    assert len(attachments) == 1
    assert attachments[0]["attachmentId"] == aid

    # Cleanup
    (_Path("./data/app_attachments") / f"{aid}.jpeg").unlink(missing_ok=True)


# ── test 15: callback payload includes attachments ──────────────────


@pytest.mark.asyncio
async def test_callback_payload_includes_attachments(monkeypatch):
    """Attached-session callback POST body includes attachments field."""
    import uuid as _t15
    import httpx
    from unittest.mock import MagicMock

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    from main import app_agent_session_registry, cfg, channel_pending_inbox

    app_agent_session_registry._sessions.clear()
    channel_pending_inbox._messages.clear()
    cfg.setdefault("app", {}).pop("contact_invitations", None)

    # Upload an image
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
    ct, body = _build_multipart(
        [{"name": "file", "filename": "cb.jpg",
          "content_type": "image/jpeg", "data": png}])
    from main import api_upload_attachment
    up = await api_upload_attachment(_MultipartRequest(ct, body))
    aid = up["attachmentId"]

    # Register attached session
    rid = f"att-cb-att-{_t15.uuid4().hex[:8]}"
    app_agent_session_registry._sessions[f"s-{rid}"] = _make_attached_session(rid)

    # Capture callback payload
    captured = {}

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"agentReplies": [{"content": "got attachments"}]}

    class _FakeClientCtx:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            client = MagicMock()

            async def _capture(url, json=None, **kw):
                captured["url"] = url
                captured["json"] = json
                return fake_resp

            client.post = _capture
            return client

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClientCtx)

    cid = f"cm-t15-{_t15.uuid4().hex[:8]}"

    class _PostReq:
        headers = {"authorization": "Bearer secret"}
        base_url = "http://127.0.0.1:8899/"

        async def json(self):
            return {
                "clientMessageId": cid,
                "content": "hello with image",
                "remoteAgentId": rid,
                "attachments": [{
                    "attachmentId": aid,
                    "type": "image",
                    "mimeType": "image/jpeg",
                    "fileName": "cb.jpg",
                    "sizeBytes": len(png),
                    "url": up["url"],
                }],
            }

    from main import api_send_message
    result = await api_send_message(_PostReq())
    assert len(result["agentReplies"]) == 1

    # Assert callback payload includes attachments
    assert "json" in captured, "callback was called"
    payload = captured["json"]
    assert "attachments" in payload
    attachments = payload["attachments"]
    assert len(attachments) == 1
    assert attachments[0]["attachmentId"] == aid
    assert attachments[0]["type"] == "image"

    # Cleanup
    (_Path("./data/app_attachments") / f"{aid}.jpeg").unlink(missing_ok=True)


# ── Store-layer tests for attachments ───────────────────────────────


@pytest.mark.asyncio
async def test_app_message_stores_and_returns_attachments():
    """insert_app_message with attachments stores JSON; SELECT returns it."""
    from memory.store import MemoryStore

    store = MemoryStore(db_path=":memory:")
    try:
        await store.insert_app_message(
            remote_id="rm-att-1",
            client_message_id="cm-att-1",
            role="user",
            content="hello",
            source_turn_id="turn-1",
            attachments=[{"attachmentId": "app_att_test", "type": "image"}],
        )
        found = await store.find_app_message_by_client_message_id("cm-att-1")
        assert found is not None
        import json as _json
        parsed = _json.loads(found["attachments_json"])
        assert len(parsed) == 1
        assert parsed[0]["attachmentId"] == "app_att_test"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_app_attachment_insert_and_get():
    """insert_app_attachment persists; get_app_attachment reads back."""
    from memory.store import MemoryStore

    store = MemoryStore(db_path=":memory:")
    try:
        await store.insert_app_attachment(
            attachment_id="app_att_store1",
            mime_type="image/png",
            file_name="test.png",
            size_bytes=123,
            storage_path="/tmp/test.png",
        )
        meta = await store.get_app_attachment("app_att_store1")
        assert meta is not None
        assert meta["mime_type"] == "image/png"
        assert meta["file_name"] == "test.png"
        assert meta["size_bytes"] == 123

        # Missing returns None
        meta2 = await store.get_app_attachment("app_att_nonexist")
        assert meta2 is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_app_attachments_get_multiple():
    """get_app_attachments returns all matching IDs, ignores missing."""
    from memory.store import MemoryStore

    store = MemoryStore(db_path=":memory:")
    try:
        await store.insert_app_attachment(
            attachment_id="app_att_a",
            mime_type="image/png",
            file_name="a.png",
            size_bytes=10,
            storage_path="/tmp/a.png",
        )
        await store.insert_app_attachment(
            attachment_id="app_att_b",
            mime_type="image/jpeg",
            file_name="b.jpg",
            size_bytes=20,
            storage_path="/tmp/b.jpg",
        )
        results = await store.get_app_attachments(
            ["app_att_a", "app_att_b", "app_att_missing"])
        assert len(results) == 2
        ids = {r["attachment_id"] for r in results}
        assert ids == {"app_att_a", "app_att_b"}

        # Empty input
        empty = await store.get_app_attachments([])
        assert empty == []
    finally:
        await store.close()
