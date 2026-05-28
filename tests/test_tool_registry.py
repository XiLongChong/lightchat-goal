"""Tests for ActionRegistry / ToolRegistry (Phase 4B)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from agent.tool_registry import (
    ActionDef,
    ActionRegistry,
    ToolDef,
    ToolRegistry,
    ACTION_REGISTRY,
    TOOL_REGISTRY,
    get_all_tool_names,
    is_action,
    is_external_tool,
)


# ══════════════════════════════════════════════════════════════════
# 1. Registry existence and content
# ══════════════════════════════════════════════════════════════════

class TestActionRegistryContent:
    def test_contains_internal_actions(self):
        names = ACTION_REGISTRY.names()
        assert "wechat_reply" in names
        assert "schedule_wechat_message" in names
        assert "skip" in names

    def test_wechat_reply_not_in_tool_registry(self):
        assert "wechat_reply" not in TOOL_REGISTRY

    def test_schedule_not_in_tool_registry(self):
        assert "schedule_wechat_message" not in TOOL_REGISTRY

    def test_skip_not_in_tool_registry(self):
        assert "skip" not in TOOL_REGISTRY


class TestToolRegistryContent:
    def test_contains_external_tools(self):
        names = TOOL_REGISTRY.names()
        assert "get_time" in names
        assert "get_weather" in names
        assert "search_memory" in names
        assert "read_persona_file" in names
        assert "propose_persona_change" in names
        assert "send_email" in names

    def test_get_weather_not_in_action_registry(self):
        assert "get_weather" not in ACTION_REGISTRY

    def test_get_time_not_in_action_registry(self):
        assert "get_time" not in ACTION_REGISTRY


# ══════════════════════════════════════════════════════════════════
# 2. Definition completeness
# ══════════════════════════════════════════════════════════════════

class TestDefinitionCompleteness:
    @pytest.mark.parametrize("registry,kind", [
        (ACTION_REGISTRY, "action"),
        (TOOL_REGISTRY, "tool"),
    ])
    def test_every_def_has_required_fields(self, registry, kind):
        for d in registry.iter_defs():
            assert d.name, f"{kind} {d.name} has empty name"
            assert d.description, f"{kind} {d.name} has empty description"
            # args_schema can be list[str] or dict (full JSON Schema)
            assert isinstance(d.args_schema, (list, dict)), \
                f"{kind} {d.name} args_schema must be list or dict, got {type(d.args_schema)}"
            assert d.side_effect_level, f"{kind} {d.name} has empty side_effect_level"


# ══════════════════════════════════════════════════════════════════
# 3. Native tool call eligibility
# ══════════════════════════════════════════════════════════════════

class TestNativeEligibility:
    def test_get_time_is_native_eligible(self):
        t = TOOL_REGISTRY.get("get_time")
        assert t is not None
        assert t.native_tool_call_eligible is True

    def test_get_weather_is_native_eligible(self):
        t = TOOL_REGISTRY.get("get_weather")
        assert t is not None
        assert t.native_tool_call_eligible is True

    def test_send_email_not_native_eligible(self):
        t = TOOL_REGISTRY.get("send_email")
        assert t is not None
        assert t.native_tool_call_eligible is False

    def test_search_memory_not_native_eligible(self):
        t = TOOL_REGISTRY.get("search_memory")
        assert t is not None
        assert t.native_tool_call_eligible is False

    def test_read_persona_not_native_eligible(self):
        t = TOOL_REGISTRY.get("read_persona_file")
        assert t is not None
        assert t.native_tool_call_eligible is False

    def test_internal_actions_not_in_native_specs(self):
        specs = TOOL_REGISTRY.get_native_tool_specs()
        names = {s["function"]["name"] for s in specs}
        assert "wechat_reply" not in names
        assert "schedule_wechat_message" not in names
        assert "skip" not in names

    def test_get_native_tool_specs_only_eligible(self):
        specs = TOOL_REGISTRY.get_native_tool_specs(eligible_only=True)
        names = {s["function"]["name"] for s in specs}
        assert names == {"get_time", "get_weather"}

    def test_get_native_tool_specs_includes_all_when_eligible_only_false(self):
        specs = TOOL_REGISTRY.get_native_tool_specs(eligible_only=False)
        names = {s["function"]["name"] for s in specs}
        assert "get_time" in names
        assert "get_weather" in names
        assert "send_email" in names  # included when eligible_only=False

    def test_native_specs_have_correct_structure(self):
        specs = TOOL_REGISTRY.get_native_tool_specs()
        for s in specs:
            assert s["type"] == "function"
            assert "name" in s["function"]
            assert "description" in s["function"]
            assert "parameters" in s["function"]
            assert s["function"]["parameters"]["type"] == "object"
            assert "properties" in s["function"]["parameters"]


# ══════════════════════════════════════════════════════════════════
# 4. send_email safety
# ══════════════════════════════════════════════════════════════════

class TestSendEmailSafety:
    def test_side_effect_is_high(self):
        t = TOOL_REGISTRY.get("send_email")
        assert t.side_effect_level == "high"

    def test_requires_confirmation(self):
        t = TOOL_REGISTRY.get("send_email")
        assert t.requires_confirmation is True

    def test_not_native_eligible(self):
        t = TOOL_REGISTRY.get("send_email")
        assert t.native_tool_call_eligible is False

    def test_has_todo_comment(self):
        t = TOOL_REGISTRY.get("send_email")
        assert "todo" in t.metadata or "TODO" in str(t.metadata)
        assert "confirmation" in str(t.metadata).lower()


# ══════════════════════════════════════════════════════════════════
# 5. Mutable metadata isolation
# ══════════════════════════════════════════════════════════════════

class TestRegistryMutableMetadata:
    def test_action_def_metadata_not_shared(self):
        a1 = ActionDef(name="a1", description="d", args_schema=[])
        a2 = ActionDef(name="a2", description="d", args_schema=[])
        a1.metadata["key"] = "only_a1"
        assert "key" not in a2.metadata

    def test_tool_def_metadata_not_shared(self):
        t1 = ToolDef(name="t1", description="d", args_schema=[])
        t2 = ToolDef(name="t2", description="d", args_schema=[])
        t1.metadata["key"] = "only_t1"
        assert "key" not in t2.metadata


# ══════════════════════════════════════════════════════════════════
# 6. Registry API
# ══════════════════════════════════════════════════════════════════

class TestRegistryAPI:
    def test_action_registry_has(self):
        assert ACTION_REGISTRY.has("wechat_reply")
        assert not ACTION_REGISTRY.has("nonexistent")

    def test_action_registry_contains(self):
        assert "wechat_reply" in ACTION_REGISTRY
        assert "get_time" not in ACTION_REGISTRY

    def test_action_registry_get(self):
        a = ACTION_REGISTRY.get("skip")
        assert a is not None
        assert a.name == "skip"
        assert a.handler_type == "internal"
        assert ACTION_REGISTRY.get("nonexistent") is None

    def test_tool_registry_has(self):
        assert TOOL_REGISTRY.has("get_weather")
        assert not TOOL_REGISTRY.has("nonexistent")

    def test_tool_registry_contains(self):
        assert "get_time" in TOOL_REGISTRY
        assert "wechat_reply" not in TOOL_REGISTRY

    def test_tool_registry_get(self):
        t = TOOL_REGISTRY.get("search_memory")
        assert t is not None
        assert t.name == "search_memory"
        assert t.native_tool_call_eligible is False

    def test_is_action_helper(self):
        assert is_action("wechat_reply")
        assert is_action("skip")
        assert not is_action("get_weather")

    def test_is_external_tool_helper(self):
        assert is_external_tool("get_time")
        assert is_external_tool("send_email")
        assert not is_external_tool("wechat_reply")

    def test_get_all_tool_names_union(self):
        all_names = get_all_tool_names()
        assert "wechat_reply" in all_names
        assert "skip" in all_names
        assert "get_time" in all_names
        assert "send_email" in all_names
        # 3 actions + 6 tools = 9 unique names
        assert len(all_names) == 9

    def test_registry_len(self):
        assert len(ACTION_REGISTRY) == 3
        assert len(TOOL_REGISTRY) == 6

    def test_empty_registry(self):
        r = ActionRegistry()
        assert r.has("none") is False
        assert r.get("none") is None
        assert len(r.names()) == 0

    def test_registry_overwrite_warns(self, caplog):
        r = ActionRegistry()
        r.register(ActionDef(name="dup", description="first", args_schema=[]))
        r.register(ActionDef(name="dup", description="second", args_schema=[]))
        assert "overwriting" in caplog.text.lower()


# ══════════════════════════════════════════════════════════════════
# 7. Backward compat: TOOL_WHITELIST still works
# ══════════════════════════════════════════════════════════════════

class TestBackwardCompat:
    def test_tool_whitelist_is_union(self):
        from agent.tools import TOOL_WHITELIST
        assert "wechat_reply" in TOOL_WHITELIST
        assert "get_time" in TOOL_WHITELIST
        assert "send_email" in TOOL_WHITELIST

    def test_execute_tool_rejects_unknown(self):
        import asyncio
        from agent.tools import execute_tool

        result = asyncio.run(execute_tool("nonexistent_tool", {}))
        assert "不在可用列表中" in result

    def test_execute_tool_handles_internal_action_noop(self):
        import asyncio
        from agent.tools import execute_tool

        result = asyncio.run(execute_tool("wechat_reply", {"text": "hi"}))
        # Internal actions return "已处理" when they reach executor
        assert "已处理" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
