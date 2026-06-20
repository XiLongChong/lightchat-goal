"""Lightweight unit tests for the Claude Code mock adapter's pure helpers.

These tests exercise the payload/response construction helpers only — no real
HTTP server is started, so there is no port usage and no flakiness.
"""

from __future__ import annotations

from datetime import datetime, timezone

from tools.claude_code_mock_adapter import (
    build_claude_mock_reply,
    build_empty_content_reply,
    build_registration_payload,
    build_heartbeat_url,
    _reply_template,
)


# ── _reply_template ────────────────────────────────────────────────


def test_reply_template_structure():
    """_reply_template returns a dict with all v1 callback fields."""
    now = datetime(2026, 6, 19, 12, 0, 2, tzinfo=timezone.utc)
    entry = _reply_template("cm-001", "hello", now=now)
    assert entry == {
        "content": "hello",
        "remoteId": "claude-mock-cm-001",
        "createdAt": "2026-06-19T12:00:02Z",
    }


def test_reply_template_default_now_ends_with_z():
    """Without an explicit now, createdAt is a Z-suffixed ISO string."""
    entry = _reply_template("cm-002", "hi")
    assert entry["createdAt"].endswith("Z")
    assert len(entry["createdAt"]) == 20


# ── build_claude_mock_reply ────────────────────────────────────────


def test_status_prompt_returns_mock_status():
    """Content containing 'status' returns a mock status report."""
    reply = build_claude_mock_reply("show status", "cm-s1")
    entry = reply["agentReplies"][0]
    assert "mock status" in entry["content"]
    assert "no real tasks" in entry["content"]
    assert entry["remoteId"] == "claude-mock-cm-s1"


def test_chinese_status_prompt_returns_mock_status():
    """Content containing '状态' (Chinese for status) returns mock status."""
    reply = build_claude_mock_reply("当前状态", "cm-s2")
    assert "mock status" in reply["agentReplies"][0]["content"]


def test_progress_prompt_returns_mock_status():
    """Content containing '进度' (Chinese for progress) returns mock status."""
    reply = build_claude_mock_reply("查看进度", "cm-s3")
    assert "mock status" in reply["agentReplies"][0]["content"]


def test_test_prompt_returns_test_intent():
    """Content containing 'test' returns test-intent reply."""
    reply = build_claude_mock_reply("run tests", "cm-t1")
    entry = reply["agentReplies"][0]
    assert "测试范围" in entry["content"]
    assert "mock adapter" in entry["content"]
    assert "没有真实执行" in entry["content"]


def test_chinese_test_prompt_returns_test_intent():
    """Content containing '测试' (Chinese for test) returns test-intent reply."""
    reply = build_claude_mock_reply("运行测试", "cm-t2")
    assert "测试范围" in reply["agentReplies"][0]["content"]


def test_fix_prompt_returns_fix_intent():
    """Content containing 'fix' returns fix-intent reply."""
    reply = build_claude_mock_reply("fix the bug", "cm-f1")
    entry = reply["agentReplies"][0]
    assert "定位问题" in entry["content"]
    assert "mock adapter" in entry["content"]
    assert "没有修改文件" in entry["content"]


def test_chinese_fix_prompt_returns_fix_intent():
    """Content containing '修' (Chinese for fix) returns fix-intent reply."""
    reply = build_claude_mock_reply("修复bug", "cm-f2")
    assert "定位问题" in reply["agentReplies"][0]["content"]


def test_bug_prompt_returns_fix_intent():
    """Content containing 'bug' returns fix-intent reply."""
    reply = build_claude_mock_reply("there is a bug", "cm-f3")
    assert "定位问题" in reply["agentReplies"][0]["content"]


def test_default_prompt_echo_like():
    """Default prompt returns 'Claude Code Mock received: <content>'."""
    reply = build_claude_mock_reply("hello world", "cm-d1")
    entry = reply["agentReplies"][0]
    assert entry["content"] == "Claude Code Mock received: hello world"


def test_keyword_priority_order():
    """Keyword matching is in defined order (status > test > fix > default)."""
    # "status" is checked first in the code
    reply = build_claude_mock_reply("fix the test status", "cm-p1")
    assert "mock status" in reply["agentReplies"][0]["content"]


def test_remote_id_format():
    """remoteId is always claude-mock-<clientMessageId>."""
    reply = build_claude_mock_reply("any", "cm-abc-123")
    assert reply["agentReplies"][0]["remoteId"] == "claude-mock-cm-abc-123"


def test_created_at_ends_with_z():
    """createdAt is always a Z-suffixed ISO string."""
    reply = build_claude_mock_reply("any", "cm-z")
    assert reply["agentReplies"][0]["createdAt"].endswith("Z")


def test_workspace_name_in_status_reply():
    """Status reply includes the workspace_name parameter."""
    reply = build_claude_mock_reply(
        "status", "cm-w1", workspace_name="my-project",
    )
    assert "workspace=my-project" in reply["agentReplies"][0]["content"]


# ── build_registration_payload ─────────────────────────────────────


def test_registration_payload_includes_source_label():
    """Registration payload includes sourceLabel=claude-code-mock."""
    payload = build_registration_payload(
        session_id="claude-code-mock-1",
        remote_agent_id="claude-code-mock-1",
        display_name="Claude Code Mock - goal",
        description="Mock coding session in D:\\projects\\goal",
        callback_url="http://127.0.0.1:9011",
    )
    assert payload["sourceLabel"] == "claude-code-mock"
    assert payload["sessionId"] == "claude-code-mock-1"
    assert payload["remoteAgentId"] == "claude-code-mock-1"
    assert payload["displayName"] == "Claude Code Mock - goal"
    assert "D:\\projects\\goal" in payload["description"]
    assert payload["callbackUrl"] == "http://127.0.0.1:9011"


