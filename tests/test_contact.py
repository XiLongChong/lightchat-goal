"""Tests for runtime.contact."""

from runtime.contact import contact_send_block_reason, contact_can_send_for_bot


def test_contact_send_block_reason_no_contact():
    contact = {}
    reason = contact_send_block_reason(contact, "default")
    assert reason == "no contact"


def test_contact_send_block_reason_missing_context_token():
    contact = {"user_id": "test_user"}
    reason = contact_send_block_reason(contact, "default")
    assert reason == "missing context_token"


def test_contact_send_block_reason_bot_not_bound():
    contact = {
        "user_id": "test_user",
        "context_token": "tok123",
        "bot_id": "other_bot",
    }
    reason = contact_send_block_reason(contact, "default")
    assert reason == "bot not bound"


def test_contact_send_block_reason_stale():
    contact = {
        "user_id": "test_user",
        "context_token": "tok123",
        "bot_id": "default",
        "updated_at": 1000.0,
    }
    reason = contact_send_block_reason(contact, "default", process_started_at=2000.0)
    assert reason == "stale contact binding"


def test_contact_send_block_reason_ok():
    contact = {
        "user_id": "test_user",
        "context_token": "tok123",
        "bot_id": "default",
        "updated_at": 2000.0,
    }
    reason = contact_send_block_reason(contact, "default", process_started_at=1000.0)
    assert reason == ""


def test_contact_can_send_for_bot_true():
    contact = {
        "user_id": "test_user",
        "context_token": "tok123",
        "bot_id": "default",
        "updated_at": 2000.0,
    }
    assert contact_can_send_for_bot(contact, "default", process_started_at=1000.0) is True


def test_contact_can_send_for_bot_false():
    contact = {"user_id": "test_user"}
    assert contact_can_send_for_bot(contact, "default") is False


def test_contact_send_block_reason_empty_bot_id_in_contact():
    """When contact has no bot_id, it should not match any bot."""
    contact = {
        "user_id": "test_user",
        "context_token": "tok123",
        "bot_id": "",
    }
    reason = contact_send_block_reason(contact, "default")
    assert reason == "bot not bound"


def test_contact_send_block_reason_none_bot_id_in_contact():
    contact = {
        "user_id": "test_user",
        "context_token": "tok123",
    }
    reason = contact_send_block_reason(contact, "default")
    # No bot_id key → str(None or "") → "" → not equal to "default"
    assert reason == "bot not bound"
