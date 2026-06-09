"""Unit tests for conversation compression."""

import json
import pytest

from agent.compressor import _content_repr, _build_compress_messages, _strip_analysis


class TestContentRepr:
    def test_string_input(self):
        assert _content_repr("hello") == "hello"

    def test_empty_string(self):
        assert _content_repr("") == ""

    def test_text_block_in_list(self):
        content = [{"type": "text", "text": "hello world"}]
        assert "hello world" in _content_repr(content)

    def test_thinking_block_truncated(self):
        long_think = "x" * 500
        content = [{"type": "thinking", "thinking": long_think}]
        result = _content_repr(content)
        assert result.startswith("[thinking:")
        assert "..." in result
        assert len(result) < 500 + 20  # truncated + wrapper

    def test_thinking_block_short(self):
        content = [{"type": "thinking", "thinking": "brief thought"}]
        result = _content_repr(content)
        assert result == "[thinking: brief thought]"

    def test_tool_use_block(self):
        content = [{"type": "tool_use", "name": "send_text_message", "input": {}}]
        result = _content_repr(content)
        assert result == "[tool_use: send_text_message]"

    def test_tool_result_block(self):
        content = [{"type": "tool_result", "tool_use_id": "call_123", "content": "sent"}]
        result = _content_repr(content)
        assert "sent" in result

    def test_mixed_blocks(self):
        content = [
            {"type": "thinking", "thinking": "brief"},
            {"type": "tool_use", "name": "get_time"},
        ]
        result = _content_repr(content)
        assert "[thinking:" in result
        assert "[tool_use:" in result

    def test_non_dict_in_list(self):
        content = ["plain text", {"type": "text", "text": "structured"}]
        result = _content_repr(content)
        assert "plain text" in result
        assert "structured" in result

    def test_empty_list(self):
        assert _content_repr([]) == ""


class TestBuildCompressMessages:
    def test_simple_conversation(self):
        events = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
        ]
        result = _build_compress_messages(events)
        lines = result.split("\n")
        assert len(lines) == 2
        assert lines[0].startswith("[user]")
        assert lines[1].startswith("[assistant]")

    def test_filters_old_analysis_summary(self):
        events = [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "call_1", "content": "sent"},
                    {"type": "text", "text": "<analysis>\nold summary\n</analysis>\n<summary>\nblah\n</summary>"},
                ],
            },
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        ]
        result = _build_compress_messages(events)
        # The old summary block starting with <analysis> should be filtered
        assert "<analysis>" not in result
        assert "tool_result" in result  # tool_result block preserved

    def test_preserves_non_analysis_text(self):
        events = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "你好"},
                    {"type": "text", "text": "今天天气不错"},
                ],
            },
        ]
        result = _build_compress_messages(events)
        assert "你好" in result
        assert "今天天气不错" in result

    def test_handles_string_content(self):
        events = [
            {"role": "user", "content": "plain text message"},
        ]
        result = _build_compress_messages(events)
        assert "[user] plain text message" in result

    def test_empty_events(self):
        result = _build_compress_messages([])
        assert result == ""


class TestStripAnalysis:
    def test_extracts_summary_only(self):
        text = "<analysis>\nworking notes\n</analysis>\n<summary>\n1. User Info: test\n</summary>"
        result = _strip_analysis(text)
        assert "<analysis>" not in result
        assert "working notes" not in result
        assert "1. User Info: test" in result

    def test_fallback_no_tags(self):
        text = "plain summary without tags"
        result = _strip_analysis(text)
        assert result == "plain summary without tags"

    def test_multiline_summary(self):
        text = "<analysis>\nnotes\n</analysis>\n<summary>\n1. User:\n   - name: Bob\n2. Topics:\n   - weather\n</summary>"
        result = _strip_analysis(text)
        assert "name: Bob" in result
        assert "weather" in result

    def test_whitespace_handling(self):
        text = "<analysis>\n\n</analysis>\n<summary>\n  extracted  \n</summary>"
        result = _strip_analysis(text)
        assert result == "extracted"


