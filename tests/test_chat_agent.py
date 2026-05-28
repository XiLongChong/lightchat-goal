"""Test JSON parser — Safeguard #1: parse fallback on invalid JSON."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.json_parser import safe_parse_json, parse_agent_output, extract_json


def test_extract_json_from_code_block():
    """Extract JSON from markdown code block."""
    raw = '```json\n{"actions": [], "done": true}\n```'
    result = extract_json(raw)
    assert result == '{"actions": [], "done": true}'


def test_extract_json_bare():
    """Extract bare JSON."""
    raw = '{"actions":[],"done":true}'
    result = extract_json(raw)
    assert result == raw


def test_extract_json_with_text_around():
    """Extract JSON surrounded by text."""
    raw = '好的，以下是回复：\n{"actions":[{"type":"message","text":"你好"}],"done":true}'
    result = extract_json(raw)
    assert result == '{"actions":[{"type":"message","text":"你好"}],"done":true}'


def test_safe_parse_valid_json():
    """Valid JSON parses correctly."""
    raw = '{"actions": [{"type": "message", "text": "hello"}], "done": true}'
    result = safe_parse_json(raw)
    assert result is not None
    assert result["done"] is True
    assert len(result["actions"]) == 1


def test_safe_parse_invalid_json_returns_none():
    """Invalid JSON returns None (caller handles fallback)."""
    raw = "这不是合法的JSON {{{"
    result = safe_parse_json(raw)
    assert result is None


def test_safe_parse_empty_string_returns_none():
    """Empty string returns None."""
    result = safe_parse_json("")
    assert result is None


def test_safe_parse_trailing_comma_fix():
    """Trailing comma before } is fixed."""
    raw = '{"actions": [{"type": "message", "text": "hi"},], "done": true,}'
    result = safe_parse_json(raw)
    assert result is not None
    assert result["done"] is True


def test_parse_agent_output_fallback():
    """parse_agent_output always returns valid structure, even on garbage."""
    result = parse_agent_output("不是JSON的输出文本")
    assert result["done"] is True
    assert len(result["actions"]) == 1
    assert result["actions"][0]["type"] == "message"
    assert "不是JSON的输出文本" in result["actions"][0]["text"]


def test_parse_agent_output_no_actions_field():
    """Missing actions field → fallback."""
    result = parse_agent_output('{"done": true}')
    assert result["done"] is True
    assert len(result["actions"]) == 1
    assert result["actions"][0]["type"] == "message"


def test_parse_agent_output_preserves_valid_json():
    """Valid JSON with long text is preserved as-is (truncation is Chat Agent's job)."""
    long_text = "啊" * 600
    raw = f'{{"actions":[{{"type":"message","text":"{long_text}"}}],"done":true}}'
    result = parse_agent_output(raw)
    # JSON parser preserves the content; truncation is Safeguard #2 in chat_agent.py
    assert result["done"] is True
    assert len(result["actions"]) == 1
    assert result["actions"][0]["text"] == long_text


# ================================================================
# Phase 2A: ChatAgent + LLMGateway integration tests
# ================================================================
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.llm_types import LLMError, LLMRequest, LLMResponse, LLMUsage


def _make_mock_gateway_response(content: str = "", **kw):
    """Build a minimal LLMResponse from a FakeGateway."""
    return LLMResponse(
        content=content,
        provider="deepseek",
        model="deepseek-v4-flash",
        usage=LLMUsage(
            prompt_tokens=kw.get("prompt_tokens", 10),
            completion_tokens=kw.get("completion_tokens", 5),
            total_tokens=kw.get("total_tokens", 15),
            cache_hit_tokens=kw.get("cache_hit_tokens", 0),
            cache_miss_tokens=kw.get("cache_miss_tokens", 10),
            reasoning_tokens=kw.get("reasoning_tokens", 0),
            estimated=kw.get("estimated", False),
        ),
        latency_ms=kw.get("latency_ms", 100),
        finish_reason=kw.get("finish_reason", "stop"),
    )


class FakeGatewayForChat:
    """LLMGateway stub that records requests and returns canned responses."""

    def __init__(self, response: LLMResponse | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if self.error:
            raise self.error
        if self.response:
            return self.response
        return _make_mock_gateway_response(
            json.dumps({"actions": [{"type": "message", "text": "hello", "delay": 0}], "done": True},
                       ensure_ascii=False)
        )


# -------------------------------------------------------------------
# 1. ChatAgent uses LLMGateway for normal chat
# -------------------------------------------------------------------
class TestChatAgentGatewayNormalChat:
    def test_chat_agent_yields_message_via_gateway(self):
        """ChatAgent produces a message action when gateway returns valid JSON."""
        from agent.chat_agent import ChatAgent

        gateway = FakeGatewayForChat(response=_make_mock_gateway_response(
            '{"actions":[{"type":"message","text":"你好呀","delay":0}],"done":true}'
        ))
        agent = ChatAgent(
            model="test", persona="test persona",
            personality="casual", temperature=0.7,
            llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="你好", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = __import__("asyncio").run(_run())
        assert len(result["messages"]) >= 1
        assert result["messages"][0]["text"] == "你好呀"
        assert result["done"] is True
        assert result["skip_reply"] is False
        assert result["llm_status"] == "ok"

    def test_chat_agent_gateway_request_has_correct_metadata(self):
        """LLMRequest passed to gateway carries purpose='chat_turn' and json_object mode by default."""
        from agent.chat_agent import ChatAgent

        gateway = FakeGatewayForChat(response=_make_mock_gateway_response(
            '{"actions":[{"type":"message","text":"ok","delay":0}],"done":true}'
        ))
        agent = ChatAgent(
            model="deepseek-v4-flash", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="hi", memories=[], history=[],
                relationship={}, trigger="user",
            )

        __import__("asyncio").run(_run())
        assert len(gateway.requests) >= 1
        req = gateway.requests[0]
        assert req.purpose == "chat_turn"
        assert req.response_mode == "json_object"
        assert req.thinking == "disabled"
        assert req.tools is None
        assert req.model == "deepseek-v4-flash"


# -------------------------------------------------------------------
# 2. JSON actions parsing unchanged after gateway migration
# -------------------------------------------------------------------
class TestChatAgentGatewayJsonParsing:
    def test_gateway_content_parses_actions_same_as_raw_client(self):
        """The JSON actions parse path is unchanged regardless of gateway client."""
        from agent.chat_agent import ChatAgent

        gateway = FakeGatewayForChat(response=_make_mock_gateway_response(
            '{"actions":[{"type":"message","text":"hi"},{"type":"message","text":"there"}],"done":true}'
        ))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="hello", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = __import__("asyncio").run(_run())
        assert len(result["messages"]) == 2
        assert result["messages"][0]["text"] == "hi"
        assert result["messages"][1]["text"] == "there"

    def test_gateway_skip_action_preserved(self):
        """Skip/no_reply action in JSON still works with gateway."""
        from agent.chat_agent import ChatAgent

        gateway = FakeGatewayForChat(response=_make_mock_gateway_response(
            '{"actions":[{"type":"skip","reason":"no need"}],"done":true}'
        ))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="ok bye", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = __import__("asyncio").run(_run())
        assert result["skip_reply"] is True
        assert "no need" in result.get("skip_reason", "")

    def test_gateway_wechat_reply_tool_intercepted(self):
        """wechat_reply tool action is intercepted as message, same as before."""
        from agent.chat_agent import ChatAgent

        gateway = FakeGatewayForChat(response=_make_mock_gateway_response(
            json.dumps({
                "actions": [{"type": "tool", "name": "wechat_reply",
                             "args": {"text": "通过工具回复"}}],
                "done": True,
            }, ensure_ascii=False)
        ))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="hello", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = __import__("asyncio").run(_run())
        assert len(result["messages"]) == 1
        assert result["messages"][0]["text"] == "通过工具回复"


# -------------------------------------------------------------------
# 3. Error fallback unchanged
# -------------------------------------------------------------------
class TestChatAgentGatewayErrorFallback:
    def test_gateway_timeout_produces_fallback_message(self):
        """When gateway raises LLMError, user sees fallback text (unchanged)."""
        from agent.chat_agent import ChatAgent

        gateway = FakeGatewayForChat(error=LLMError(
            kind="timeout", message="timed out", provider="deepseek",
            retryable=True,
        ))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="你好", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = __import__("asyncio").run(_run())
        assert result["llm_status"] == "error"
        assert len(result["fallback_events"]) >= 1
        assert result["fallback_events"][0]["type"] == "llm_error"
        # Fallback message text unchanged
        assert any("信号不太好" in m.get("text", "") for m in result["messages"])

    def test_gateway_provider_error_produces_fallback(self):
        """Any gateway error keeps the same user-facing behavior."""
        from agent.chat_agent import ChatAgent

        gateway = FakeGatewayForChat(error=LLMError(
            kind="provider_overloaded", message="503", provider="deepseek",
            retryable=True,
        ))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="你好", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = __import__("asyncio").run(_run())
        assert result["llm_status"] == "error"
        assert result["skip_reply"] is False  # fallback message is sent


# -------------------------------------------------------------------
# 4. Multi-round tool loop usage merging
# -------------------------------------------------------------------
class TestChatAgentGatewayUsageMerging:
    def test_single_round_usage_present(self):
        """Single round: token_usage reflects LLMUsage fields."""
        from agent.chat_agent import ChatAgent

        gateway = FakeGatewayForChat(response=_make_mock_gateway_response(
            '{"actions":[{"type":"message","text":"hi","delay":0}],"done":true}',
            prompt_tokens=100, completion_tokens=42, total_tokens=142,
            cache_hit_tokens=30, cache_miss_tokens=70, reasoning_tokens=5,
        ))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="hi", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = __import__("asyncio").run(_run())
        usage = result["token_usage"]
        assert usage["prompt_tokens"] == 100
        assert usage["completion_tokens"] == 42
        assert usage["total_tokens"] == 142
        assert usage["cache_hit_tokens"] == 30
        assert usage["cache_miss_tokens"] == 70
        assert usage["reasoning_tokens"] == 5
        assert usage["estimated"] is False

    def test_multi_round_usage_accumulates(self):
        """Two rounds: usage accumulates across tool loop iterations."""
        from agent.chat_agent import ChatAgent

        round = 0
        responses = [
            _make_mock_gateway_response(
                '{"actions":[{"type":"tool","name":"get_time","args":{}}],"done":false}',
                prompt_tokens=50, completion_tokens=15, total_tokens=65,
            ),
            _make_mock_gateway_response(
                '{"actions":[{"type":"message","text":"now","delay":0}],"done":true}',
                prompt_tokens=80, completion_tokens=10, total_tokens=90,
            ),
        ]

        class MultiRoundGateway:
            def __init__(self):
                self.requests: list[LLMRequest] = []

            async def complete(self, request: LLMRequest) -> LLMResponse:
                self.requests.append(request)
                nonlocal round
                resp = responses[round]
                round += 1
                return resp

        gateway = MultiRoundGateway()
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="几点了", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = __import__("asyncio").run(_run())
        usage = result["token_usage"]
        assert usage["prompt_tokens"] == 130  # 50 + 80
        assert usage["completion_tokens"] == 25  # 15 + 10
        assert usage["total_tokens"] == 155  # 65 + 90
        assert result["iterations"] == 2


# -------------------------------------------------------------------
# 5. raw_response not in conversation_history
# -------------------------------------------------------------------
class TestChatAgentRawResponseSafety:
    def test_raw_response_not_in_turn_result(self):
        """Turn result dict must not contain raw_response."""
        from agent.chat_agent import ChatAgent

        gateway = FakeGatewayForChat(response=_make_mock_gateway_response(
            '{"actions":[{"type":"message","text":"safe","delay":0}],"done":true}'
        ))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="hi", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = __import__("asyncio").run(_run())
        assert "raw_response" not in result
        assert "reasoning_content" not in result
        for msg in result["messages"]:
            assert "raw_response" not in msg
            assert "reasoning_content" not in str(msg)


# -------------------------------------------------------------------
# 6. response_mode is json_object by default, text when disabled
# -------------------------------------------------------------------
class TestChatAgentResponseModeText:
    def test_gateway_request_uses_json_object_by_default(self):
        """ChatAgent defaults to response_mode=json_object since Phase 3A."""
        from agent.chat_agent import ChatAgent

        gateway = FakeGatewayForChat(response=_make_mock_gateway_response(
            '{"actions":[{"type":"message","text":"ok"}],"done":true}'
        ))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="hi", memories=[], history=[],
                relationship={}, trigger="user",
            )

        __import__("asyncio").run(_run())
        for req in gateway.requests:
            assert req.response_mode == "json_object"
            assert req.thinking == "disabled"

    def test_json_output_disabled_uses_text_mode(self):
        """When json_output_enabled=False, response_mode stays text."""
        from agent.chat_agent import ChatAgent

        gateway = FakeGatewayForChat(response=_make_mock_gateway_response(
            '{"actions":[{"type":"message","text":"ok"}],"done":true}'
        ))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
            json_output_enabled=False,
        )

        async def _run():
            return await agent.chat(
                user_message="hi", memories=[], history=[],
                relationship={}, trigger="user",
            )

        __import__("asyncio").run(_run())
        for req in gateway.requests:
            assert req.response_mode == "text"
            assert req.thinking == "disabled"

    def test_json_output_disabled_still_parses_json(self):
        """Even with json_output disabled, valid JSON actions still work."""
        from agent.chat_agent import ChatAgent

        gateway = FakeGatewayForChat(response=_make_mock_gateway_response(
            '{"actions":[{"type":"message","text":"text mode works"}],"done":true}'
        ))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
            json_output_enabled=False,
        )

        async def _run():
            return await agent.chat(
                user_message="hi", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = __import__("asyncio").run(_run())
        assert len(result["messages"]) >= 1
        assert result["messages"][0]["text"] == "text mode works"


# -------------------------------------------------------------------
# 7. LLMGateway is required (no raw client fallback)
# -------------------------------------------------------------------
class TestChatAgentGatewayRequired:
    def test_chat_agent_with_gateway_works(self):
        """ChatAgent now requires llm_gateway; all paths use it."""
        from agent.chat_agent import ChatAgent

        gw = FakeGatewayForChat(response=_make_mock_gateway_response(
            '{"actions":[{"type":"message","text":"via_gateway"}],"done":true}'
        ))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7,
            llm_gateway=gw,
        )

        async def _run():
            return await agent.chat(
                user_message="test", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = __import__("asyncio").run(_run())
        assert len(result["messages"]) >= 1
        assert result["messages"][0]["text"] == "via_gateway"


# -------------------------------------------------------------------
# 8. Phase 3A: JSON Output normalization tests
# -------------------------------------------------------------------
class TestPhase3AJsonOutputNormal:
    """Normal path: json_object mode works correctly."""

    def test_multiple_actions_normal(self):
        from agent.chat_agent import ChatAgent

        gateway = FakeGatewayForChat(response=_make_mock_gateway_response(
            json.dumps({
                "actions": [
                    {"type": "message", "text": "第一条", "delay": 0},
                    {"type": "message", "text": "第二条", "delay": 1},
                ],
                "done": True,
            }, ensure_ascii=False)
        ))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="hi", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = __import__("asyncio").run(_run())
        assert len(result["messages"]) == 2
        assert result["messages"][0]["text"] == "第一条"
        assert result["messages"][1]["text"] == "第二条"

    def test_tool_loop_still_works_with_json_object(self):
        from agent.chat_agent import ChatAgent

        round_idx = [0]
        responses = [
            _make_mock_gateway_response(
                json.dumps({"actions": [{"type": "tool", "name": "get_time", "args": {}}], "done": False},
                           ensure_ascii=False),
                prompt_tokens=50, completion_tokens=15, total_tokens=65,
            ),
            _make_mock_gateway_response(
                json.dumps({"actions": [{"type": "message", "text": "现在的时间是..."}], "done": True},
                           ensure_ascii=False),
                prompt_tokens=80, completion_tokens=10, total_tokens=90,
            ),
        ]

        class MultiRoundGateway:
            def __init__(self):
                self.requests: list = []

            async def complete(self, request):
                self.requests.append(request)
                idx = round_idx[0]
                round_idx[0] += 1
                return responses[idx]

        gateway = MultiRoundGateway()
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="几点了", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = __import__("asyncio").run(_run())
        assert result["iterations"] == 2
        assert len(result["messages"]) >= 1
        # Both iterations should use json_object
        for req in gateway.requests:
            assert req.response_mode == "json_object"

    def test_done_false_triggers_continuation(self):
        from agent.chat_agent import ChatAgent

        round_idx = [0]
        responses = [
            _make_mock_gateway_response(
                json.dumps({"actions": [{"type": "tool", "name": "get_time", "args": {}}], "done": False},
                           ensure_ascii=False),
            ),
            _make_mock_gateway_response(
                json.dumps({"actions": [{"type": "message", "text": "final"}], "done": True},
                           ensure_ascii=False),
            ),
        ]

        class MRG:
            def __init__(self):
                self.requests: list = []

            async def complete(self, request):
                self.requests.append(request)
                idx = round_idx[0]
                round_idx[0] += 1
                return responses[idx]

        gateway = MRG()
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="几点了", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = __import__("asyncio").run(_run())
        assert result["iterations"] == 2
        assert result["messages"][0]["text"] == "final"


class TestPhase3AConfigToggle:
    """Config toggle: json_output.chat_agent = true / false."""

    def test_default_is_json_object(self):
        from agent.chat_agent import ChatAgent

        gateway = FakeGatewayForChat(response=_make_mock_gateway_response(
            '{"actions":[{"type":"message","text":"hi"}],"done":true}'
        ))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="hi", memories=[], history=[],
                relationship={}, trigger="user",
            )

        __import__("asyncio").run(_run())
        assert gateway.requests[0].response_mode == "json_object"

    def test_explicit_false_uses_text(self):
        from agent.chat_agent import ChatAgent

        gateway = FakeGatewayForChat(response=_make_mock_gateway_response(
            '{"actions":[{"type":"message","text":"hi"}],"done":true}'
        ))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
            json_output_enabled=False,
        )

        async def _run():
            return await agent.chat(
                user_message="hi", memories=[], history=[],
                relationship={}, trigger="user",
            )

        __import__("asyncio").run(_run())
        assert gateway.requests[0].response_mode == "text"

    def test_explicit_true_uses_json_object(self):
        from agent.chat_agent import ChatAgent

        gateway = FakeGatewayForChat(response=_make_mock_gateway_response(
            '{"actions":[{"type":"message","text":"hi"}],"done":true}'
        ))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
            json_output_enabled=True,
        )

        async def _run():
            return await agent.chat(
                user_message="hi", memories=[], history=[],
                relationship={}, trigger="user",
            )

        __import__("asyncio").run(_run())
        assert gateway.requests[0].response_mode == "json_object"


