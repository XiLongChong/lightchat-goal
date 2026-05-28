"""Tests for MessageEnvelope (Phase 4A)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from agent.message_envelope import (
    MessageEnvelope,
    message_envelopes_to_provider_messages,
)
from agent.llm_types import LLMResponse, LLMUsage


# ── helpers ────────────────────────────────────────────────────────

def _make_llm_response(**kw) -> LLMResponse:
    return LLMResponse(
        content=kw.get("content", "hello"),
        provider=kw.get("provider", "deepseek"),
        model=kw.get("model", "deepseek-v4-flash"),
        usage=kw.get("usage", LLMUsage()),
        latency_ms=kw.get("latency_ms", 100),
        finish_reason=kw.get("finish_reason", "stop"),
        reasoning_content=kw.get("reasoning_content", ""),
        tool_calls=kw.get("tool_calls"),
        request_id=kw.get("request_id", ""),
        raw_response=kw.get("raw_response"),
    )


# ══════════════════════════════════════════════════════════════════
# 1. Basic construction and legacy history
# ══════════════════════════════════════════════════════════════════

class TestMessageEnvelopeBasic:
    def test_legacy_user_row_to_provider_message(self):
        row = {"role": "user", "content": "你好", "ts": 1716876543.0, "source": "user"}
        env = MessageEnvelope.from_legacy_history(row)
        msg = env.to_openai_message()

        assert msg["role"] == "user"
        assert msg["content"] == "你好"
        assert "ts" not in msg
        assert "source" not in msg
        assert "provider_private" not in msg
        assert "metadata" not in msg

    def test_legacy_assistant_row_to_provider_message(self):
        row = {
            "role": "assistant",
            "content": '{"actions":[{"type":"message","text":"hi"}]}',
            "ts": 1716876544.0,
        }
        env = MessageEnvelope.from_legacy_history(row)
        msg = env.to_openai_message()

        assert msg["role"] == "assistant"
        assert msg["content"] == row["content"]  # kept as-is, no parsing
        assert "ts" not in msg

    def test_legacy_row_preserves_ts_and_source(self):
        row = {"role": "user", "content": "hello", "ts": 1716876543.0, "source": "proactive"}
        env = MessageEnvelope.from_legacy_history(row)
        assert env.ts == 1716876543.0
        assert env.source == "proactive"

    def test_legacy_row_invalid_role_defaults_to_user(self):
        row = {"role": "unknown_role", "content": "test"}
        env = MessageEnvelope.from_legacy_history(row)
        assert env.role == "user"

    def test_legacy_row_missing_content(self):
        row = {"role": "user"}
        env = MessageEnvelope.from_legacy_history(row)
        assert env.content is None

    def test_system_message_to_provider_message(self):
        env = MessageEnvelope(role="system", content="You are a helpful assistant.")
        msg = env.to_openai_message()
        assert msg["role"] == "system"
        assert msg["content"] == "You are a helpful assistant."

    def test_name_field_preserved(self):
        env = MessageEnvelope(role="user", content="hello", name="alice")
        msg = env.to_openai_message()
        assert msg["name"] == "alice"

    def test_name_absent_when_none(self):
        env = MessageEnvelope(role="user", content="hello")
        msg = env.to_openai_message()
        assert "name" not in msg

    def test_metadata_not_in_provider_message(self):
        env = MessageEnvelope(
            role="user",
            content="hello",
            metadata={"request_id": "abc", "turn_id": "t1"},
        )
        msg = env.to_openai_message()
        assert "metadata" not in msg
        assert "request_id" not in msg

    def test_provider_private_not_in_provider_message(self):
        env = MessageEnvelope(
            role="assistant",
            content="hello",
            provider_private={
                "provider": "deepseek",
                "model": "v4",
                "reasoning_content": "internal thought",
                "raw_message": object(),
            },
        )
        msg = env.to_openai_message()
        assert "provider_private" not in msg
        assert "reasoning_content" not in msg
        assert "raw_message" not in str(msg)

    def test_reasoning_content_not_in_plain_content(self):
        env = MessageEnvelope(
            role="assistant",
            content="user visible reply",
            provider_private={"reasoning_content": "secret chain of thought"},
        )
        content = env.content or ""
        assert "secret chain of thought" not in content


# ══════════════════════════════════════════════════════════════════
# 2. Tool-call reserved structure
# ══════════════════════════════════════════════════════════════════

class TestMessageEnvelopeToolCalls:
    def test_assistant_with_tool_calls_in_provider_message(self):
        tool_calls = [
            {
                "id": "call_001",
                "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city":"成都"}'},
            }
        ]
        env = MessageEnvelope(role="assistant", content=None, tool_calls=tool_calls)
        msg = env.to_openai_message()

        assert msg["role"] == "assistant"
        assert msg["tool_calls"] == tool_calls
        assert msg.get("content") is None  # tool_calls often have null content

    def test_tool_result_envelope(self):
        env = MessageEnvelope(
            role="tool",
            tool_call_id="call_001",
            content='{"temperature": 25, "condition": "sunny"}',
        )
        msg = env.to_openai_message()

        assert msg["role"] == "tool"
        assert msg["tool_call_id"] == "call_001"
        assert msg["content"] == '{"temperature": 25, "condition": "sunny"}'

    def test_tool_role_without_tool_call_id_is_invalid(self):
        env = MessageEnvelope(role="tool", content="result")
        errors = env.validate()
        assert any("tool_call_id" in e for e in errors)

    def test_tool_call_id_on_non_tool_role_is_invalid(self):
        env = MessageEnvelope(role="assistant", tool_call_id="call_001")
        errors = env.validate()
        assert any("tool_call_id" in e for e in errors)

    def test_valid_envelope_no_errors(self):
        env = MessageEnvelope(role="user", content="hello")
        assert env.validate() == []


# ══════════════════════════════════════════════════════════════════
# 3. LLMResponse → MessageEnvelope
# ══════════════════════════════════════════════════════════════════

class TestMessageEnvelopeFromLLMResponse:
    def test_content_becomes_assistant_content(self):
        resp = _make_llm_response(content="你好呀")
        env = MessageEnvelope.from_llm_response(resp)
        assert env.role == "assistant"
        assert env.content == "你好呀"

    def test_reasoning_content_in_provider_private(self):
        resp = _make_llm_response(content="ok", reasoning_content="Let me think...")
        env = MessageEnvelope.from_llm_response(resp)
        assert env.provider_private["reasoning_content"] == "Let me think..."
        assert env.content == "ok"
        # reasoning_content must not leak into content
        assert "Let me think" not in (env.content or "")

    def test_tool_calls_copied_to_envelope(self):
        tool_calls = [{"id": "c1", "type": "function", "function": {"name": "f"}}]
        resp = _make_llm_response(content=None, tool_calls=tool_calls)
        env = MessageEnvelope.from_llm_response(resp)
        assert env.tool_calls == tool_calls

    def test_provider_and_model_in_private(self):
        resp = _make_llm_response(provider="deepseek", model="deepseek-v4-pro")
        env = MessageEnvelope.from_llm_response(resp)
        assert env.provider_private["provider"] == "deepseek"
        assert env.provider_private["model"] == "deepseek-v4-pro"

    def test_raw_response_not_in_envelope_content(self):
        raw = {"choices": [{"message": {"content": "secret"}}]}
        resp = _make_llm_response(content="public", raw_response=raw)
        env = MessageEnvelope.from_llm_response(resp)
        assert env.content == "public"
        # raw_response should NOT be in provider_private
        assert "raw_response" not in env.provider_private
        # nor in content
        assert "secret" not in (env.content or "")

    def test_null_tool_calls_remains_none(self):
        resp = _make_llm_response(content="ok", tool_calls=None)
        env = MessageEnvelope.from_llm_response(resp)
        assert env.tool_calls is None

    def test_to_provider_message_does_not_include_private(self):
        resp = _make_llm_response(
            content="hello",
            reasoning_content="secret thoughts",
        )
        env = MessageEnvelope.from_llm_response(resp)
        msg = env.to_openai_message()
        assert "provider_private" not in msg
        assert "reasoning_content" not in msg
        assert "secret" not in str(msg)


# ══════════════════════════════════════════════════════════════════
# 4. Batch conversion helper
# ══════════════════════════════════════════════════════════════════

class TestBatchConversion:
    def test_deepseek_provider(self):
        envelopes = [
            MessageEnvelope(role="system", content="sys"),
            MessageEnvelope(role="user", content="hello"),
            MessageEnvelope(role="assistant", content="hi"),
        ]
        msgs = message_envelopes_to_provider_messages(envelopes, provider="deepseek")
        assert len(msgs) == 3
        assert msgs[0] == {"role": "system", "content": "sys"}
        assert msgs[1] == {"role": "user", "content": "hello"}
        assert msgs[2] == {"role": "assistant", "content": "hi"}

    def test_openai_provider(self):
        envelopes = [MessageEnvelope(role="user", content="q")]
        msgs = message_envelopes_to_provider_messages(envelopes, provider="openai")
        assert msgs == [{"role": "user", "content": "q"}]

    def test_unknown_provider_falls_back(self):
        envelopes = [MessageEnvelope(role="user", content="q")]
        msgs = message_envelopes_to_provider_messages(envelopes, provider="anthropic")
        assert msgs == [{"role": "user", "content": "q"}]

    def test_empty_list(self):
        assert message_envelopes_to_provider_messages([], provider="deepseek") == []


# ══════════════════════════════════════════════════════════════════
# 5. Safety: provider_private / reasoning_content isolation
# ══════════════════════════════════════════════════════════════════

class TestMessageEnvelopeSafety:
    def test_provider_private_not_in_serialized_dict(self):
        """to_openai_message must never include provider_private."""
        env = MessageEnvelope(
            role="assistant",
            content="public",
            provider_private={"secret": "value"},
        )
        msg = env.to_openai_message()
        assert "provider_private" not in msg
        assert "secret" not in msg

    def test_metadata_not_in_serialized_dict(self):
        env = MessageEnvelope(
            role="user",
            content="hello",
            metadata={"internal_id": "abc123"},
        )
        msg = env.to_openai_message()
        assert "metadata" not in msg
        assert "internal_id" not in msg

    def test_to_openai_message_strips_ts_and_source(self):
        row = {"role": "user", "content": "hi", "ts": 1.5, "source": "proactive"}
        env = MessageEnvelope.from_legacy_history(row)
        msg = env.to_openai_message()
        assert "ts" not in msg
        assert "source" not in msg

    def test_cannot_accidentally_access_provider_private_via_content(self):
        """No path from content should expose provider_private data."""
        env = MessageEnvelope(
            role="assistant",
            content="normal reply",
            provider_private={"reasoning_content": "hidden"},
        )
        # The envelope's own content field is separate
        assert env.content == "normal reply"
        # And the provider message only has role + content
        msg = env.to_openai_message()
        assert set(msg.keys()).issubset({"role", "content"})


# ══════════════════════════════════════════════════════════════════
# 5b. Mutable default isolation (Phase 4B review)
# ══════════════════════════════════════════════════════════════════

class TestMessageEnvelopeMutableDefaults:
    def test_provider_private_not_shared_between_instances(self):
        env1 = MessageEnvelope(role="user", content="a")
        env2 = MessageEnvelope(role="user", content="b")

        env1.provider_private["key"] = "only_env1"
        assert "key" not in env2.provider_private
        assert env1.provider_private["key"] == "only_env1"

    def test_metadata_not_shared_between_instances(self):
        env1 = MessageEnvelope(role="user", content="a")
        env2 = MessageEnvelope(role="user", content="b")

        env1.metadata["request_id"] = "req-1"
        assert "request_id" not in env2.metadata
        assert env1.metadata["request_id"] == "req-1"

    def test_provider_private_uses_default_factory(self):
        env = MessageEnvelope(role="user", content="test")
        assert isinstance(env.provider_private, dict)
        assert env.provider_private == {}

    def test_metadata_uses_default_factory(self):
        env = MessageEnvelope(role="user", content="test")
        assert isinstance(env.metadata, dict)
        assert env.metadata == {}


# ══════════════════════════════════════════════════════════════════
# 6. ChatAgent message equivalence (without runtime wiring)
# ══════════════════════════════════════════════════════════════════

class TestChatAgentMessageEquivalence:
    """Verify that envelopes produce messages equivalent to legacy dicts."""

    def test_system_message_roundtrip(self):
        legacy = {"role": "system", "content": "You are helpful."}
        env = MessageEnvelope.from_legacy_history(legacy)
        msg = env.to_openai_message()
        assert msg == legacy

    def test_user_message_roundtrip(self):
        legacy = {"role": "user", "content": "hello world", "ts": 100.0}
        env = MessageEnvelope.from_legacy_history(legacy)
        msg = env.to_openai_message()
        assert msg["role"] == "user"
        assert msg["content"] == "hello world"

    def test_assistant_message_roundtrip(self):
        legacy = {
            "role": "assistant",
            "content": '{"actions":[{"type":"message","text":"hi"}]}',
        }
        env = MessageEnvelope.from_legacy_history(legacy)
        msg = env.to_openai_message()
        assert msg["role"] == "assistant"
        assert msg["content"] == legacy["content"]  # unchanged

    def test_large_batch_equivalence(self):
        """A batch of 20 mixed messages produces equivalent output."""
        legacy_messages = []
        for i in range(20):
            if i % 3 == 0:
                legacy_messages.append({"role": "system", "content": f"sys {i}"})
            elif i % 3 == 1:
                legacy_messages.append({"role": "user", "content": f"user {i}"})
            else:
                legacy_messages.append({"role": "assistant", "content": f"asst {i}"})

        envelopes = [MessageEnvelope.from_legacy_history(m) for m in legacy_messages]
        provider_msgs = message_envelopes_to_provider_messages(envelopes)

        for i, (legacy, msg) in enumerate(zip(legacy_messages, provider_msgs)):
            assert msg["role"] == legacy["role"], f"mismatch at index {i}"
            assert msg["content"] == legacy["content"], f"mismatch at index {i}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