def test_registration_payload_workspace_derived_values():
    """Display name and description are derived from workspace."""
    payload = build_registration_payload(
        session_id="cc-mock",
        remote_agent_id="cc-mock",
        display_name="Claude Code Mock - myapp",
        description="Mock coding session in /home/user/myapp",
        callback_url="http://127.0.0.1:9011",
    )
    assert payload["displayName"] == "Claude Code Mock - myapp"
    assert "Mock coding session" in payload["description"]
    assert "/home/user/myapp" in payload["description"]


def test_single_reply_not_multiple():
    """Each prompt returns exactly one agent reply."""
    for content in ("status", "test", "fix bug", "hello"):
        reply = build_claude_mock_reply(content, "cm-x")
        assert len(reply["agentReplies"]) == 1


def test_sticker_keyword_triggers_sticker_attachment():
    """Content containing sticker/表情/happy returns a sticker attachment."""
    from tools.claude_code_mock_adapter import build_claude_mock_reply

    for keyword in ("sticker", "表情", "happy"):
        reply = build_claude_mock_reply(keyword, "cm-s")
        assert len(reply["agentReplies"]) == 1
        att = reply["agentReplies"][0].get("attachments", [])
        assert len(att) == 1
        assert att[0]["type"] == "sticker"
        assert att[0]["stickerId"] == "happy_01"


def test_sticker_reply_has_text_content():
    """Sticker reply still has text content alongside the attachment."""
    from tools.claude_code_mock_adapter import build_claude_mock_reply

    reply = build_claude_mock_reply("send me a sticker!", "cm-t")
    assert len(reply["agentReplies"]) == 1
    entry = reply["agentReplies"][0]
    assert isinstance(entry["content"], str) and entry["content"]
    assert len(entry.get("attachments", [])) == 1


def test_status_prompt_does_not_return_sticker():
    """'status' prompt does NOT trigger the sticker path."""
    from tools.claude_code_mock_adapter import build_claude_mock_reply

    reply = build_claude_mock_reply("status", "cm-n")
    assert len(reply["agentReplies"]) == 1
    assert reply["agentReplies"][0].get("attachments", []) == []


# ── build_empty_content_reply ───────────────────────────────────────


def test_empty_content_reply_with_image_attachment():
    """Empty content + image attachment returns a text acknowledgement."""
    reply = build_empty_content_reply(
        [{"type": "image", "attachmentId": "app_att_test", "mimeType": "image/png"}],
        "cm-img",
    )
    assert len(reply["agentReplies"]) == 1
    entry = reply["agentReplies"][0]
    assert "image attachment" in entry["content"]
    assert "cannot inspect" in entry["content"]
    assert entry.get("attachments") is None


def test_empty_content_reply_with_sticker_attachment():
    """Empty content + sticker attachment returns a text reply — no crash."""
    reply = build_empty_content_reply(
        [{"type": "sticker", "stickerId": "happy_01"}],
        "cm-stk",
    )
    assert len(reply["agentReplies"]) == 1
    entry = reply["agentReplies"][0]
    assert isinstance(entry["content"], str) and entry["content"]
    assert entry.get("attachments", []) == []


def test_empty_content_reply_without_images_returns_generic():
    """Non-image attachments (e.g. sticker only) get a generic ack."""
    reply = build_empty_content_reply(
        [{"type": "sticker", "stickerId": "laugh_01"}],
        "cm-gen",
    )
    entry = reply["agentReplies"][0]
    assert "received" in entry["content"].lower()
    assert "image" not in entry["content"].lower()


def test_empty_content_reply_has_required_fields():
    """Empty-content replies still have remoteId and createdAt."""
    reply = build_empty_content_reply(
        [{"type": "image", "attachmentId": "a"}], "cm-fields",
    )
    entry = reply["agentReplies"][0]
    assert entry["remoteId"].startswith("claude-mock-")
    assert entry["createdAt"].endswith("Z")


# ── sticker_happy_e2e regression ────────────────────────────────────


def test_sticker_happy_keyword_still_triggers_sticker_attachment():
    """Content containing 'happy' still returns a sticker (not overridden)."""
    reply = build_claude_mock_reply("happy birthday!", "cm-e2e")
    assert len(reply["agentReplies"]) == 1
    entry = reply["agentReplies"][0]
    assert len(entry.get("attachments", [])) == 1
    assert entry["attachments"][0]["type"] == "sticker"
    assert entry["attachments"][0]["stickerId"] == "happy_01"


# ── heartbeat URL helper ────────────────────────────────────────────


def test_build_heartbeat_url_defaults():
    """build_heartbeat_url constructs the correct Router endpoint."""
    url = build_heartbeat_url(
        router_base_url="http://127.0.0.1:8899/",
        session_id="mock-1",
    )
    assert url == "http://127.0.0.1:8899/api/app-agent-sessions/mock-1/heartbeat"


def test_build_heartbeat_url_no_trailing_slash():
    """Trailing slash is stripped from base URL."""
    url = build_heartbeat_url(
        router_base_url="http://10.0.2.2:8899",
        session_id="my-session",
    )
    assert url.endswith("/api/app-agent-sessions/my-session/heartbeat")
    assert not "//api" in url