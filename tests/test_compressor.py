"""Unit tests for conversation compression."""

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from agent.compressor import (
    _filter_summary_blocks,
    _build_compression_call,
    _extract_summary,
    _extract_memory_operations,
    _in_compress_window,
    validate_memory_operations,
    COMPRESSION_PROMPT,
)


class TestFilterSummaryBlocks:
    def test_filters_summary_block(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "<summary>\nold summary\n</summary>"},
                    {"type": "text", "text": "<channel>hello</channel>"},
                ],
            },
        ]
        result = _filter_summary_blocks(messages)
        assert len(result) == 1
        texts = [b["text"] for b in result[0]["content"] if b.get("type") == "text"]
        assert "<summary>" not in " ".join(texts)
        assert "hello" in texts[0]

    def test_skips_message_if_all_blocks_filtered(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "<summary>\nold\n</summary>"},
                ],
            },
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        ]
        result = _filter_summary_blocks(messages)
        assert len(result) == 1  # user message skipped, only assistant remains
        assert result[0]["role"] == "assistant"

    def test_preserves_non_summary_blocks(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "call_x", "content": "sent"},
                    {"type": "text", "text": "normal text"},
                ],
            },
        ]
        result = _filter_summary_blocks(messages)
        assert len(result) == 1
        assert len(result[0]["content"]) == 2  # both blocks preserved

    def test_passes_through_plain_text_content(self):
        messages = [
            {"role": "user", "content": "plain text"},
            {"role": "assistant", "content": [{"type": "text", "text": "reply"}]},
        ]
        result = _filter_summary_blocks(messages)
        assert len(result) == 2

    def test_handles_empty_list(self):
        result = _filter_summary_blocks([])
        assert result == []


class TestBuildCompressionCall:
    def test_appends_fake_user_message(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        ]
        result = _build_compression_call(messages)
        assert len(result) == 3
        assert result[-1]["role"] == "user"
        assert result[-1]["content"] == COMPRESSION_PROMPT

    def test_filters_summary_before_appending(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "<summary>\nold\n</summary>"},
                ],
            },
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        ]
        result = _build_compression_call(messages)
        # user with only summary filtered out → only assistant + fake user
        assert len(result) == 2
        assert result[0]["role"] == "assistant"


class TestExtractSummary:
    def test_extracts_summary_block(self):
        text = "<analysis>\nnotes\n</analysis>\n<summary>\n1. User Identity & Preferences: test user with long enough summary text\n</summary>"
        result = _extract_summary(text)
        assert "1. User Identity & Preferences" in result

    def test_rejects_short_summary(self):
        text = "<analysis>\nx\n</analysis>\n<summary>\nshort\n</summary>"
        result = _extract_summary(text)
        assert result is None

    def test_fallback_no_tags(self):
        text = "just some plain text that is long enough to pass the length check"
        result = _extract_summary(text)
        assert result is None  # No <summary> tag → not extractable

    def test_multiline_summary(self):
        text = "<analysis>\nnotes\n</analysis>\n<summary>\n1. User:\n   - name: Bob\n2. Topics:\n   - weather\n</summary>"
        result = _extract_summary(text)
        assert "name: Bob" in result
        assert "weather" in result


class TestMemoryOperations:
    def test_extracts_empty_operations(self):
        assert _extract_memory_operations(
            "<memory_operations>[]</memory_operations>"
        ) == []

    def test_rejects_delete_and_missing_evidence(self):
        delete = (
            '<memory_operations>[{"command":"delete","path":"topic.md",'
            '"reason":"old","evidence":"quote"}]</memory_operations>'
        )
        assert _extract_memory_operations(delete) is None
        missing = (
            '<memory_operations>[{"command":"create","path":"topic.md",'
            '"file_text":"# Topic","reason":"stable"}]</memory_operations>'
        )
        assert _extract_memory_operations(missing) is None

    def test_requires_user_evidence_and_index_for_create(self):
        raw = (
            '<memory_operations>['
            '{"command":"create","path":"user-background.md",'
            '"file_text":"# 用户背景","reason":"stable","evidence":"我住在成都"},'
            '{"command":"str_replace","path":"MEMORY.md",'
            '"old_str":"# 长期记忆索引",'
            '"new_str":"# 长期记忆索引\\n- [背景](user-background.md) - 成都",'
            '"reason":"index","evidence":"我住在成都"}'
            ']</memory_operations>'
        )
        operations = _extract_memory_operations(raw)
        assert operations is not None
        assert validate_memory_operations(
            [{"role": "user", "content": "我住在成都"}], operations
        ) is True
        assert validate_memory_operations(
            [{"role": "user", "content": "别的内容"}], operations
        ) is False