class TestPhase3AExceptionPaths:
    """Exception path handling."""

    def test_empty_content_fallback(self):
        from agent.chat_agent import ChatAgent

        gateway = FakeGatewayForChat(response=_make_mock_gateway_response(""))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="hi", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = __import__("asyncio").run(_run())
        assert len(result["messages"]) >= 1
        assert "信号不太好" in result["messages"][0]["text"]
        assert any(e["type"] == "invalid_response" for e in result["fallback_events"])

    def test_non_json_content_fallback(self):
        from agent.chat_agent import ChatAgent

        gateway = FakeGatewayForChat(response=_make_mock_gateway_response(
            "这是一段不是JSON的普通回复文本"
        ))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="hi", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = __import__("asyncio").run(_run())
        assert len(result["messages"]) >= 1
        # Strict mode: non-JSON → fallback, NOT sent as message text
        assert any(e["type"] == "non_json_content" for e in result["fallback_events"])
        # User should see safe fallback text, NOT the raw LLM content
        for msg in result["messages"]:
            assert "不是JSON" not in msg.get("text", "")

    def test_non_json_content_legacy_text_mode(self):
        """In text mode (json_output_enabled=False), non-JSON is treated as message."""
        from agent.chat_agent import ChatAgent

        gateway = FakeGatewayForChat(response=_make_mock_gateway_response(
            "这是一段不是JSON的普通回复文本"
        ))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
            json_output_enabled=False,
        )

        async def _run():
            return await agent.chat(
                user_message="hi", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = __import__("asyncio").run(_run())
        assert len(result["messages"]) >= 1
        # In text mode, non-JSON is treated as plain text message
        assert any(e["type"] == "invalid_json" for e in result["fallback_events"])

    def test_markdown_fence_json_is_parsed(self):
        from agent.chat_agent import ChatAgent

        # Model wraps JSON in ```json ... ```
        gateway = FakeGatewayForChat(response=_make_mock_gateway_response(
            '```json\n{"actions":[{"type":"message","text":"fence_ok"}],"done":true}\n```'
        ))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="hi", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = __import__("asyncio").run(_run())
        assert len(result["messages"]) >= 1
        assert result["messages"][0]["text"] == "fence_ok"

    def test_top_level_not_object_fallback(self):
        from agent.chat_agent import ChatAgent

        # Array instead of object
        gateway = FakeGatewayForChat(response=_make_mock_gateway_response('[1, 2, 3]'))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="hi", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = __import__("asyncio").run(_run())
        assert len(result["messages"]) >= 1
        # Strict mode: array (not object) → non_json_content fallback
        assert any(e["type"] == "non_json_content" for e in result["fallback_events"])
        # User sees safe fallback, not the raw array
        for msg in result["messages"]:
            assert "[1, 2, 3]" not in msg.get("text", "")

    def test_missing_actions_field_fallback(self):
        from agent.chat_agent import ChatAgent

        gateway = FakeGatewayForChat(response=_make_mock_gateway_response('{"done": true}'))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="hi", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = __import__("asyncio").run(_run())
        assert len(result["messages"]) >= 1
        assert any(e["type"] == "missing_actions" for e in result["fallback_events"])

    def test_actions_not_list_fallback(self):
        from agent.chat_agent import ChatAgent

        gateway = FakeGatewayForChat(response=_make_mock_gateway_response(
            '{"actions": "not a list", "done": true}'
        ))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="hi", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = __import__("asyncio").run(_run())
        assert len(result["messages"]) >= 1
        # Should have fallback event for actions_not_a_list
        fallback_types = [e.get("type", "") for e in result.get("fallback_events", [])]
        assert any("not_a_list" in t for t in fallback_types), f"fallback_events: {fallback_types}"

    def test_wechat_reply_without_text_filtered(self):
        from agent.chat_agent import ChatAgent

        gateway = FakeGatewayForChat(response=_make_mock_gateway_response(
            json.dumps({
                "actions": [
                    {"type": "message", "text": ""},
                    {"type": "message", "text": "valid"},
                ],
                "done": True,
            }, ensure_ascii=False)
        ))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="hi", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = __import__("asyncio").run(_run())
        # Only "valid" message remains
        assert len(result["messages"]) == 1
        assert result["messages"][0]["text"] == "valid"
        assert any(e["type"] == "schema_error" for e in result["fallback_events"])

    def test_schedule_without_delay_still_works_with_default(self):
        from agent.chat_agent import ChatAgent

        gateway = FakeGatewayForChat(response=_make_mock_gateway_response(
            json.dumps({
                "actions": [
                    {"type": "tool", "name": "schedule_wechat_message",
                     "args": {"messages": [{"text": "remind", "delay": 0}]}},
                ],
                "done": True,
            }, ensure_ascii=False)
        ))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="remind me", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = __import__("asyncio").run(_run())
        # delay_minutes defaults safely; schedule is created with a default delay
        assert len(result["scheduled_messages"]) == 1
        assert result["scheduled_messages"][0]["delay_minutes"] == 0.1
        # Schema validation still logs a warning
        assert any(e["type"] == "schema_error" for e in result["fallback_events"])

    def test_finish_reason_length_recorded(self):
        from agent.chat_agent import ChatAgent

        gateway = FakeGatewayForChat(response=_make_mock_gateway_response(
            '{"actions":[{"type":"message","text":"truncated',
            finish_reason="length",
        ))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="hi", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = __import__("asyncio").run(_run())
        assert any(
            "length" in str(e.get("detail", ""))
            for e in result.get("fallback_events", [])
        )

    def test_unknown_action_type_filtered(self):
        from agent.chat_agent import ChatAgent

        gateway = FakeGatewayForChat(response=_make_mock_gateway_response(
            json.dumps({
                "actions": [
                    {"type": "invalid_action_type", "data": "???"},
                    {"type": "message", "text": "valid"},
                ],
                "done": True,
            }, ensure_ascii=False)
        ))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="hi", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = __import__("asyncio").run(_run())
        assert len(result["messages"]) == 1
        assert result["messages"][0]["text"] == "valid"
        assert any(e["type"] == "schema_error" for e in result["fallback_events"])


class TestPhase3AFinishReasonLength:
    """finish_reason=length handling in strict json mode."""

    def test_length_triggers_fallback_in_strict_mode(self):
        from agent.chat_agent import ChatAgent

        gateway = FakeGatewayForChat(response=_make_mock_gateway_response(
            '{"actions":[{"type":"message","text":"truncated',
            finish_reason="length",
        ))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="hi", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = __import__("asyncio").run(_run())
        assert len(result["messages"]) >= 1
        # Should have recorded the length event
        assert any(
            "length" in str(e.get("detail", ""))
            for e in result.get("fallback_events", [])
        )
        # User should see safe fallback text
        assert any("信号不太好" in m["text"] for m in result["messages"])

    def test_length_retry_succeeds_uses_retry_output(self):
        from agent.chat_agent import ChatAgent

        call_count = [0]
        responses = [
            # First call: truncated
            _make_mock_gateway_response('{"actions":[{"type":"message","text":"trun',
                                        finish_reason="length",
                                        prompt_tokens=50, completion_tokens=5, total_tokens=55),
            # Retry call: valid
            _make_mock_gateway_response(
                '{"actions":[{"type":"message","text":"完整回复"}],"done":true}',
                finish_reason="stop",
                prompt_tokens=50, completion_tokens=8, total_tokens=58,
            ),
        ]

        class LengthRetryGateway:
            def __init__(self):
                self.requests: list = []

            async def complete(self, request):
                self.requests.append(request)
                idx = call_count[0]
                call_count[0] += 1
                return responses[idx]

        gateway = LengthRetryGateway()
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="hi", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = __import__("asyncio").run(_run())
        # Retry succeeded, we use retry output
        assert len(result["messages"]) >= 1
        assert result["messages"][0]["text"] == "完整回复"
        # Both calls used json_object
        for req in gateway.requests:
            assert req.response_mode == "json_object"
        # Usage merged from both calls
        assert result["token_usage"]["prompt_tokens"] == 100  # 50 + 50
        assert result["token_usage"]["completion_tokens"] == 13  # 5 + 8
        # Should have retry success event
        retry_events = [e for e in result["fallback_events"] if e["type"] == "finish_reason_length_retry"]
        assert len(retry_events) == 1
        assert retry_events[0]["detail"] == "success"

    def test_length_retry_fails_fallback(self):
        from agent.chat_agent import ChatAgent

        call_count = [0]
        responses = [
            _make_mock_gateway_response('{"actions":[{"type":"message","text":"trun',
                                        finish_reason="length",
                                        prompt_tokens=50, completion_tokens=5, total_tokens=55),
            # Retry also truncated
            _make_mock_gateway_response('{"actions":[{"type":"message","text":"also',
                                        finish_reason="length",
                                        prompt_tokens=50, completion_tokens=3, total_tokens=53),
        ]

        class LengthRetryFailGateway:
            def __init__(self):
                self.requests: list = []

            async def complete(self, request):
                self.requests.append(request)
                idx = call_count[0]
                call_count[0] += 1
                return responses[idx]

        gateway = LengthRetryFailGateway()
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="hi", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = __import__("asyncio").run(_run())
        # Both retries failed → fallback message
        assert any("信号不太好" in m["text"] for m in result["messages"])
        # Usage merged from both calls
        assert result["token_usage"]["prompt_tokens"] == 100
        assert result["token_usage"]["completion_tokens"] == 8
        # Retry failed event recorded
        retry_events = [e for e in result["fallback_events"] if e["type"] == "finish_reason_length_retry"]
        assert retry_events[0]["detail"] == "failed"

    def test_length_in_text_mode_passes_through(self):
        """In text mode, length is logged but parse continues (legacy behavior)."""
        from agent.chat_agent import ChatAgent

        gateway = FakeGatewayForChat(response=_make_mock_gateway_response(
            "这是一段被截断的普通文本",
            finish_reason="length",
        ))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
            json_output_enabled=False,
        )

        async def _run():
            return await agent.chat(
                user_message="hi", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = __import__("asyncio").run(_run())
        assert len(result["messages"]) >= 1
        # In text mode, the truncated text is still sent as a message
        assert any("被截断" in m["text"] for m in result["messages"])
        # length event is still recorded
        assert any(
            "length" in str(e.get("detail", ""))
            for e in result.get("fallback_events", [])
        )


class TestPhase3ASafety:
    """Safety guarantees maintained with json_object mode."""

    def test_raw_response_not_in_turn_result_json_object(self):
        from agent.chat_agent import ChatAgent

        gateway = FakeGatewayForChat(response=_make_mock_gateway_response(
            '{"actions":[{"type":"message","text":"safe"}],"done":true}'
        ))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="hi", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = __import__("asyncio").run(_run())
        assert "raw_response" not in result
        assert "reasoning_content" not in result
        for msg in result["messages"]:
            assert "raw_response" not in msg
            assert "{" not in msg.get("text", "")  # No bare JSON to user

    def test_thinking_still_disabled(self):
        from agent.chat_agent import ChatAgent

        gateway = FakeGatewayForChat(response=_make_mock_gateway_response(
            '{"actions":[{"type":"message","text":"ok"}],"done":true}'
        ))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="hi", memories=[], history=[],
                relationship={}, trigger="user",
            )

        __import__("asyncio").run(_run())
        for req in gateway.requests:
            assert req.thinking == "disabled"

    def test_no_tool_calls_in_request(self):
        from agent.chat_agent import ChatAgent

        gateway = FakeGatewayForChat(response=_make_mock_gateway_response(
            '{"actions":[{"type":"message","text":"ok"}],"done":true}'
        ))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="hi", memories=[], history=[],
                relationship={}, trigger="user",
            )

        __import__("asyncio").run(_run())
        for req in gateway.requests:
            assert req.tools is None

    def test_user_does_not_see_bare_json(self):
        from agent.chat_agent import ChatAgent

        gateway = FakeGatewayForChat(response=_make_mock_gateway_response(
            '{"actions":[{"type":"message","text":"你好！今天天气不错"}],"done":true}'
        ))
        agent = ChatAgent(
            model="test", persona="p", personality="",
            temperature=0.7, llm_gateway=gateway,
        )

        async def _run():
            return await agent.chat(
                user_message="hi", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = __import__("asyncio").run(_run())
        for msg in result["messages"]:
            assert "actions" not in msg.get("text", "")
            assert "wechat_reply" not in msg.get("text", "")


# ================================================================
# Phase 3A: Validate helper unit tests (no async needed)
# ================================================================
class TestValidateAgentOutput:
    def test_valid_simple_actions(self):
        from utils.json_parser import validate_agent_output

        errors = validate_agent_output({
            "actions": [{"type": "message", "text": "hello"}],
            "done": True,
        })
        assert errors == []

    def test_valid_tool_action(self):
        from utils.json_parser import validate_agent_output

        errors = validate_agent_output({
            "actions": [{"type": "tool", "name": "get_time", "args": {}}],
            "done": False,
        })
        assert errors == []

    def test_valid_skip_action(self):
        from utils.json_parser import validate_agent_output

        errors = validate_agent_output({
            "actions": [{"type": "skip", "reason": "no need"}],
            "done": True,
        })
        assert errors == []

    def test_missing_actions_field(self):
        from utils.json_parser import validate_agent_output

        errors = validate_agent_output({"done": True})
        assert any("missing_actions" in e for e in errors)

    def test_actions_not_list(self):
        from utils.json_parser import validate_agent_output

        errors = validate_agent_output({"actions": "not_a_list"})
        assert any("not_list" in e for e in errors)

    def test_action_not_object(self):
        from utils.json_parser import validate_agent_output

        errors = validate_agent_output({
            "actions": ["just a string", {"type": "message", "text": "ok"}],
            "done": True,
        })
        assert any("not_object" in e for e in errors)

    def test_message_empty_text(self):
        from utils.json_parser import validate_agent_output

        errors = validate_agent_output({
            "actions": [{"type": "message", "text": ""}],
            "done": True,
        })
        assert any("empty_text" in e for e in errors)

    def test_wechat_reply_empty_text(self):
        from utils.json_parser import validate_agent_output

        errors = validate_agent_output({
            "actions": [{"type": "tool", "name": "wechat_reply", "args": {"text": ""}}],
            "done": True,
        })
        assert any("wechat_reply_empty_text" in e for e in errors)

    def test_schedule_missing_delay(self):
        from utils.json_parser import validate_agent_output

        errors = validate_agent_output({
            "actions": [{"type": "tool", "name": "schedule_wechat_message",
                         "args": {"messages": [{"text": "hi"}]}}],
            "done": True,
        })
        assert any("schedule_missing_delay" in e for e in errors)

    def test_non_dict_input(self):
        from utils.json_parser import validate_agent_output

        errors = validate_agent_output([1, 2, 3])  # type: ignore[arg-type]
        assert any("top_level_not_object" in e for e in errors)

    def test_done_not_bool(self):
        from utils.json_parser import validate_agent_output

        errors = validate_agent_output({
            "actions": [{"type": "message", "text": "hi"}],
            "done": "yes",
        })
        assert any("done_not_bool" in e for e in errors)


# ══════════════════════════════════════════════════════════════════
# Bug B fix: history role formatting tests
# ══════════════════════════════════════════════════════════════════

class TestHistoryRoleFormatting:
    # The role field in the API message (user/assistant) already correctly
    # distinguishes who said what.  We intentionally keep the original
    # [历史消息] format and raw JSON content — extracting text or adding
    # role labels confuses the model into echo-chambering history content
    # back as output instead of generating JSON.

    def test_user_message_standard_format(self):
        """User messages: [历史消息 | ts | 来源:xxx] + original content."""
        from agent.chat_agent import _format_history_content

        result = _format_history_content({
            "role": "user", "content": "今天吃了什么",
            "ts": 1717000000, "source": "wechat",
        })
        assert "[历史消息" in result
        assert "来源:wechat" in result
        assert "今天吃了什么" in result

    def test_assistant_message_keeps_raw_json(self):
        """Assistant messages keep raw JSON — do NOT extract text."""
        from agent.chat_agent import _format_history_content

        raw = '{"actions":[{"type":"tool","name":"wechat_reply","args":{"text":"有点无聊","delay":0}}],"done":true}'
        result = _format_history_content({
            "role": "assistant", "content": raw,
            "ts": 1717000000, "source": "user_reply",
        })
        assert "[历史消息" in result
        assert "来源:user_reply" in result
        # Raw JSON preserved — text is inside JSON, not bare
        assert '"actions"' in result
        assert '"wechat_reply"' in result
        assert '有点无聊' in result  # Still in the raw JSON string

    def test_proactive_message_standard_format(self):
        """Proactive messages: standard [历史消息] + source preserved."""
        from agent.chat_agent import _format_history_content

        raw = '{"actions":[{"type":"tool","name":"wechat_reply","args":{"text":"脑子里空转","delay":0}}],"done":true}'
        result = _format_history_content({
            "role": "assistant", "content": raw,
            "ts": 1717000000, "source": "proactive",
        })
        assert "[历史消息" in result
        assert "来源:proactive" in result
        # JSON structure preserved
        assert '"actions"' in result

    def test_no_role_labels_in_content(self):
        """No [Bot回复]/[用户消息]/[Bot主动消息] labels in content."""
        from agent.chat_agent import _format_history_content

        for role, source, content in [
            ("user", "wechat", "hello"),
            ("assistant", "user_reply", '{"actions":[{"type":"tool","name":"wechat_reply","args":{"text":"hi","delay":0}}],"done":true}'),
            ("assistant", "proactive", '{"actions":[{"type":"tool","name":"wechat_reply","args":{"text":"ping","delay":0}}],"done":true}'),
        ]:
            result = _format_history_content({
                "role": role, "content": content,
                "ts": 1717000000, "source": source,
            })
            assert "[Bot回复" not in result
            assert "[Bot主动消息" not in result
            assert "[用户消息" not in result


if __name__ == "__main__":
    test_extract_json_from_code_block()
    test_extract_json_bare()
    test_extract_json_with_text_around()
    test_safe_parse_valid_json()
    test_safe_parse_invalid_json_returns_none()
    test_safe_parse_empty_string_returns_none()
    test_safe_parse_trailing_comma_fix()
    test_parse_agent_output_fallback()
    test_parse_agent_output_no_actions_field()
    test_parse_agent_output_preserves_valid_json()
    print("[OK] All JSON parser tests passed")
