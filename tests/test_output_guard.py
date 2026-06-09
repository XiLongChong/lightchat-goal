from agent.output_guard import (
    HARD_FALLBACK_TEXT,
    guard_messages,
    guard_single_message,
)


def test_guard_single_message_cleans_wechat_text():
    assert guard_single_message("**你好**\n\n\n`今天`") == "你好\n\n今天"


def test_guard_single_message_keeps_empty_text_empty():
    assert guard_single_message("") == ""


def test_guard_single_message_blocks_tool_call_id():
    issues: list[str] = []
    result = guard_single_message("internal call_abcdefgh leaked", issues=issues)
    assert result == HARD_FALLBACK_TEXT
    assert issues == ["tool_call_id_blocked"]


def test_guard_single_message_redacts_secret():
    secret = "sk-" + "a" * 24
    assert guard_single_message(f"key={secret}") == "key=[redacted]"


def test_guard_single_message_truncates_long_text():
    assert len(guard_single_message("a" * 600)) == 500


def test_guard_messages_limits_count_and_preserves_delay():
    messages = [{"text": f"message {index}", "delay": index} for index in range(5)]
    assert guard_messages(messages, max_messages=2) == [
        {"text": "message 0", "delay": 0},
        {"text": "message 1", "delay": 1},
    ]


def test_guard_messages_returns_fallback_when_all_entries_are_empty():
    assert guard_messages([{"text": ""}]) == [
        {"text": HARD_FALLBACK_TEXT, "delay": 0}
    ]


def test_guard_messages_keeps_empty_batch_empty():
    assert guard_messages([]) == []
