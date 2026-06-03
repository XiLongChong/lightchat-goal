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
    def test_action_registry_is_empty_in_v0_2(self):
        """v0.2: ActionRegistry is empty — wechat_reply and skip removed."""
        names = ACTION_REGISTRY.names()
        assert len(names) == 0

    def test_wechat_reply_is_in_tool_registry(self):
        """v1.5: wechat_reply is a registered tool."""
        assert "wechat_reply" in TOOL_REGISTRY

    def test_schedule_not_in_tool_registry(self):
        assert "schedule_wechat_message" not in TOOL_REGISTRY

    def test_skip_not_in_tool_registry(self):
        assert "skip" not in TOOL_REGISTRY


class TestToolRegistryContent:
    def test_contains_external_tools(self):
        """v1.6-C: 13 tools registered (11 active + 2 deprecated)."""
        names = TOOL_REGISTRY.names()
        assert "get_time" in names
        assert "get_weather" in names
        assert "wechat_reply" in names
        assert "done" in names
        assert "schedule_wake_up" in names
        assert "send_wechat_messages" in names   # kept deprecated
        assert "skip_reply" in names             # kept deprecated
        assert "search_memory" not in names
        assert "send_email" not in names
        assert len(names) == 13  # 6 original + 2 deprecated + 5 Phase 2

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

    def test_send_email_not_registered(self):
        """v0.2: send_email removed from registry."""
        t = TOOL_REGISTRY.get("send_email")
        assert t is None

    def test_search_memory_not_registered(self):
        """v0.2: search_memory removed from registry."""
        t = TOOL_REGISTRY.get("search_memory")
        assert t is None

    def test_read_persona_not_registered(self):
        """v0.2: read_persona_file removed from registry."""
        t = TOOL_REGISTRY.get("read_persona_file")
        assert t is None

    def test_send_wechat_messages_not_native_eligible(self):
        """v1.5: send_wechat_messages is deprecated, not native-eligible."""
        t = TOOL_REGISTRY.get("send_wechat_messages")
        assert t is not None
        assert t.native_tool_call_eligible is False

    def test_skip_reply_not_native_eligible(self):
        """v1.5: skip_reply is deprecated, not native-eligible."""
        t = TOOL_REGISTRY.get("skip_reply")
        assert t is not None
        assert t.native_tool_call_eligible is False

    def test_wechat_reply_is_native_eligible(self):
        """v1.5: wechat_reply is native-eligible."""
        t = TOOL_REGISTRY.get("wechat_reply")
        assert t is not None
        assert t.native_tool_call_eligible is True

    def test_done_is_native_eligible(self):
        """v1.5: done is native-eligible terminal tool."""
        t = TOOL_REGISTRY.get("done")
        assert t is not None
        assert t.native_tool_call_eligible is True
        assert t.tool_category == "terminal_tool"

    def test_get_native_tool_specs_only_eligible(self):
        """v1.6-C: native specs include the scheduled state tool."""
        specs = TOOL_REGISTRY.get_native_tool_specs(eligible_only=True)
        names = {s["function"]["name"] for s in specs}
        assert names == {
            "get_time", "get_weather", "wechat_reply", "done",
            "skip_proactive", "schedule_wake_up",
            "mark_reminder_delivered", "apply_forget_directive",
            "generate_image_message", "send_image_message", "send_sticker",
        }

    def test_get_native_tool_specs_includes_all_when_eligible_only_false(self):
        """v1.6-C: all registered tools visible when eligible_only=False."""
        specs = TOOL_REGISTRY.get_native_tool_specs(eligible_only=False)
        names = {s["function"]["name"] for s in specs}
        assert "get_time" in names
        assert "get_weather" in names
        assert "wechat_reply" in names
        assert "done" in names
        assert "skip_proactive" in names
        assert "schedule_wake_up" in names
        assert "send_wechat_messages" in names
        assert "skip_reply" in names
        assert len(names) == 13  # 11 active + 2 deprecated

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
    """v0.2: send_email removed. Safety is enforced by not registering it."""

    def test_send_email_not_registered(self):
        t = TOOL_REGISTRY.get("send_email")
        assert t is None

    def test_send_wechat_messages_has_high_side_effect(self):
        t = TOOL_REGISTRY.get("send_wechat_messages")
        assert t is not None
        assert t.side_effect_level == "high"


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
        """v0.2: ActionRegistry is empty."""
        assert not ACTION_REGISTRY.has("wechat_reply")
        assert not ACTION_REGISTRY.has("skip")
        assert not ACTION_REGISTRY.has("nonexistent")

    def test_action_registry_contains(self):
        assert "wechat_reply" not in ACTION_REGISTRY
        assert "get_time" not in ACTION_REGISTRY

    def test_action_registry_get(self):
        assert ACTION_REGISTRY.get("skip") is None
        assert ACTION_REGISTRY.get("nonexistent") is None

    def test_tool_registry_has(self):
        assert TOOL_REGISTRY.has("get_weather")
        assert TOOL_REGISTRY.has("wechat_reply")   # v1.5
        assert TOOL_REGISTRY.has("done")            # v1.5
        assert not TOOL_REGISTRY.has("nonexistent")

    def test_tool_registry_contains(self):
        assert "get_time" in TOOL_REGISTRY
        assert "wechat_reply" in TOOL_REGISTRY      # v1.5
        assert "done" in TOOL_REGISTRY              # v1.5

    def test_tool_registry_get(self):
        t = TOOL_REGISTRY.get("get_time")
        assert t is not None
        assert t.name == "get_time"
        assert t.native_tool_call_eligible is True

    def test_is_action_helper(self):
        """v0.2: ActionRegistry empty, is_action() always False."""
        assert not is_action("wechat_reply")
        assert not is_action("skip")
        assert not is_action("get_weather")

    def test_is_external_tool_helper(self):
        assert is_external_tool("get_time")
        assert is_external_tool("send_wechat_messages")
        assert is_external_tool("wechat_reply")    # v1.5: now a registered tool
        assert is_external_tool("done")            # v1.5
        assert not is_external_tool("send_email")

    def test_get_all_tool_names_union(self):
        """v1.6-C: 13 tools (11 active + 2 deprecated)."""
        all_names = get_all_tool_names()
        assert "get_time" in all_names
        assert "get_weather" in all_names
        assert "wechat_reply" in all_names
        assert "done" in all_names
        assert "skip_proactive" in all_names
        assert "schedule_wake_up" in all_names
        assert "send_wechat_messages" in all_names
        assert "skip_reply" in all_names
        assert "send_email" not in all_names
        assert len(all_names) == 13  # 11 active + 2 deprecated

    def test_registry_len(self):
        assert len(ACTION_REGISTRY) == 0
        assert len(TOOL_REGISTRY) == 13  # 11 active + 2 deprecated

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
        """v1.5: 6 tools in whitelist."""
        from agent.tools import TOOL_WHITELIST
        assert "get_time" in TOOL_WHITELIST
        assert "get_weather" in TOOL_WHITELIST
        assert "wechat_reply" in TOOL_WHITELIST
        assert "done" in TOOL_WHITELIST
        assert "send_wechat_messages" in TOOL_WHITELIST
        assert "skip_reply" in TOOL_WHITELIST
        assert "send_email" not in TOOL_WHITELIST

    def test_execute_tool_rejects_unknown(self):
        import asyncio
        from agent.tools import execute_tool

        result = asyncio.run(execute_tool("nonexistent_tool", {}))
        assert "不在可用列表中" in result

    def test_execute_tool_accepts_wechat_reply(self):
        """v1.5: wechat_reply is a registered tool."""
        import asyncio
        from agent.tools import execute_tool

        result = asyncio.run(execute_tool("wechat_reply", {"text": "hi"}))
        assert "错误" not in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
