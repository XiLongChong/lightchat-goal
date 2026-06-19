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

    from main import api_contact_invitations

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
        "id", "displayName", "avatarUri", "description",
        "agentType", "sourceLabel", "endpoint", "createdAt", "expiresAt",
    }
    assert required_fields.issubset(inv.keys())
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