class TestInCompressWindow:
    TZ = timezone(timedelta(hours=8))

    def _dt(self, hour: int, minute: int) -> datetime:
        return datetime(2026, 6, 10, hour, minute, 0, tzinfo=self.TZ)

    def test_inside_window_0400(self):
        assert _in_compress_window(self._dt(4, 0)) is True

    def test_inside_window_0356(self):
        assert _in_compress_window(self._dt(3, 56)) is True

    def test_inside_window_0424(self):
        assert _in_compress_window(self._dt(4, 24)) is True

    def test_before_window_0355(self):
        assert _in_compress_window(self._dt(3, 55)) is False

    def test_after_window_0425(self):
        assert _in_compress_window(self._dt(4, 25)) is False

    def test_midday(self):
        assert _in_compress_window(self._dt(11, 44)) is False

    def test_midnight(self):
        assert _in_compress_window(self._dt(0, 0)) is False


class TestApplyCompression:
    """Integration tests for the new apply_compression (takes compressed_messages list)."""

    @pytest.mark.asyncio
    async def test_writes_compressed_messages_and_soft_deletes_old(self):
        from memory.store import MemoryStore

        store = MemoryStore(db_path=":memory:")
        cid = "test_user:default"
        await store.ensure_conversation("test_user", "default")

        # Create some original events
        tid = "turn_1"
        await store.begin_turn(tid, cid, "hello")
        await store.append_event(tid, "assistant", [{"type": "text", "text": "hi"}])
        await store.finish_turn(tid)

        # Apply compression with the new compressed_messages format
        compressed = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "<summary>\ncompressed content\n</summary>"},
                {"type": "text", "text": "<channel>new message</channel>"},
            ],
        }]
        ok = await store.apply_compression(cid, compressed)
        assert ok

        # Verify active events are the compressed ones
        events = await store.get_recent_events(cid)
        assert len(events) == 1
        assert events[0]["role"] == "user"
        assert isinstance(events[0]["content"], list)
        texts = [b["text"] for b in events[0]["content"] if b.get("type") == "text"]
        assert any("compressed content" in t for t in texts)
        assert any("new message" in t for t in texts)

        await store.close()

    @pytest.mark.asyncio
    async def test_update_last_compressed_at(self):
        from memory.store import MemoryStore

        store = MemoryStore(db_path=":memory:")
        cid = "test_user:default"
        await store.ensure_conversation("test_user", "default")

        tid = "turn_1"
        await store.begin_turn(tid, cid, "hello")
        await store.append_event(tid, "assistant", [{"type": "text", "text": "hi"}])
        await store.finish_turn(tid)

        await store.apply_compression(cid, [{"role": "user", "content": "summary"}])
        conv = await store.get_conversation("test_user", "default")
        assert conv["last_compressed_at"] > 0

        await store.close()

    @pytest.mark.asyncio
    async def test_commit_compression_writes_history_and_daily_summary_together(self):
        from memory.store import MemoryStore

        store = MemoryStore(db_path=":memory:")
        cid = "test_user:default"
        await store.ensure_conversation("test_user", "default")
        tid = "turn_1"
        await store.begin_turn(tid, cid, "hello")
        compressed = [{
            "role": "user",
            "content": [{"type": "text", "text": "<summary>\nnew state\n</summary>"}],
        }]
        await store.commit_compression(
            cid,
            compressed,
            "2026-06-10",
            "<summary>\nnew state\n</summary>",
        )
        events = await store.get_recent_events(cid)
        summaries = await store.get_daily_summaries(cid)
        assert "new state" in events[0]["content"][0]["text"]
        assert summaries[0]["summary_date"] == "2026-06-10"
        await store.close()

    @pytest.mark.asyncio
    async def test_rebuild_loads_compressed_state(self):
        from memory.store import MemoryStore
        from memory.conversation import ConversationContext

        store = MemoryStore(db_path=":memory:")
        cid = "test_user:default"
        await store.ensure_conversation("test_user", "default")

        tid = "turn_1"
        await store.begin_turn(tid, cid, "hello")
        await store.append_event(tid, "assistant", [{"type": "text", "text": "hi"}])
        await store.finish_turn(tid)

        compressed = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "<summary>\ncompressed\n</summary>"},
                {"type": "text", "text": "<channel>msg</channel>"},
            ],
        }]
        await store.apply_compression(cid, compressed)

        ctx = ConversationContext(user_id="test_user", bot_id="default")
        await ctx.rebuild(store)
        assert len(ctx.messages) == 1
        assert ctx.messages[0]["role"] == "user"
        assert len(ctx.messages[0]["content"]) == 2

        await store.close()

    @pytest.mark.asyncio
    async def test_old_events_inactive_after_compression(self):
        from memory.store import MemoryStore

        store = MemoryStore(db_path=":memory:")
        cid = "test_user:default"
        await store.ensure_conversation("test_user", "default")

        tid = "turn_1"
        await store.begin_turn(tid, cid, "hello")
        await store.append_event(tid, "assistant", [{"type": "text", "text": "hi"}])
        await store.finish_turn(tid)

        await store.apply_compression(cid, [{"role": "user", "content": "summary"}])

        # get_recent_events only returns active=1, so we verify only 1 event
        events = await store.get_recent_events(cid)
        assert len(events) == 1

        await store.close()


