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
