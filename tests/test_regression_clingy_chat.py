"""Regression tests for clingy + chat + proactive state consistency.

Covers 6 scenarios:
  1. Normal /chat user message → delivered, diagnostics recorded
  2. User silent request → skipped, no fallback, no awaiting
  3. Proactive candidate → sent + send_status both "delivered"
  4. Clingy awaiting transitions (set / clear / guard)
  5. Clingy send (due → send → count increment → max cap)
  6. Exception handling (internal error → not "delivered")

Uses in-memory SQLite where possible. Mocks LLM/bridge calls.
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from agent.llm_types import LLMResponse, LLMUsage


# ── helpers ──────────────────────────────────────────────────────

class _FakeGateway:
    """LLMGateway stub for ChatAgent tests. Accepts JSON response content."""

    def __init__(self, response_json: str):
        self.response_json = response_json

    async def complete(self, request):
        return LLMResponse(
            content=self.response_json,
            provider="fake",
            model=request.model or "test",
            usage=LLMUsage(),
            latency_ms=1,
            finish_reason="stop",
        )


def _make_mock_gateway(json_content: str):
    return _FakeGateway(json_content)

def _make_mock_store():
    """Create an in-memory MemoryStore for testing."""
    from memory.store import MemoryStore
    store = MemoryStore(db_path=":memory:")
    return store


async def _init_store(store):
    await store._migrate()
    return store


# ══════════════════════════════════════════════════════════════════
# Scenario 1: Normal /chat user message
# ══════════════════════════════════════════════════════════════════

class TestNormalChatMessage:
    def test_is_real_user_inbound_passes_wechat(self):
        from main import _is_real_user_inbound
        assert _is_real_user_inbound("wxid_abc", "今天天气不错", "user", "wechat") is True

    def test_chat_agent_produces_reply(self):
        """ChatAgent with a valid message produces assistant output."""
        from agent.chat_agent import ChatAgent
        gw = _make_mock_gateway(
            '{"actions":[{"type":"message","text":"你好呀","delay":0}],"done":true}'
        )
        agent = ChatAgent(
            model="test", persona="test persona",
            personality="casual", temperature=0.7,
            llm_gateway=gw,
        )

        async def _run():
            return await agent.chat(
                user_message="你好", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = asyncio.run(_run())
        assert len(result["messages"]) >= 1
        assert result["messages"][0]["text"] == "你好呀"
        assert result["done"] is True
        assert result["skip_reply"] is False

    def test_chat_agent_records_turn_id(self):
        """Every chat turn gets a unique turn_id."""
        from agent.chat_agent import ChatAgent
        gw = _make_mock_gateway(
            '{"actions":[{"type":"message","text":"好","delay":0}],"done":true}'
        )
        agent = ChatAgent(
            model="test", persona="test", personality="",
            temperature=0.7, llm_gateway=gw,
        )

        async def _run():
            return await agent.chat(
                user_message="测试", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = asyncio.run(_run())
        assert result["turn_id"].startswith("turn_")
        assert result["llm_status"] == "ok"


# ══════════════════════════════════════════════════════════════════
# Scenario 2: User silent request
# ══════════════════════════════════════════════════════════════════

class TestUserSilentRequest:
    def test_chat_agent_skip_action_no_messages(self):
        """Model outputs skip → no messages, skip_reply=True."""
        from agent.chat_agent import ChatAgent
        gw = _make_mock_gateway(
            '{"actions":[{"type":"skip","reason":"用户明确不需要回复"}],"done":true}'
        )
        agent = ChatAgent(
            model="test", persona="test", personality="",
            temperature=0.7, llm_gateway=gw,
        )

        async def _run():
            return await agent.chat(
                user_message="好了 收到这条消息 不用给我回复", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = asyncio.run(_run())
        assert result["messages"] == []
        assert result["skip_reply"] is True
        assert "用户明确不需要回复" in result.get("skip_reason", "")
        # No fallback placeholder injected
        assert not any(
            "信号不好" in m.get("text", "") or "让我想想" in m.get("text", "")
            for m in result.get("messages", [])
        )

    def test_skip_no_fallback_event_for_silent_request(self):
        """Skip should record user_silent_request fallback event."""
        from agent.chat_agent import ChatAgent
        gw = _make_mock_gateway(
            '{"actions":[{"type":"skip","reason":"用户明确不需要回复"}],"done":true}'
        )
        agent = ChatAgent(
            model="test", persona="test", personality="",
            temperature=0.7, llm_gateway=gw,
        )

        async def _run():
            return await agent.chat(
                user_message="不用回复", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = asyncio.run(_run())
        events = result.get("fallback_events", [])
        types = [e.get("type") for e in events if isinstance(e, dict)]
        assert "user_silent_request" in types

    def test_top_level_skip_json_preserved(self):
        """Top-level {"skip":true} without actions field is preserved."""
        from utils.json_parser import parse_agent_output
        parsed = parse_agent_output('{"skip": true, "done": true}')
        assert parsed.get("skip") is True
        assert parsed["actions"] == []

    def test_is_real_user_inbound_rejects_empty(self):
        from main import _is_real_user_inbound
        assert _is_real_user_inbound("wxid_abc", "", "user", "wechat") is False
        assert _is_real_user_inbound("wxid_abc", "   ", "user", "wechat") is False

    def test_is_real_user_inbound_rejects_admin_proactive(self):
        from main import _is_real_user_inbound
        assert _is_real_user_inbound("wxid_abc", "hello", "admin", "wechat") is False
        assert _is_real_user_inbound("wxid_abc", "hello", "proactive", "wechat") is False
        assert _is_real_user_inbound("wxid_abc", "hello", "system", "wechat") is False


# ══════════════════════════════════════════════════════════════════
# Scenario 3: Proactive candidate send_status consistency
# ══════════════════════════════════════════════════════════════════

class TestProactiveCandidateConsistency:
    @pytest.mark.asyncio
    async def test_candidate_delivered_sets_sent_true(self):
        """update_proactive_candidate with send_status=delivered must set sent=true."""
        store = _make_mock_store()
        await _init_store(store)
        candidate_id = await store.create_proactive_candidate(
            reason_type="check_in",
            reason_detail="test",
            user_id="wxid_test",
            bot_id="test_bot",
        )
        await store.update_proactive_candidate(
            candidate_id,
            sent=True,
            send_status="delivered",
            turn_id="turn_abc",
            batch_id="batch_abc",
        )
        # Verify via the candidates list
        candidates = await store.get_recent_proactive_candidates(limit=5)
        found = [c for c in candidates if c["id"] == candidate_id]
        assert len(found) == 1
        assert found[0]["sent"] == 1
        assert found[0]["send_status"] == "delivered"
        assert found[0]["turn_id"] == "turn_abc"

    @pytest.mark.asyncio
    async def test_candidate_failed_does_not_set_sent(self):
        """Failed send must leave sent=false."""
        store = _make_mock_store()
        await _init_store(store)
        candidate_id = await store.create_proactive_candidate(
            reason_type="check_in",
            reason_detail="test",
            user_id="wxid_test",
            bot_id="test_bot",
        )
        await store.update_proactive_candidate(
            candidate_id,
            sent=False,
            send_status="failed",
        )
        candidates = await store.get_recent_proactive_candidates(limit=5)
        found = [c for c in candidates if c["id"] == candidate_id]
        assert found[0]["sent"] == 0
        assert found[0]["send_status"] == "failed"

    @pytest.mark.asyncio
    async def test_candidate_default_sent_is_false(self):
        """Newly created candidate has sent=false."""
        store = _make_mock_store()
        await _init_store(store)
        candidate_id = await store.create_proactive_candidate(
            reason_type="self_share",
            reason_detail="test",
            user_id="wxid_test",
            bot_id="test_bot",
        )
        candidates = await store.get_recent_proactive_candidates(limit=5)
        found = [c for c in candidates if c["id"] == candidate_id]
        assert found[0]["sent"] == 0

    @pytest.mark.asyncio
    async def test_candidate_sent_and_send_status_agree(self):
        """When send_status=delivered, sent must be true. When failed, sent=false."""
        store = _make_mock_store()
        await _init_store(store)

        # Delivered candidate
        c1 = await store.create_proactive_candidate(
            reason_type="check_in", reason_detail="ok",
            user_id="u1", bot_id="b1",
        )
        await store.update_proactive_candidate(c1, sent=True, send_status="delivered")

        # Failed candidate
        c2 = await store.create_proactive_candidate(
            reason_type="check_in", reason_detail="fail",
            user_id="u1", bot_id="b1",
        )
        await store.update_proactive_candidate(c2, sent=False, send_status="failed")

        candidates = await store.get_recent_proactive_candidates(limit=10)
        for c in candidates:
            if c["send_status"] == "delivered":
                assert c["sent"] == 1, f"candidate {c['id']}: delivered but sent=false"
            elif c["send_status"] == "failed":
                assert c["sent"] == 0, f"candidate {c['id']}: failed but sent=true"


# ══════════════════════════════════════════════════════════════════
# Scenario 4: Clingy awaiting state transitions
# ══════════════════════════════════════════════════════════════════

class TestClingyAwaitingTransitions:
    @pytest.mark.asyncio
    async def test_set_awaiting_on_delivered_reply(self):
        """After bot reply, awaiting_user_reply is set."""
        store = _make_mock_store()
        await _init_store(store)
        uid, bid = "wxid_test", "test_bot"
        await store.update_clingy_state(
            user_id=uid, bot_id=bid,
            awaiting_user_reply=1,
            last_proactive_sent_at=time.time(),
            last_proactive_intent="user_reply",
            next_clingy_due_at=time.time() + 1200,
            clingy_followup_count=0,
        )
        state = await store.get_clingy_state(uid, bid)
        assert state["awaiting_user_reply"] == 1
        assert state["last_proactive_intent"] == "user_reply"

    @pytest.mark.asyncio
    async def test_clear_awaiting_on_real_user_inbound(self):
        """clear_clingy_awaiting resets awaiting flag and count."""
        store = _make_mock_store()
        await _init_store(store)
        uid, bid = "wxid_test", "test_bot"
        await store.update_clingy_state(
            user_id=uid, bot_id=bid,
            awaiting_user_reply=1,
            clingy_followup_count=2,
            next_clingy_due_at=time.time() + 600,
        )
        await store.clear_clingy_awaiting(uid, bid)
        state = await store.get_clingy_state(uid, bid)
        assert state["awaiting_user_reply"] == 0
        assert state["next_clingy_due_at"] == 0
        assert state["clingy_followup_count"] == 0

    @pytest.mark.asyncio
    async def test_proactive_sets_awaiting_via_commit(self):
        """commit_proactive_send with non-clingy intent → awaiting=1."""
        store = _make_mock_store()
        await _init_store(store)
        from agent.scheduler import SchedulerAgent
        agent = SchedulerAgent(
            store=store, model="test", llm_gateway=MagicMock(),
            config={"proactive": {"generation": {"clingy_mode_enabled": True, "clingy_after_minutes": 20}, "clingy": {"max_followups_per_unanswered": 3}}},
        )
        agent.clingy_mode_enabled = True
        uid, bid = "wxid_test", "test_bot"
        await agent.commit_proactive_send(
            user_id=uid, bot_id=bid, topic_id="", intent="check_in",
        )
        state = await store.get_clingy_state(uid, bid)
        assert state["awaiting_user_reply"] == 1
        assert state["last_proactive_intent"] == "check_in"
        assert state["clingy_followup_count"] == 0
        assert state["next_clingy_due_at"] > 0

    @pytest.mark.asyncio
    async def test_clingy_count_increments_on_clingy_send(self):
        """commit_proactive_send with clingy_followup intent → count +1, awaiting stays."""
        store = _make_mock_store()
        await _init_store(store)
        from agent.scheduler import SchedulerAgent
        agent = SchedulerAgent(
            store=store, model="test", llm_gateway=MagicMock(),
            config={"proactive": {"generation": {"clingy_mode_enabled": True, "clingy_after_minutes": 20}, "clingy": {"max_followups_per_unanswered": 3}}},
        )
        agent.clingy_mode_enabled = True
        uid, bid = "wxid_test", "test_bot"

        # First set awaiting (simulating a prior proactive/chat reply)
        await store.update_clingy_state(
            user_id=uid, bot_id=bid,
            awaiting_user_reply=1,
            clingy_followup_count=0,
            last_proactive_intent="check_in",
        )
        # Send clingy followup #1
        await agent.commit_proactive_send(
            user_id=uid, bot_id=bid, topic_id="", intent="clingy_followup",
        )
        state = await store.get_clingy_state(uid, bid)
        assert state["clingy_followup_count"] == 1
        assert state["last_proactive_intent"] == "clingy_followup"
        # awaiting should NOT be cleared by clingy send
        assert state["awaiting_user_reply"] == 1

    @pytest.mark.asyncio
    async def test_clingy_max_stops_further_due(self):
        """At max_followups, next_clingy_due_at is set to 0."""
        store = _make_mock_store()
        await _init_store(store)
        from agent.scheduler import SchedulerAgent
        agent = SchedulerAgent(
            store=store, model="test", llm_gateway=MagicMock(),
            config={"proactive": {"generation": {"clingy_mode_enabled": True, "clingy_after_minutes": 20}, "clingy": {"max_followups_per_unanswered": 2}}},
        )
        agent.clingy_mode_enabled = True
        uid, bid = "wxid_test", "test_bot"

        await store.update_clingy_state(
            user_id=uid, bot_id=bid,
            awaiting_user_reply=1,
            clingy_followup_count=1,
            last_proactive_intent="check_in",
        )
        # This should be the 2nd (max) followup
        await agent.commit_proactive_send(
            user_id=uid, bot_id=bid, topic_id="", intent="clingy_followup",
        )
        state = await store.get_clingy_state(uid, bid)
        assert state["clingy_followup_count"] == 2
        # next_clingy_due_at should be 0 (no more scheduled)
        assert state["next_clingy_due_at"] == 0


# ══════════════════════════════════════════════════════════════════
# Scenario 5: Clingy check — due, send, block
# ══════════════════════════════════════════════════════════════════

class TestClingyCheckLogic:
    @pytest.mark.asyncio
    async def test_clingy_not_due_blocks_proactive(self):
        """When awaiting and not due, block_proactive is True."""
        from agent.scheduler import SchedulerAgent
        from datetime import datetime, timezone, timedelta
        CST = timezone(timedelta(hours=8))

        store = _make_mock_store()
        await _init_store(store)
        agent = SchedulerAgent(
            store=store, model="test", llm_gateway=MagicMock(),
            config={"proactive": {"generation": {"clingy_mode_enabled": True, "clingy_after_minutes": 20}, "clingy": {"max_followups_per_unanswered": 3}}},
        )
        uid, bid = "wxid_test", "test_bot"
        now = time.time()

        # Set awaiting with future due time
        await store.update_clingy_state(
            user_id=uid, bot_id=bid,
            awaiting_user_reply=1,
            next_clingy_due_at=now + 3600,  # 1 hour from now
            clingy_followup_count=0,
            last_proactive_intent="check_in",
        )

        # Create a turn to satisfy derived_awaiting
        await store.create_turn(
            turn_id="turn_test_1", user_id=uid, bot_id=bid,
            user_messages="", assistant_actions='[{"text":"hello","delay":0}]',
            tool_calls="[]", source="proactive", trigger="proactive_reason",
            send_status="delivered",
        )

        prereqs = {"user_id": uid, "bot_id": bid, "bot_active": True,
                    "proactive_enabled": True, "bridge_available": True,
                    "valid_contact": True}
        now_cst = datetime.now(CST)
        result = await agent.run_clingy_check(
            now_ts=now, now_cst=now_cst, prerequisites=prereqs,
            dialogue_state={"wait_state": "assistant_waiting_user"},
        )
        assert result["sent"] is False
        assert result["block_proactive"] is True
        assert "not yet due" in str(result.get("reason", ""))

    @pytest.mark.asyncio
    async def test_clingy_disabled_returns_false(self):
        """When clingy_mode_enabled=False, check returns immediately."""
        from agent.scheduler import SchedulerAgent
        from datetime import datetime, timezone, timedelta
        CST = timezone(timedelta(hours=8))

        store = _make_mock_store()
        await _init_store(store)
        agent = SchedulerAgent(
            store=store, model="test", llm_gateway=MagicMock(),
            config={"proactive": {"generation": {"clingy_mode_enabled": False}}},
        )
        prereqs = {"user_id": "u1", "bot_id": "b1"}
        result = await agent.run_clingy_check(
            now_ts=time.time(), now_cst=datetime.now(CST), prerequisites=prereqs,
        )
        assert result["sent"] is False
        assert "disabled" in str(result.get("reason", ""))

    @pytest.mark.asyncio
    async def test_max_followups_blocks_clingy(self):
        """At max count, clingy check returns False."""
        from agent.scheduler import SchedulerAgent
        from datetime import datetime, timezone, timedelta
        CST = timezone(timedelta(hours=8))

        store = _make_mock_store()
        await _init_store(store)
        agent = SchedulerAgent(
            store=store, model="test", llm_gateway=MagicMock(),
            config={"proactive": {"generation": {"clingy_mode_enabled": True, "clingy_after_minutes": 20}, "clingy": {"max_followups_per_unanswered": 2}}},
        )
        uid, bid = "wxid_test", "test_bot"
        now = time.time()

        await store.update_clingy_state(
            user_id=uid, bot_id=bid,
            awaiting_user_reply=1,
            next_clingy_due_at=now - 1,  # due now
            clingy_followup_count=2,     # at max
            last_proactive_intent="check_in",
        )

        # Create a turn for derived_awaiting
        await store.create_turn(
            turn_id="turn_test_max", user_id=uid, bot_id=bid,
            user_messages="", assistant_actions='[{"text":"x","delay":0}]',
            tool_calls="[]", source="proactive", trigger="proactive_reason",
            send_status="delivered",
        )

        prereqs = {"user_id": uid, "bot_id": bid, "bot_active": True,
                    "proactive_enabled": True, "bridge_available": True,
                    "valid_contact": True}
        result = await agent.run_clingy_check(
            now_ts=now, now_cst=datetime.now(CST), prerequisites=prereqs,
            dialogue_state={"wait_state": "assistant_waiting_user"},
        )
        assert result["sent"] is False
        assert "max reached" in str(result.get("reason", ""))


# ══════════════════════════════════════════════════════════════════
# Scenario 6: Exception / error path — no false "delivered"
# ══════════════════════════════════════════════════════════════════

class TestErrorPathNoFalseDelivered:
    def test_chat_agent_llm_error_produces_fallback_not_delivered(self):
        """LLM error → llm_status='error', fallback event recorded."""
        from agent.chat_agent import ChatAgent
        from agent.llm_types import LLMError
        gw = MagicMock()
        gw.complete = AsyncMock(side_effect=LLMError(
            kind="network_error", message="Connection timeout",
            provider="deepseek", retryable=True,
        ))
        agent = ChatAgent(
            model="test", persona="test", personality="",
            temperature=0.7, llm_gateway=gw,
        )

        async def _run():
            return await agent.chat(
                user_message="你好", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = asyncio.run(_run())
        assert result["llm_status"] == "error"
        assert len(result.get("fallback_events", [])) >= 1
        assert result["fallback_events"][0]["type"] == "llm_error"

    def test_chat_agent_llm_error_still_has_turn_id(self):
        """Even on error, a turn_id is generated for diagnostics."""
        from agent.chat_agent import ChatAgent
        from agent.llm_types import LLMError
        gw = MagicMock()
        gw.complete = AsyncMock(side_effect=LLMError(
            kind="timeout", message="Timeout",
            provider="deepseek", retryable=True,
        ))
        agent = ChatAgent(
            model="test", persona="test", personality="",
            temperature=0.7, llm_gateway=gw,
        )

        async def _run():
            return await agent.chat(
                user_message="你好", memories=[], history=[],
                relationship={}, trigger="user",
            )

        result = asyncio.run(_run())
        assert result["turn_id"].startswith("turn_")

    def test_json_parser_skip_preserved_on_missing_actions(self):
        """JSON with skip but no actions field → skip preserved, not lost."""
        from utils.json_parser import parse_agent_output
        # Model outputs top-level skip without actions array
        r1 = parse_agent_output('{"skip": true, "reason": "no reply needed", "done": true}')
        assert r1.get("skip") is True
        assert r1["actions"] == []
        assert r1.get("skip_reason") == "no reply needed"

        # Also check no_reply variant
        r2 = parse_agent_output('{"no_reply": true, "done": true}')
        assert r2.get("skip") is True
        assert r2["actions"] == []

        # Normal reply should still work
        r3 = parse_agent_output('{"actions":[{"type":"message","text":"hi"}],"done":true}')
        assert len(r3["actions"]) == 1

    @pytest.mark.asyncio
    async def test_update_turn_send_status_to_failed(self):
        """update_turn_send_status can set status to 'failed'."""
        store = _make_mock_store()
        await _init_store(store)
        await store.record_turn_diagnostics(
            turn_id="turn_err_test", user_id="u1", source="wechat",
            send_status="generated",
        )
        await store.update_turn_send_status("turn_err_test", "failed", error="timeout")
        diags = await store.get_recent_turn_diagnostics(limit=5, user_id="u1")
        found = [d for d in diags if d["turn_id"] == "turn_err_test"]
        assert len(found) == 1
        assert found[0]["send_status"] == "failed"
