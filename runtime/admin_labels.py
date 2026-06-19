"""Admin display labels — pure conversion functions, no state."""

from typing import Any

from agent.state_machine import TurnScratchpad


def route_label(route: str) -> str:
    return {
        "ordinary_chat": "普通聊天",
    }.get(route or "", route or "未知")


def policy_label(policy: str) -> str:
    return {
        "short_term_history": "短期对话历史",
    }.get(policy or "", policy or "无")


def send_status_label(status: str) -> str:
    return {
        "created": "已生成",
        "generated": "已生成",
        "sending": "发送中",
        "delivered": "已送达",
        "failed": "发送失败",
    }.get(status or "", status or "未知")


def source_label(source: str) -> str:
    return {
        "wechat": "微信",
        "user_reply": "用户回复",
        "admin": "后台手动",
    }.get(source or "", source or "未知")


def visible_messages_as_dicts(turn: TurnScratchpad) -> list[dict[str, Any]]:
    """Convert VisibleMessage list to backward-compat dict list."""
    return [
        {
            "text": vm.guarded_text or vm.original_text,
            "delay": vm.delay,
            "send_status": vm.delivery_status,
            "delivery_status": vm.delivery_status,
        }
        for vm in turn.visible_messages
    ]
