"""Tests for runtime.admin_labels."""

from types import SimpleNamespace

from runtime.admin_labels import (
    policy_label,
    route_label,
    send_status_label,
    source_label,
    visible_messages_as_dicts,
)


def test_route_label_known_and_unknown():
    assert route_label("ordinary_chat") == "普通聊天"
    assert route_label("custom_route") == "custom_route"
    assert route_label("") == "未知"


def test_policy_label_known_and_unknown():
    assert policy_label("short_term_history") == "短期对话历史"
    assert policy_label("custom_policy") == "custom_policy"
    assert policy_label("") == "无"


def test_send_status_label_known_and_unknown():
    assert send_status_label("delivered") == "已送达"
    assert send_status_label("custom_status") == "custom_status"
    assert send_status_label("") == "未知"


def test_source_label_known_and_unknown():
    assert source_label("wechat") == "微信"
    assert source_label("custom_source") == "custom_source"
    assert source_label("") == "未知"


def test_visible_messages_as_dicts_prefers_guarded_text():
    turn = SimpleNamespace(
        visible_messages=[
            SimpleNamespace(
                guarded_text="guarded",
                original_text="original",
                delay=1.5,
                delivery_status="delivered",
            ),
            SimpleNamespace(
                guarded_text="",
                original_text="fallback",
                delay=0.0,
                delivery_status="failed",
            ),
        ]
    )

    assert visible_messages_as_dicts(turn) == [
        {
            "text": "guarded",
            "delay": 1.5,
            "send_status": "delivered",
            "delivery_status": "delivered",
        },
        {
            "text": "fallback",
            "delay": 0.0,
            "send_status": "failed",
            "delivery_status": "failed",
        },
    ]