class TestReplaceWithCompressed:
    def test_stores_summary_as_single_block(self):
        from memory.conversation import ConversationContext

        ctx = ConversationContext(user_id="test", bot_id="default")
        ctx.replace_with_compressed("compressed summary")
        assert len(ctx.messages) == 1
        assert ctx.messages[0]["role"] == "user"
        content = ctx.messages[0]["content"]
        assert len(content) == 1
        assert content[0]["type"] == "text"
        assert "compressed summary" in content[0]["text"]
        assert content[0]["text"].startswith("<summary>")

    def test_overwrites_existing_messages(self):
        from memory.conversation import ConversationContext

        ctx = ConversationContext(user_id="test", bot_id="default")
        ctx.add_event("user", "old message")
        ctx.add_event("assistant", [{"type": "text", "text": "old reply"}])
        assert len(ctx.messages) == 2

        ctx.replace_with_compressed("new summary")
        assert len(ctx.messages) == 1
        assert "new summary" in ctx.messages[0]["content"][0]["text"]


class TestIsSummaryOnlyUserMessage:
    def test_match(self):
        from agent.runtime_context_builder import ContextBuilder
        msg = {
            "role": "user",
            "content": [{"type": "text", "text": "<summary>\nblah\n</summary>"}],
        }
        assert ContextBuilder._is_summary_only_user_message(msg) is True

    def test_wrong_role(self):
        from agent.runtime_context_builder import ContextBuilder
        msg = {
            "role": "assistant",
            "content": [{"type": "text", "text": "<summary>\nblah\n</summary>"}],
        }
        assert ContextBuilder._is_summary_only_user_message(msg) is False

    def test_multiple_blocks(self):
        from agent.runtime_context_builder import ContextBuilder
        msg = {
            "role": "user",
            "content": [
                {"type": "text", "text": "<summary>\nblah\n</summary>"},
                {"type": "text", "text": "<channel>msg</channel>"},
            ],
        }
        assert ContextBuilder._is_summary_only_user_message(msg) is False

    def test_not_summary_text(self):
        from agent.runtime_context_builder import ContextBuilder
        msg = {
            "role": "user",
            "content": [{"type": "text", "text": "hello"}],
        }
        assert ContextBuilder._is_summary_only_user_message(msg) is False

    def test_non_text_block(self):
        from agent.runtime_context_builder import ContextBuilder
        msg = {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "x", "content": "sent"}],
        }
        assert ContextBuilder._is_summary_only_user_message(msg) is False

    def test_string_content(self):
        from agent.runtime_context_builder import ContextBuilder
        msg = {"role": "user", "content": "<summary>\nblah\n</summary>"}
        assert ContextBuilder._is_summary_only_user_message(msg) is False


class TestClearAll:
    """Verify clear_all still works correctly after compression changes."""

    @pytest.mark.asyncio
    async def test_clear_all_preserves_contact_state(self):
        from memory.store import MemoryStore

        store = MemoryStore(db_path=":memory:")
        await store.ensure_conversation("test_user", "default")

        # Write some events + compress
        tid = "turn_1"
        cid = "test_user:default"
        await store.begin_turn(tid, cid, "hello")
        await store.append_event(tid, "assistant", [{"type": "text", "text": "hi"}])
        await store.finish_turn(tid)
        await store.apply_compression(cid, [{"role": "user", "content": "summary"}])

        await store.clear_all()
        events = await store.get_recent_events(cid)
        assert len(events) == 0

        # Contact state should survive
        await store.update_contact_state("test_user", "token123", "default")
        contact = await store.get_contact_state()
        assert contact["user_id"] == "test_user"

        await store.close()
