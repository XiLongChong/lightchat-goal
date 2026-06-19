"""Contact send eligibility helpers — pure functions."""

from typing import Any


def contact_send_block_reason(
    contact: dict[str, Any],
    bot_id: str,
    process_started_at: float = 0.0,
) -> str:
    """Return why a stored WeChat contact cannot receive current-bot sends."""
    if not contact.get("user_id"):
        return "no contact"
    if not contact.get("context_token"):
        return "missing context_token"
    contact_bot = str(contact.get("bot_id", "") or "")
    if contact_bot != bot_id:
        return "bot not bound"
    updated_at = float(contact.get("updated_at") or 0)
    if updated_at < process_started_at:
        return "stale contact binding"
    return ""


def contact_can_send_for_bot(
    contact: dict[str, Any],
    bot_id: str,
    process_started_at: float = 0.0,
) -> bool:
    return contact_send_block_reason(contact, bot_id, process_started_at) == ""