class TestCompressConversationIntegration:
    """Integration tests with MemoryStore (no actual LLM calls)."""

    @pytest.mark.asyncio
    async def test_get_all_events_returns_full_history(self):
        from memory.store import MemoryStore
        from memory.conversation import ConversationContext
        import time

        store = MemoryStore(db_path=":memory:")
        cid = "test_user:default"
        await store.ensure_conversation("test_user", "default")

        # Create 2 turns
        tid1 = "turn_1"
        await store.begin_turn(tid1, cid, "hello")
        await store.append_event(tid1, "assistant", [{"type": "text", "text": "hi"}])
        await store.finish_turn(tid1)

        tid2 = "turn_2"
        await store.begin_turn(tid2, cid, "how are you")
        await store.append_event(tid2, "assistant", [{"type": "text", "text": "good"}])
        await store.finish_turn(tid2)

        events = await store.get_all_events_for_compress(cid)
        assert len(events) >= 4  # user + assistant for each turn
        await store.close()

    @pytest.mark.asyncio
    async def test_apply_compression_soft_deletes_and_sets_active(self):
        from memory.store import MemoryStore

        store = MemoryStore(db_path=":memory:")
        cid = "test_user:default"
        await store.ensure_conversation("test_user", "default")

        tid = "turn_1"
        # Simulate a full turn: user input → assistant → user(tool_result) → assistant(final)
        await store.begin_turn(tid, cid, "hello")
        # Assistant thinks then calls tool
        await store.append_event(tid, "assistant", [
            {"type": "thinking", "thinking": "brief"},
            {"type": "tool_use", "id": "call_x", "name": "send_text_message", "input": {"text": "hi"}},
        ])
        # User with tool_result
        user_content = [
            {"type": "tool_result", "tool_use_id": "call_x", "content": "sent"},
        ]
        await store.append_event(tid, "user", user_content)
        # Assistant final reply
        await store.append_event(tid, "assistant", [{"type": "text", "text": "hi there"}])
        await store.finish_turn(tid)

        ok = await store.apply_compression(cid, "conversation summary here")
        assert ok

        events_after = await store.get_recent_events(cid)
        # After compression: 2 active events (user with summary + assistant final)
        assert len(events_after) == 2

        # Verify summary is in the user message content
        user_ev = events_after[0]
        assert user_ev["role"] == "user"
        assert isinstance(user_ev["content"], list)
        text_blocks = [b for b in user_ev["content"] if isinstance(b, dict) and b.get("type") == "text"]
        assert any("conversation summary here" in str(b.get("text", "")) for b in text_blocks)

        assert events_after[1]["role"] == "assistant"

        # Verify last_compressed_at was updated
        conv = await store.get_conversation("test_user", "default")
        assert conv["last_compressed_at"] > 0

        await store.close()

    @pytest.mark.asyncio
    async def test_apply_compression_fails_without_tool_result(self):
        from memory.store import MemoryStore

        store = MemoryStore(db_path=":memory:")
        cid = "test_user:default"
        await store.ensure_conversation("test_user", "default")

        tid = "turn_1"
        await store.begin_turn(tid, cid, "hello")
        # Only assistant, no user with tool_result (other than seq=0 which is plain text)
        await store.append_event(tid, "assistant", [{"type": "text", "text": "hi"}])
        await store.finish_turn(tid)

        ok = await store.apply_compression(cid, "summary")
        assert not ok  # Should fail — no tool_result to attach to
        await store.close()

    @pytest.mark.asyncio
    async def test_rebuild_loads_only_active_events(self):
        from memory.store import MemoryStore
        from memory.conversation import ConversationContext

        store = MemoryStore(db_path=":memory:")
        cid = "test_user:default"
        await store.ensure_conversation("test_user", "default")

        tid = "turn_1"
        await store.begin_turn(tid, cid, "hello")
        await store.append_event(tid, "assistant", [{"type": "text", "text": "hi"}])
        await store.finish_turn(tid)

        # Soft-delete the assistant event
        db = await store._get_db()
        await db.execute("UPDATE raw_events SET active=0 WHERE turn_id=?", (tid,))
        await db.commit()

        ctx = ConversationContext(user_id="test_user", bot_id="default")
        await ctx.rebuild(store)
        assert len(ctx.messages) == 0  # All inactive

        await store.close()

    @pytest.mark.asyncio
    async def test_clear_all_resets_everything(self):
        from memory.store import MemoryStore

        store = MemoryStore(db_path=":memory:")
        cid = "test_user:default"
        await store.ensure_conversation("test_user", "default")

        tid = "turn_1"
        await store.begin_turn(tid, cid, "hello")
        await store.append_event(tid, "assistant", [{"type": "text", "text": "hi"}])
        await store.finish_turn(tid)

        await store.clear_all()
        events = await store.get_recent_events(cid)
        assert len(events) == 0

        # Contact state should survive
        await store.update_contact_state("test_user", "token123", "default")
        contact = await store.get_contact_state()
        assert contact["user_id"] == "test_user"

        await store.close()
