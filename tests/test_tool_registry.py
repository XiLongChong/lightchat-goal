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

    def test_send_text_message_is_in_tool_registry(self):
        """Phase 13: send_text_message is the canonical message tool."""
        assert "send_text_message" in TOOL_REGISTRY

    def test_schedule_not_in_tool_registry(self):
        assert "schedule_wechat_message" not in TOOL_REGISTRY

    def test_skip_not_in_tool_registry(self):
        assert "skip" not in TOOL_REGISTRY


class TestToolRegistryContent:
    def test_contains_canonical_tools(self):
        """Phase 13: only canonical tools registered (11 active)."""
        names = TOOL_REGISTRY.names()
        assert "get_time" in names
        assert "get_weather" in names
        assert "send_text_message" in names
        assert "finish_turn" in names
        assert "schedule_user_reminder" in names
        assert "skip_initiative" in names
        assert "mark_reminder_delivered" in names
        assert "apply_forget_directive" in names
        assert "generate_image_message" in names
        assert "send_image_message" in names
        assert "send_sticker" in names
        assert "search_memory" not in names
        assert "send_email" not in names
        # Old deprecated names no longer registered
        assert "wechat_reply" not in names
        assert "done" not in names
        assert "skip_proactive" not in names
        assert "schedule_wake_up" not in names
        assert len(names) == 11  # 11 canonical tools

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

    def test_old_names_not_registered(self):
        """Phase 13: all old/deprecated tool names removed from registry."""
        assert TOOL_REGISTRY.get("wechat_reply") is None
        assert TOOL_REGISTRY.get("done") is None
        assert TOOL_REGISTRY.get("skip_proactive") is None
        assert TOOL_REGISTRY.get("schedule_wake_up") is None
        assert TOOL_REGISTRY.get("skip_reply") is None
        assert TOOL_REGISTRY.get("send_wechat_messages") is None
        assert TOOL_REGISTRY.get("create_scheduled_trigger") is None

    def test_send_text_message_is_eligible(self):
        """Phase 13: send_text_message is the canonical name."""
        t = TOOL_REGISTRY.get("send_text_message")
        assert t is not None
        assert t.native_tool_call_eligible is True

    def test_finish_turn_is_eligible(self):
        """Phase 13: finish_turn is the canonical name."""
        t = TOOL_REGISTRY.get("finish_turn")
        assert t is not None
        assert t.native_tool_call_eligible is True

    def test_get_native_tool_specs_only_eligible(self):
        """Phase 13: eligible specs use canonical names only."""
        specs = TOOL_REGISTRY.get_native_tool_specs(eligible_only=True)
        names = {s["function"]["name"] for s in specs}
        assert names == {
            "get_time", "get_weather",
            "send_text_message", "finish_turn",
            "skip_initiative", "schedule_user_reminder",
            "mark_reminder_delivered", "apply_forget_directive",
            "generate_image_message", "send_image_message", "send_sticker",
        }

    def test_get_native_tool_specs_all_equal_eligible(self):
        """Phase 13: eligible_only=False same as True when no deprecated tools."""
        specs = TOOL_REGISTRY.get_native_tool_specs(eligible_only=False)
        names = {s["function"]["name"] for s in specs}
        assert "send_text_message" in names
        assert "finish_turn" in names
        assert "skip_initiative" in names
        assert "schedule_user_reminder" in names
        assert "wechat_reply" not in names
        assert len(names) == 11  # all 11 canonical tools

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

    def test_send_text_message_has_high_side_effect(self):
        t = TOOL_REGISTRY.get("send_text_message")
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
        assert not ACTION_REGISTRY.has("send_text_message")
        assert not ACTION_REGISTRY.has("skip")
        assert not ACTION_REGISTRY.has("nonexistent")

    def test_action_registry_contains(self):
        assert "get_time" not in ACTION_REGISTRY

    def test_action_registry_get(self):
        assert ACTION_REGISTRY.get("skip") is None
        assert ACTION_REGISTRY.get("nonexistent") is None

    def test_tool_registry_has(self):
        assert TOOL_REGISTRY.has("get_weather")
        assert TOOL_REGISTRY.has("send_text_message")
        assert TOOL_REGISTRY.has("finish_turn")
        assert not TOOL_REGISTRY.has("nonexistent")

    def test_tool_registry_contains(self):
        assert "get_time" in TOOL_REGISTRY
        assert "send_text_message" in TOOL_REGISTRY
        assert "finish_turn" in TOOL_REGISTRY

    def test_tool_registry_get(self):
        t = TOOL_REGISTRY.get("get_time")
        assert t is not None
        assert t.name == "get_time"
        assert t.native_tool_call_eligible is True

    def test_is_action_helper(self):
        """v0.2: ActionRegistry empty, is_action() always False."""
        assert not is_action("send_text_message")
        assert not is_action("skip")
        assert not is_action("get_weather")

    def test_is_external_tool_helper(self):
        assert is_external_tool("get_time")
        assert is_external_tool("send_text_message")
        assert is_external_tool("finish_turn")
        assert not is_external_tool("send_email")

    def test_get_all_tool_names_union(self):
        """Phase 13: 11 canonical tools only."""
        all_names = get_all_tool_names()
        assert "get_time" in all_names
        assert "get_weather" in all_names
        assert "send_text_message" in all_names
        assert "finish_turn" in all_names
        assert "skip_initiative" in all_names
        assert "schedule_user_reminder" in all_names
        assert "send_email" not in all_names
        assert len(all_names) == 11  # 11 canonical tools

    def test_registry_len(self):
        assert len(ACTION_REGISTRY) == 0
        assert len(TOOL_REGISTRY) == 11  # 11 canonical tools

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
        """Phase 13: canonical tools in whitelist."""
        from agent.tools import TOOL_WHITELIST
        assert "get_time" in TOOL_WHITELIST
        assert "get_weather" in TOOL_WHITELIST
        assert "send_text_message" in TOOL_WHITELIST
        assert "finish_turn" in TOOL_WHITELIST
        assert "send_email" not in TOOL_WHITELIST

    def test_execute_tool_rejects_unknown(self):
        import asyncio
        from agent.tools import execute_tool

        result = asyncio.run(execute_tool("nonexistent_tool", {}))
        assert "不在可用列表中" in result

    def test_execute_tool_accepts_get_time(self):
        """Phase 13: get_time is an external tool with executor."""
        import asyncio
        from agent.tools import execute_tool

        result = asyncio.run(execute_tool("get_time", {}))
        assert "错误" not in result
        assert len(result) > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
