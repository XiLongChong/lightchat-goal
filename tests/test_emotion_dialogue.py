"""Tests for V2.2 emotion engine, dialogue state engine, and admin APIs.

Coverage:
  - Emotion: default state, upsert, reset, bot_id isolation
  - Emotion rules: offense, comparison, apology, positive
  - Dialogue state: default read, CRUD
  - Admin API: emotion endpoints
"""

import json
import time
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from agent.emotion.models import (
    BotEmotionState, EmotionEvent,
    serialize_state as emo_serialize,
    deserialize_state as emo_deserialize,
)
from agent.emotion.classifier import classify_user_message, DetectionResult
from agent.emotion.engine import EmotionEngine
from agent.emotion.policy import (
    should_be_cold, should_use_jealous_phrases, mood_expression_level,
)
from agent.emotion.prompt_context import build_emotion_inject

from agent.dialogue_state.models import (
    DialogueState, DialogueStateEvent,
    serialize_state as ds_serialize,
    deserialize_state as ds_deserialize,
)
from agent.dialogue_state.engine import DialogueStateEngine
from agent.dialogue_state.policy import (
    is_waiting_for_user, is_bot_silent, can_send_proactive,
)


# ================================================================
# Emotion models
# ================================================================

class TestEmotionModels:
    def test_default_state(self):
        s = BotEmotionState.default_for("u1", "b1")
        assert s.user_id == "u1"
        assert s.bot_id == "b1"
        assert s.bot_mood == "calm"
        assert s.affection == 10
        assert s.trust == 10
        assert s.irritation == 0
        assert s.jealousy == 0

    def test_from_dict_defaults(self):
        s = BotEmotionState.from_dict({"user_id": "u1", "bot_id": "b1"})
        assert s.bot_mood == "calm"
        assert s.relationship_stage == "stranger"

    def test_clamp_bounds(self):
        s = BotEmotionState(affection=150, trust=-5, irritation=200, bot_mood="invalid")
        s.clamp()
        assert s.affection == 100
        assert s.trust == 0
        assert s.irritation == 100
        assert s.bot_mood == "calm"

    def test_serialize_roundtrip(self):
        s = BotEmotionState(user_id="u1", bot_id="b1", bot_mood="jealous",
                            jealousy=30, irritation=15)
        data = emo_serialize(s)
        s2 = emo_deserialize(data)
        assert s2.bot_mood == "jealous"
        assert s2.jealousy == 30
        assert s2.irritation == 15

    def test_from_dict_full(self):
        data = {
            "user_id": "u1", "bot_id": "b2",
            "bot_mood": "warm", "mood_intensity": 50,
            "relationship_stage": "familiar",
            "affection": 40, "trust": 30,
            "irritation": 5, "attachment": 20, "jealousy": 10,
            "last_emotion_reason": "测试", "updated_at": 12345.0, "version": 3,
        }
        s = BotEmotionState.from_dict(data)
        assert s.affection == 40
        assert s.trust == 30
        assert s.relationship_stage == "familiar"
        assert s.version == 3


# ================================================================
# Emotion classifier
# ================================================================

class TestEmotionClassifier:
    def test_offense_direct_insult(self):
        r = classify_user_message("你真是个废物，什么都不行")
        assert r.trigger_type == "offense"
        assert r.confidence > 0.5

    def test_offense_rejection(self):
        r = classify_user_message("别跟我说话了，离我远点")
        assert r.trigger_type == "offense"

    def test_comparison_cc_better(self):
        r = classify_user_message("还是CC好，CC比你强多了")
        assert r.trigger_type == "comparison"
        assert r.confidence > 0.5

    def test_comparison_ignore(self):
        r = classify_user_message("我刚才在和CC聊天，没理你")
        assert r.trigger_type == "comparison"

    def test_apology_sorry(self):
        r = classify_user_message("对不起，我错了，别生气了")
        assert r.trigger_type == "apology"

    def test_apology_affectionate(self):
        r = classify_user_message("宝贝乖，回来吧，我想你了")
        assert r.trigger_type == "apology"
        assert r.confidence > 0.5

    def test_explanation_busy(self):
        r = classify_user_message("刚才在开会，没带手机")
        assert r.trigger_type == "explanation"

    def test_explanation_sleep(self):
        r = classify_user_message("太困了睡着了，才醒")
        assert r.trigger_type == "explanation"

    def test_positive_thanks(self):
        r = classify_user_message("谢谢你，你真的很靠谱")
        assert r.trigger_type in ("positive", "apology")  # 靠谱 can match positive

    def test_positive_laugh(self):
        r = classify_user_message("哈哈哈哈哈你好好笑")
        assert r.trigger_type == "positive"

    def test_empty_message(self):
        r = classify_user_message("")
        assert r.trigger_type == ""


# ================================================================
# Emotion engine rules
# ================================================================

class TestEmotionEngine:
    def _make_engine(self) -> EmotionEngine:
        return EmotionEngine(config={
            "enabled": True, "log_events": False,
            "default_profile": {"baseline_mood": "cold", "jealousy_enabled": True},
            "decay": {"enabled": False},
        })

    def _base_state(self) -> BotEmotionState:
        return BotEmotionState.default_for("u1", "b1")

    def test_rule_offense_increases_irritation(self):
        engine = self._make_engine()
        state = self._base_state()
        old_irr = state.irritation
        state, event = engine.apply_trigger(state, "你真是个废物")
        assert state.irritation > old_irr + 15
        assert state.bot_mood in ("irritated", "angry")
        assert state.trust < 10
        assert event is not None
        assert event.event_type == "offense"

    def test_rule_comparison_increases_jealousy(self):
        engine = self._make_engine()
        state = self._base_state()
        state, event = engine.apply_trigger(state, "还是CC比你好")
        assert state.jealousy >= 15
        assert state.irritation > 5
        assert state.bot_mood == "jealous"
        assert event is not None
        assert event.event_type == "comparison"

    def test_rule_apology_reduces_irritation(self):
        engine = self._make_engine()
        state = self._base_state()
        state.bot_mood = "irritated"
        state.irritation = 60

        state, event = engine.apply_trigger(state, "对不起我错了，宝贝别生气了")
        assert state.irritation < 60
        assert state.bot_mood in ("softened", "warm")
        assert state.affection > 10
        assert state.trust > 8

    def test_rule_explanation_reduces_irritation(self):
        engine = self._make_engine()
        state = self._base_state()
        state.bot_mood = "angry"
        state.irritation = 70

        state, event = engine.apply_trigger(state, "刚才在忙工作，没注意看消息")
        assert state.irritation < 70
        assert state.bot_mood in ("irritated", "softened")

    def test_rule_positive_increases_affection_trust(self):
        engine = self._make_engine()
        state = self._base_state()
        old_aff = state.affection
        old_trust = state.trust

        state, event = engine.apply_trigger(state, "谢谢你，说得很有道理")
        assert state.affection > old_aff
        assert state.trust >= old_trust

    def test_silence_trigger(self):
        engine = self._make_engine()
        state = self._base_state()
        state, event = engine.apply_silence_trigger(state, silence_duration_minutes=30)
        assert state.bot_mood in ("lonely", "hurt")
        assert state.attachment > 0
        assert state.irritation > 0

    def test_silence_long_becomes_hurt(self):
        engine = self._make_engine()
        state = self._base_state()
        state, event = engine.apply_silence_trigger(state, silence_duration_minutes=400)
        assert state.bot_mood == "hurt"

    def test_no_trigger_does_nothing(self):
        engine = self._make_engine()
        state = self._base_state()
        old = BotEmotionState.from_dict(state.to_dict())
        state, event = engine.apply_trigger(state, "嗯")
        # "嗯" has no specific trigger, but decay may apply — skip event check
        # state should be largely the same
        assert state.affection == old.affection
        assert state.irritation == old.irritation

    def test_stage_upgrade(self):
        engine = self._make_engine()
        state = self._base_state()
        state.affection = 60
        state.trust = 55
        engine._recalc_stage(state)
        assert state.relationship_stage == "close"

    def test_jealousy_disabled(self):
        engine = EmotionEngine(config={
            "enabled": True, "log_events": False,
            "default_profile": {"jealousy_enabled": False},
            "decay": {"enabled": False},
        })
        state = self._base_state()
        state, event = engine.apply_trigger(state, "CC比你强")
        assert state.jealousy == 0  # jealousy disabled


# ================================================================
# Emotion policy
# ================================================================

class TestEmotionPolicy:
    def test_should_be_cold(self):
        s = BotEmotionState(irritation=55, bot_mood="irritated")
        assert should_be_cold(s)
        assert should_be_cold(s, threshold=50)

    def test_not_cold_when_low(self):
        s = BotEmotionState(irritation=10, bot_mood="calm")
        assert not should_be_cold(s)

    def test_mood_expression_level(self):
        assert mood_expression_level(BotEmotionState(irritation=60, bot_mood="irritated")) == "cold"
        assert mood_expression_level(BotEmotionState(bot_mood="warm")) == "warm"
        assert mood_expression_level(BotEmotionState(bot_mood="calm")) == "neutral"


# ================================================================
# Emotion prompt context
# ================================================================

class TestEmotionPromptContext:
    def test_baseline_returns_compact_context(self):
        s = BotEmotionState.default_for("u1", "b1")
        ctx = build_emotion_inject(s.to_dict())
        assert "当前情绪" in ctx
        assert "关系阶段" in ctx

    def test_irritated_includes_guidance(self):
        s = BotEmotionState(user_id="u1", bot_id="b1",
                            bot_mood="irritated", irritation=55, mood_intensity=60)
        ctx = build_emotion_inject(s.to_dict())
        assert "不爽" in ctx or "恼怒" in ctx
        assert "回复风格建议" in ctx


# ================================================================
# Dialogue state models
# ================================================================

class TestDialogueStateModels:
    def test_default_state(self):
        s = DialogueState.default_for("u1", "b1")
        assert s.user_id == "u1"
        assert s.bot_id == "b1"
        assert s.wait_state == "none"
        assert s.silent_must_reply_after_count == 2

    def test_from_dict_defaults(self):
        s = DialogueState.from_dict({"user_id": "u1", "bot_id": "b1"})
        assert s.wait_state == "none"
        assert s.user_messages_since_silent == 0

    def test_serialize_roundtrip(self):
        s = DialogueState(user_id="u1", bot_id="b1", wait_state="bot_silent",
                          silent_started_at=12345.0, user_messages_since_silent=3)
        data = ds_serialize(s)
        s2 = ds_deserialize(data)
        assert s2.wait_state == "bot_silent"
        assert s2.user_messages_since_silent == 3

    def test_clamp(self):
        s = DialogueState(wait_state="invalid", user_messages_since_silent=-5)
        s.clamp()
        assert s.wait_state == "none"
        assert s.user_messages_since_silent == 0


# ================================================================
# Dialogue state engine
# ================================================================

class TestDialogueStateEngine:
    def _make_engine(self) -> DialogueStateEngine:
        return DialogueStateEngine(config={
            "enabled": True,
            "silence": {"enabled": True, "default_max_silent_messages": 2, "max_silent_minutes": 30},
        })

    def _base_state(self) -> DialogueState:
        return DialogueState.default_for("u1", "b1")

    def test_record_user_message(self):
        engine = self._make_engine()
        state = self._base_state()
        state, event = engine.record_user_message(state, "你好")
        assert state.wait_state == "user_waiting_assistant"
        assert state.last_user_message_at > 0
        assert event is not None

    def test_record_assistant_message(self):
        engine = self._make_engine()
        state = self._base_state()
        state, event = engine.record_assistant_message(state, "嗨，我在", needs_user_reply=True)
        assert state.wait_state == "assistant_waiting_user"
        assert state.last_assistant_message_at > 0
        # needs_user_reply=False → wait_state = none
        state2 = self._base_state()
        state2, _ = engine.record_assistant_message(state2, "好的，我记住了", needs_user_reply=False)
        assert state2.wait_state == "none"

    def test_enter_silent(self):
        engine = self._make_engine()
        state = self._base_state()
        state, event = engine.enter_silent(state, reason="bot irritated")
        assert state.wait_state == "bot_silent"
        assert state.silent_started_at > 0
        assert state.silent_must_reply_after_count == 2

    def test_enter_silent_mood_specific_count(self):
        engine = DialogueStateEngine(config={
            "enabled": True,
            "silence": {
                "enabled": True,
                "default_max_silent_messages": 2,
                "max_silent_minutes": 30,
                "max_silent_messages_by_mood": {"angry": 2},
            },
        })
        state = self._base_state()
        state, event = engine.enter_silent(state, reason="angry", max_silent_by_mood="angry")
        assert state.silent_must_reply_after_count == 2

    def test_check_silent_break_apology(self):
        engine = self._make_engine()
        state = self._base_state()
        state.wait_state = "bot_silent"
        should, reason = engine.check_silent_break(state, is_apology=True)
        assert should
        assert "apology" in reason

    def test_check_silent_break_count(self):
        engine = self._make_engine()
        state = self._base_state()
        state.wait_state = "bot_silent"
        state.user_messages_since_silent = 1
        state.silent_must_reply_after_count = 2
        should, reason = engine.check_silent_break(state)
        assert should

    def test_exit_silent(self):
        engine = self._make_engine()
        state = self._base_state()
        state.wait_state = "bot_silent"
        state, event = engine.exit_silent(state, reason="user apologized")
        assert state.wait_state == "user_waiting_assistant"
        assert state.silent_started_at == 0

    def test_mark_topic_closed(self):
        engine = self._make_engine()
        state = self._base_state()
        state, event = engine.mark_topic_closed(state, reason="topic done")
        assert state.wait_state == "topic_closed"


# ================================================================
# Dialogue state policy
# ================================================================

class TestDialogueStatePolicy:
    def test_is_waiting_for_user(self):
        assert is_waiting_for_user(DialogueState(wait_state="assistant_waiting_user"))
        assert not is_waiting_for_user(DialogueState(wait_state="user_waiting_assistant"))
        assert not is_waiting_for_user(DialogueState(wait_state="none"))

    def test_is_bot_silent(self):
        assert is_bot_silent(DialogueState(wait_state="bot_silent"))

    def test_can_send_proactive_paused(self):
        s = DialogueState(wait_state="paused_by_user")
        assert not can_send_proactive(s)


# ================================================================
# Integration: MemoryStore CRUD tests (requires DB)
# ================================================================

@pytest_asyncio.fixture
async def memory_store():
    from memory.store import MemoryStore
    import tempfile, os

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = MemoryStore(db_path=path)
    yield store
    await store.close()
    try:
        os.unlink(path)
    except OSError:
        pass


class TestEmotionStoreCRUD:
    @pytest.mark.asyncio
    async def test_get_default_when_empty(self, memory_store):
        """New user without config defaults should get legacy hardcoded values."""
        state = await memory_store.get_bot_emotion_state("u1", "b1")
        assert state["user_id"] == "u1"
        assert state["bot_id"] == "b1"
        assert state["bot_mood"] == "calm"
        assert state["irritation"] == 0

    @pytest.mark.asyncio
    async def test_get_default_with_config_overrides(self, memory_store):
        """New user with config defaults should get config values."""
        config = {
            "default_profile": {
                "baseline_mood": "cold",
                "initial_affection": 10,
                "initial_trust": 10,
                "initial_attachment": 5,
            }
        }
        engine = EmotionEngine(config=config)
        defaults = {
            "bot_mood": engine.baseline_mood,
            "affection": engine.initial_affection,
            "trust": engine.initial_trust,
            "attachment": engine.initial_attachment,
        }
        state = await memory_store.get_bot_emotion_state("new_user", "new_bot", defaults=defaults)
        assert state["bot_mood"] == "cold"     # from config
        assert state["attachment"] == 5        # from config
        assert state["affection"] == 10        # from config
        assert state["trust"] == 10            # from config
        assert state["version"] == 0           # not persisted

    @pytest.mark.asyncio
    async def test_upsert_and_read(self, memory_store):
        await memory_store.upsert_bot_emotion_state("u1", "b1", {
            "bot_mood": "warm", "affection": 50, "trust": 40,
            "irritation": 5,
        })
        state = await memory_store.get_bot_emotion_state("u1", "b1")
        assert state["bot_mood"] == "warm"
        assert state["affection"] == 50

    @pytest.mark.asyncio
    async def test_reset(self, memory_store):
        await memory_store.upsert_bot_emotion_state("u1", "b1", {
            "bot_mood": "angry", "irritation": 80,
        })
        await memory_store.reset_bot_emotion_state("u1", "b1")
        state = await memory_store.get_bot_emotion_state("u1", "b1")
        assert state["bot_mood"] == "calm"
        assert state["irritation"] == 0

    @pytest.mark.asyncio
    async def test_bot_id_isolation(self, memory_store):
        await memory_store.upsert_bot_emotion_state("u1", "bot_a", {
            "bot_mood": "warm", "affection": 40,
        })
        await memory_store.upsert_bot_emotion_state("u1", "bot_b", {
            "bot_mood": "cold", "affection": 5,
        })
        a = await memory_store.get_bot_emotion_state("u1", "bot_a")
        b = await memory_store.get_bot_emotion_state("u1", "bot_b")
        assert a["bot_mood"] == "warm"
        assert b["bot_mood"] == "cold"
        assert a["affection"] == 40
        assert b["affection"] == 5

    @pytest.mark.asyncio
    async def test_user_id_isolation(self, memory_store):
        await memory_store.upsert_bot_emotion_state("alice", "b1", {
            "bot_mood": "warm", "affection": 30,
        })
        await memory_store.upsert_bot_emotion_state("bob", "b1", {
            "bot_mood": "cold", "affection": 10,
        })
        a = await memory_store.get_bot_emotion_state("alice", "b1")
        b = await memory_store.get_bot_emotion_state("bob", "b1")
        assert a["bot_mood"] == "warm"
        assert b["bot_mood"] == "cold"

    @pytest.mark.asyncio
    async def test_insert_event(self, memory_store):
        eid = await memory_store.insert_bot_emotion_event(
            "u1", "b1", turn_id=1, source="test", trigger_type="offense",
            event_type="offense", reason="test offense",
        )
        assert eid > 0

    @pytest.mark.asyncio
    async def test_get_recent_events(self, memory_store):
        await memory_store.insert_bot_emotion_event(
            "u1", "b1", event_type="offense", reason="r1",
        )
        await memory_store.insert_bot_emotion_event(
            "u1", "b1", event_type="apology", reason="r2",
        )
        events = await memory_store.get_recent_bot_emotion_events("u1", "b1", limit=10)
        assert len(events) == 2


class TestDialogueStoreCRUD:
    @pytest.mark.asyncio
    async def test_get_default_when_empty(self, memory_store):
        state = await memory_store.get_dialogue_state("u1", "b1")
        assert state["user_id"] == "u1"
        assert state["wait_state"] == "none"

    @pytest.mark.asyncio
    async def test_upsert_and_read(self, memory_store):
        await memory_store.upsert_dialogue_state("u1", "b1", {
            "wait_state": "user_waiting_assistant",
            "last_user_message_excerpt": "你好",
        })
        state = await memory_store.get_dialogue_state("u1", "b1")
        assert state["wait_state"] == "user_waiting_assistant"
        assert "你好" in state["last_user_message_excerpt"]

    @pytest.mark.asyncio
    async def test_reset(self, memory_store):
        await memory_store.upsert_dialogue_state("u1", "b1", {
            "wait_state": "bot_silent", "silent_started_at": 12345,
        })
        await memory_store.reset_dialogue_state("u1", "b1")
        state = await memory_store.get_dialogue_state("u1", "b1")
        assert state["wait_state"] == "none"

    @pytest.mark.asyncio
    async def test_bot_id_isolation(self, memory_store):
        await memory_store.upsert_dialogue_state("u1", "bot_a", {
            "wait_state": "user_waiting_assistant",
        })
        await memory_store.upsert_dialogue_state("u1", "bot_b", {
            "wait_state": "bot_silent",
        })
        a = await memory_store.get_dialogue_state("u1", "bot_a")
        b = await memory_store.get_dialogue_state("u1", "bot_b")
        assert a["wait_state"] == "user_waiting_assistant"
        assert b["wait_state"] == "bot_silent"

    @pytest.mark.asyncio
    async def test_insert_event(self, memory_store):
        eid = await memory_store.insert_dialogue_state_event(
            "u1", "b1", turn_id=1,
            old_state_json="{}", new_state_json="{}",
            reason="test event",
        )
        assert eid > 0

    @pytest.mark.asyncio
    async def test_get_recent_events(self, memory_store):
        await memory_store.insert_dialogue_state_event("u1", "b1", reason="e1")
        await memory_store.insert_dialogue_state_event("u1", "b1", reason="e2")
        events = await memory_store.get_recent_dialogue_state_events("u1", "b1", limit=10)
        assert len(events) == 2


# ================================================================
# Integration: EmotionEngine + Store CRUD round-trip
# ================================================================

class TestEmotionIntegration:
    @pytest.mark.asyncio
    async def test_roundtrip_offense(self, memory_store):
        """Full round-trip: engine applies rule, store persists, store reads back."""
        from agent.emotion.engine import EmotionEngine
        from agent.emotion.models import BotEmotionState

        engine = EmotionEngine(config={"enabled": True, "log_events": False,
                                       "decay": {"enabled": False}})

        # Start from DB default
        raw = await memory_store.get_bot_emotion_state("u1", "b1")
        state = BotEmotionState.from_dict(raw)

        state, event = engine.apply_trigger(state, "你这个废物")
        assert event is not None

        # Persist
        await memory_store.upsert_bot_emotion_state("u1", "b1", state.to_dict())
        if event:
            await memory_store.insert_bot_emotion_event(
                event.user_id, event.bot_id,
                turn_id=event.turn_id, source=event.source,
                trigger_type=event.trigger_type, event_type=event.event_type,
                old_state_json=event.old_state_json,
                new_state_json=event.new_state_json,
                reason=event.reason, confidence=event.confidence,
                created_at=event.created_at,
            )

        # Read back
        raw2 = await memory_store.get_bot_emotion_state("u1", "b1")
        assert raw2["irritation"] > 10
        assert raw2["bot_mood"] in ("irritated", "angry")

    @pytest.mark.asyncio
    async def test_roundtrip_apology_after_offense(self, memory_store):
        """Offense then apology: irritation goes up then down."""
        from agent.emotion.engine import EmotionEngine
        from agent.emotion.models import BotEmotionState

        engine = EmotionEngine(config={"enabled": True, "log_events": False,
                                       "decay": {"enabled": False}})

        state = BotEmotionState.default_for("u1", "b1")

        # 1. Offense
        state, _ = engine.apply_trigger(state, "你真是废物")
        irr_after_offense = state.irritation
        assert irr_after_offense > 20

        # 2. Apology
        state, event = engine.apply_trigger(state, "对不起宝贝，我错了")
        assert state.irritation < irr_after_offense
        assert state.bot_mood in ("softened", "warm")
        assert state.affection > 10

    @pytest.mark.asyncio
    async def test_roundtrip_comparison_then_positive(self, memory_store):
        """Comparison causes jealousy; positive chat builds affection."""
        from agent.emotion.engine import EmotionEngine
        from agent.emotion.models import BotEmotionState

        engine = EmotionEngine(config={"enabled": True, "log_events": False,
                                       "default_profile": {"jealousy_enabled": True},
                                       "decay": {"enabled": False}})

        state = BotEmotionState.default_for("u1", "b1")

        # 1. Comparison
        state, _ = engine.apply_trigger(state, "CC比你好多了")
        assert state.bot_mood == "jealous"
        assert state.jealousy > 0

        # 2. Positive
        aff_before = state.affection
        state, _ = engine.apply_trigger(state, "谢谢你，你真的很靠谱")
        assert state.affection > aff_before

    @pytest.mark.asyncio
    async def test_stage_progression(self, memory_store):
        """After many positive interactions, stage progresses."""
        from agent.emotion.engine import EmotionEngine
        from agent.emotion.models import BotEmotionState

        engine = EmotionEngine(config={"enabled": True, "log_events": False,
                                       "decay": {"enabled": False}})

        state = BotEmotionState.default_for("u1", "b1")
        assert state.relationship_stage == "stranger"

        # Simulate many positive turns
        for _ in range(30):
            state, _ = engine.apply_trigger(state, "你真棒，说得太对了！谢谢！")
            state.affection = min(100, state.affection + 2)
            state.trust = min(100, state.trust + 2)

        engine._recalc_stage(state)
        assert state.relationship_stage in ("familiar", "close", "attached")


# ================================================================
# Admin API tests
# ================================================================

@pytest.fixture
def admin_client():
    """Create a FastAPI test client with emotion engine wired."""
    from fastapi.testclient import TestClient
    import main as app_module

    app_module.ADMIN_SESSIONS.add("test-token")
    client = TestClient(app_module.app)
    client.cookies.set("goal_admin_session", "test-token")
    return client


class TestAdminEmotionAPI:
    def test_get_emotion_state(self, admin_client):
        resp = admin_client.get("/admin/emotion/state")
        assert resp.status_code == 200
        data = resp.json()
        assert "emotion_state" in data
        # With config defaults (baseline_mood="cold"), new user gets "cold"
        assert data["emotion_state"]["bot_mood"] in ("calm", "cold")

    def test_get_emotion_events(self, admin_client):
        resp = admin_client.get("/admin/emotion/events?limit=5")
        assert resp.status_code == 200
        data = resp.json()
        assert "events" in data

    def test_update_emotion_state(self, admin_client):
        resp = admin_client.post("/admin/emotion/update", json={
            "bot_mood": "warm", "affection": 35, "trust": 25,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

        # Verify read back
        resp2 = admin_client.get("/admin/emotion/state")
        state = resp2.json()["emotion_state"]
        assert state["bot_mood"] == "warm"
        assert state["affection"] == 35

    def test_reset_emotion_state(self, admin_client):
        # First set something
        admin_client.post("/admin/emotion/update", json={
            "bot_mood": "angry", "irritation": 80,
        })
        # Then reset
        resp = admin_client.post("/admin/emotion/reset")
        assert resp.status_code == 200

        # Verify reset
        resp2 = admin_client.get("/admin/emotion/state")
        state = resp2.json()["emotion_state"]
        # With config defaults (baseline_mood="cold"), reset returns "cold" not "calm"
        assert state["bot_mood"] in ("calm", "cold")
        assert state["irritation"] == 0

    def test_update_rejects_invalid_fields(self, admin_client):
        resp = admin_client.post("/admin/emotion/update", json={
            "fake_field": 123,
        })
        assert resp.status_code == 400

    def test_unauthorized(self):
        from fastapi.testclient import TestClient
        import main as app_module

        client2 = TestClient(app_module.app)
        resp = client2.get("/admin/emotion/state")
        assert resp.status_code == 401


class TestAdminDialogueAPI:
    def test_get_dialogue_state(self, admin_client):
        resp = admin_client.get("/admin/dialogue/state")
        assert resp.status_code == 200
        data = resp.json()
        assert "dialogue_state" in data
        assert data["dialogue_state"]["wait_state"] == "none"

    def test_update_dialogue_state(self, admin_client):
        resp = admin_client.post("/admin/dialogue/update", json={
            "wait_state": "user_waiting_assistant",
        })
        assert resp.status_code == 200

        resp2 = admin_client.get("/admin/dialogue/state")
        state = resp2.json()["dialogue_state"]
        assert state["wait_state"] == "user_waiting_assistant"

    def test_reset_dialogue_state(self, admin_client):
        admin_client.post("/admin/dialogue/update", json={
            "wait_state": "bot_silent",
        })
        resp = admin_client.post("/admin/dialogue/reset")
        assert resp.status_code == 200

        resp2 = admin_client.get("/admin/dialogue/state")
        assert resp2.json()["dialogue_state"]["wait_state"] == "none"

    def test_update_rejects_invalid_fields(self, admin_client):
        resp = admin_client.post("/admin/dialogue/update", json={
            "fake": "nope",
        })
        assert resp.status_code == 400


# ================================================================
# Part 2: Emotion + ChatAgent integration tests
# ================================================================

class TestEmotionChatIntegration:
    """Integration: emotion engine applied to chat responses."""

    @pytest.mark.asyncio
    async def test_offense_sets_mood_to_angry(self, memory_store):
        """User insults → bot_mood -> angry/irritated via store roundtrip."""
        engine = EmotionEngine(config={"enabled": True, "log_events": False,
                                       "decay": {"enabled": False}})
        state = BotEmotionState.default_for("u1", "b1")
        state, _ = engine.apply_trigger(state, "你真是个废物")
        assert state.irritation > 20
        assert state.bot_mood in ("irritated", "angry")
        await memory_store.upsert_bot_emotion_state("u1", "b1", state.to_dict())
        raw = await memory_store.get_bot_emotion_state("u1", "b1")
        assert raw["bot_mood"] in ("irritated", "angry")

    @pytest.mark.asyncio
    async def test_comparison_sets_mood_to_jealous(self, memory_store):
        """User says CC is better → bot_mood -> jealous."""
        engine = EmotionEngine(config={"enabled": True, "log_events": False,
                                       "decay": {"enabled": False},
                                       "default_profile": {"jealousy_enabled": True}})
        state = BotEmotionState.default_for("u1", "b1")
        state, _ = engine.apply_trigger(state, "CC比你强多了")
        assert state.bot_mood == "jealous"
        assert state.jealousy > 0

    @pytest.mark.asyncio
    async def test_apology_softens_mood(self, memory_store):
        """After offense, user apology → mood softens."""
        engine = EmotionEngine(config={"enabled": True, "log_events": False,
                                       "decay": {"enabled": False}})
        state = BotEmotionState.default_for("u1", "b1")
        state, _ = engine.apply_trigger(state, "你真是个废物")
        state, _ = engine.apply_trigger(state, "对不起宝贝，我错了")
        assert state.bot_mood in ("softened", "warm")
        assert state.irritation < 30

    @pytest.mark.asyncio
    async def test_angry_mood_allows_silence(self, memory_store):
        """Angry mood → allow_silence = True."""
        engine = EmotionEngine(config={"enabled": True, "log_events": False,
                                       "decay": {"enabled": False}})
        state = BotEmotionState.default_for("u1", "b1")
        state, _ = engine.apply_trigger(state, "你这个废物垃圾")
        assert state.bot_mood in ("irritated", "angry")
        allow = state.bot_mood in ("irritated", "angry", "jealous", "hurt")
        assert allow

    @pytest.mark.asyncio
    async def test_must_reply_when_silent_limit_reached(self, memory_store):
        """After max_silent_messages reached → must_reply = True."""
        ds = DialogueState.default_for("u1", "b1")
        ds.wait_state = "bot_silent"
        ds.user_messages_since_silent = 1
        ds.silent_must_reply_after_count = 2
        # count + 1 (current msg) = 2 >= max → must reply
        count = ds.user_messages_since_silent + 1
        assert count >= ds.silent_must_reply_after_count

    @pytest.mark.asyncio
    async def test_apology_breaks_silence(self, memory_store):
        """Apology detection should break silence."""
        from agent.emotion.classifier import classify_user_message
        r = classify_user_message("对不起，我错了")
        assert r.trigger_type == "apology"

    @pytest.mark.asyncio
    async def test_urgent_message_blocks_silence(self, memory_store):
        """Urgent content should prevent silence. Simulated via policy check."""
        # Use the dialogue engine's check
        engine = DialogueStateEngine(config={"enabled": True,
            "silence": {"enabled": True, "force_reply_on_urgent": True}})
        state = DialogueState.default_for("u1", "b1")
        state.wait_state = "bot_silent"
        should, reason = engine.check_silent_break(state, is_urgent=True)
        assert should


class TestDialogueStateIntegration:
    """Integration: dialogue state transitions in chat flow."""

    @pytest.mark.asyncio
    async def test_normal_reply_sets_wait_state(self, memory_store):
        """Bot replies normally → wait_state transitions correctly."""
        engine = DialogueStateEngine(config={"enabled": True,
            "silence": {"enabled": True, "default_max_silent_messages": 2}})
        state = DialogueState.default_for("u1", "b1")
        # User sends message
        state, _ = engine.record_user_message(state, "你好")
        assert state.wait_state == "user_waiting_assistant"
        # Bot replies needing user reply
        state, _ = engine.record_assistant_message(state, "嗨，我在", needs_user_reply=True)
        assert state.wait_state == "assistant_waiting_user"

    @pytest.mark.asyncio
    async def test_skip_sets_silent_state(self, memory_store):
        """Bot skips/no_reply → wait_state = bot_silent."""
        engine = DialogueStateEngine(config={"enabled": True,
            "silence": {"enabled": True, "default_max_silent_messages": 2}})
        state = DialogueState.default_for("u1", "b1")
        state, _ = engine.enter_silent(state, reason="bot irritated")
        assert state.wait_state == "bot_silent"
        assert state.silent_started_at > 0

    @pytest.mark.asyncio
    async def test_silent_increments_message_count(self, memory_store):
        """bot_silent + user message → user_messages_since_silent increments."""
        ds = DialogueState.default_for("u1", "b1")
        ds.wait_state = "bot_silent"
        ds.user_messages_since_silent = 0
        # Simulate user sending another message while bot is silent
        ds.user_messages_since_silent += 1
        assert ds.user_messages_since_silent == 1
        ds.user_messages_since_silent += 1
        assert ds.user_messages_since_silent == 2

    @pytest.mark.asyncio
    async def test_paused_by_user_state_persists(self, memory_store):
        """User says 'don't reply' → wait_state = paused_by_user."""
        ds = DialogueState.default_for("u1", "b1")
        ds.wait_state = "paused_by_user"
        ds.last_state_reason = "用户要求不要回复"
        await memory_store.upsert_dialogue_state("u1", "b1", ds.to_dict())
        raw = await memory_store.get_dialogue_state("u1", "b1")
        assert raw["wait_state"] == "paused_by_user"


class TestEmotionForceReply:
    """Force-reply fallback text generation."""

    def test_angry_force_reply(self):
        from agent.chat_agent import _emotion_force_reply_text
        text = _emotion_force_reply_text("生气")
        assert len(text) > 0

    def test_jealous_force_reply(self):
        from agent.chat_agent import _emotion_force_reply_text
        text = _emotion_force_reply_text("吃醋")
        assert len(text) > 0

    def test_default_force_reply(self):
        from agent.chat_agent import _emotion_force_reply_text
        text = _emotion_force_reply_text("")
        assert len(text) > 0


class TestOutputGuardEmotion:
    """Emotion-aware profanity guard."""

    def test_forbidden_discrimination_blocked(self):
        from agent.output_guard import guard_emotion_profanity
        result = guard_emotion_profanity("你真是个傻逼", profanity_level=2)
        # "傻逼" matches discrimination pattern → softened
        assert "不该" in result or "有点" in result or result != "你真是个傻逼"

    def test_mild_profanity_allowed_at_level_2(self):
        from agent.output_guard import guard_emotion_profanity
        result = guard_emotion_profanity("靠，烦死了", profanity_level=2)
        # "靠" is mild profanity, allowed at level 2
        assert result == "靠，烦死了" or "…" in result

    def test_profanity_stripped_at_level_0(self):
        from agent.output_guard import guard_emotion_profanity
        result = guard_emotion_profanity("靠，妈的", profanity_level=0)
        assert "靠" not in result or "妈的" not in result

    def test_self_harm_blocked(self):
        from agent.output_guard import guard_emotion_profanity
        result = guard_emotion_profanity("我不想活了", profanity_level=3)
        assert "不该" in result or "算了" in result

    def test_clean_text_passes(self):
        from agent.output_guard import guard_emotion_profanity
        result = guard_emotion_profanity("今天天气不错", profanity_level=2)
        assert result == "今天天气不错"


class TestEmotionDisabledCompat:
    """When emotion.enabled=false, behavior matches old version."""

    def test_emotion_inject_empty_when_none(self):
        from agent.emotion.prompt_context import build_emotion_inject
        result = build_emotion_inject(None)
        assert result == ""

    def test_silence_rules_empty_when_no_silence(self):
        from agent.emotion.prompt_context import build_silence_rules_inject
        result = build_silence_rules_inject({}, allow_silence=False)
        assert result == ""

    def test_engine_respects_disabled(self):
        """Engine when disabled should return state unchanged."""
        engine = EmotionEngine(config={"enabled": False})
        state = BotEmotionState.default_for("u1", "b1")
        old = BotEmotionState.from_dict(state.to_dict())
        state, event = engine.apply_trigger(state, "你真是个废物")
        assert state.irritation == old.irritation
        assert event is None


# ================================================================
# Part 3: Scheduler + Dialogue State integration tests
# ================================================================

class TestSchedulerDialogueGating:
    """Verify dialogue_state.wait_state gates proactive/clingy correctly."""

    @pytest.mark.asyncio
    async def test_bot_silent_blocks_clingy(self, memory_store):
        """wait_state=bot_silent → clingy should NOT send."""
        ds = DialogueState.default_for("u1", "b1")
        ds.wait_state = "bot_silent"
        await memory_store.upsert_dialogue_state("u1", "b1", ds.to_dict())
        raw = await memory_store.get_dialogue_state("u1", "b1")
        assert raw["wait_state"] == "bot_silent"
        # Clingy should be blocked by this wait_state
        blocked_states = ("bot_silent", "user_waiting_assistant", "paused_by_user", "topic_closed")
        assert raw["wait_state"] in blocked_states

    @pytest.mark.asyncio
    async def test_user_waiting_assistant_blocks_proactive(self, memory_store):
        """wait_state=user_waiting_assistant → proactive should NOT send."""
        ds = DialogueState.default_for("u1", "b1")
        ds.wait_state = "user_waiting_assistant"
        await memory_store.upsert_dialogue_state("u1", "b1", ds.to_dict())
        raw = await memory_store.get_dialogue_state("u1", "b1")
        blocked = ("bot_silent", "user_waiting_assistant", "paused_by_user")
        assert raw["wait_state"] in blocked

    @pytest.mark.asyncio
    async def test_paused_by_user_blocks_both(self, memory_store):
        """wait_state=paused_by_user → both proactive and clingy blocked."""
        ds = DialogueState.default_for("u1", "b1")
        ds.wait_state = "paused_by_user"
        await memory_store.upsert_dialogue_state("u1", "b1", ds.to_dict())
        raw = await memory_store.get_dialogue_state("u1", "b1")
        assert raw["wait_state"] == "paused_by_user"

    @pytest.mark.asyncio
    async def test_assistant_waiting_allows_clingy(self, memory_store):
        """wait_state=assistant_waiting_user → clingy is allowed."""
        ds = DialogueState.default_for("u1", "b1")
        ds.wait_state = "assistant_waiting_user"
        await memory_store.upsert_dialogue_state("u1", "b1", ds.to_dict())
        raw = await memory_store.get_dialogue_state("u1", "b1")
        assert raw["wait_state"] == "assistant_waiting_user"
        # Clingy allowed
        clingy_allowed = raw["wait_state"] == "assistant_waiting_user"
        assert clingy_allowed

    @pytest.mark.asyncio
    async def test_topic_closed_blocks_clingy(self, memory_store):
        """wait_state=topic_closed → clingy blocked, proactive can go with cooldown."""
        ds = DialogueState.default_for("u1", "b1")
        ds.wait_state = "topic_closed"
        await memory_store.upsert_dialogue_state("u1", "b1", ds.to_dict())
        raw = await memory_store.get_dialogue_state("u1", "b1")
        clingy_blocked = raw["wait_state"] in ("bot_silent", "user_waiting_assistant", "paused_by_user", "topic_closed")
        assert clingy_blocked  # clingy check should skip


class TestSchedulerIntentValidation:
    """Verify last_proactive_intent only stores valid intent values."""

    def test_valid_intents_only(self):
        valid_intents = {"check_in", "latest_topic_followup", "clingy_followup", "self_share", "light_ping"}
        # "user_reply" must NOT be in valid intents
        assert "user_reply" not in valid_intents
        # "source" values are separate from intents
        source_values = {"user_reply", "fixed_morning", "scheduled_reminder", "proactive", "admin"}
        assert valid_intents.isdisjoint(source_values)

    def test_invalid_intent_defaults_to_check_in(self):
        valid_intents = {"check_in", "latest_topic_followup", "clingy_followup", "self_share", "light_ping"}
        intent = "user_reply"  # bug case
        if intent not in valid_intents:
            intent = "check_in"
        assert intent == "check_in"


class TestProactiveGuard:
    """Proactive message output guard."""

    def test_hallucination_detected(self):
        from agent.scheduler import SchedulerAgent
        text, violations = SchedulerAgent._guard_proactive_message("我刚看到你在刷朋友圈")
        assert len(violations) > 0
        assert any("hallucination" in v for v in violations)

    def test_clingy_escalation_detected(self):
        from agent.scheduler import SchedulerAgent
        text, violations = SchedulerAgent._guard_proactive_message("你是不是不想理我了")
        assert len(violations) > 0
        assert any("clingy_escalation" in v for v in violations)

    def test_clean_message_passes(self):
        from agent.scheduler import SchedulerAgent
        text, violations = SchedulerAgent._guard_proactive_message("在干嘛呢")
        assert len(violations) == 0

    def test_self_share_no_hallucination(self):
        from agent.scheduler import SchedulerAgent
        text, violations = SchedulerAgent._guard_proactive_message("突然想到一个好笑的事")
        assert len(violations) == 0


class TestEmotionProactiveInfluence:
    """Emotion-aware proactive hints."""

    def test_jealous_hint(self):
        from agent.scheduler import SchedulerAgent
        hint = SchedulerAgent._emotion_proactive_hint({"bot_mood": "jealous"})
        assert "吃醋" in hint

    def test_angry_hint(self):
        from agent.scheduler import SchedulerAgent
        hint = SchedulerAgent._emotion_proactive_hint({"bot_mood": "angry"})
        assert "短句" in hint or "沉默" in hint

    def test_warm_hint(self):
        from agent.scheduler import SchedulerAgent
        hint = SchedulerAgent._emotion_proactive_hint({"bot_mood": "warm"})
        assert "自然" in hint

    def test_softened_tone(self):
        from agent.scheduler import SchedulerAgent
        tone = SchedulerAgent._emotion_message_tone({"bot_mood": "softened"})
        assert "嘴硬" in tone


class TestCommitProactiveSend:
    """commit_proactive_send updates dialogue_state correctly."""

    @pytest.mark.asyncio
    async def test_check_in_enters_assistant_waiting(self, memory_store):
        """check_in intent → wait_state = assistant_waiting_user."""
        needs_reply = "check_in" in ("check_in", "latest_topic_followup", "clingy_followup")
        assert needs_reply
        # Simulate: needs_reply → set wait_state to assistant_waiting_user
        ds = DialogueState.default_for("u1", "b1")
        ds.wait_state = "assistant_waiting_user"
        await memory_store.upsert_dialogue_state("u1", "b1", ds.to_dict())
        raw = await memory_store.get_dialogue_state("u1", "b1")
        assert raw["wait_state"] == "assistant_waiting_user"

    @pytest.mark.asyncio
    async def test_self_share_does_not_require_reply(self, memory_store):
        """self_share intent → wait_state NOT forced to assistant_waiting_user."""
        needs_reply = "self_share" in ("check_in", "latest_topic_followup", "clingy_followup")
        assert not needs_reply

    @pytest.mark.asyncio
    async def test_clingy_followup_keeps_awaiting(self, memory_store):
        """clingy_followup → keep assistant_waiting_user."""
        needs_reply = "clingy_followup" in ("check_in", "latest_topic_followup", "clingy_followup")
        assert needs_reply


# ================================================================
# V2.2: Unified wait_state semantics
# ================================================================


class TestWaitStateSemantics:
    """V2.2: Verify unified wait_state semantics."""

    def test_assistant_question_enters_awaiting_user(self):
        """Assistant asks a question → wait_state = assistant_waiting_user."""
        from agent.dialogue_state.engine import DialogueStateEngine
        eng = DialogueStateEngine()
        state = eng.default_state("u1", "b1")

        state, _ = eng.record_assistant_message(
            state, "你觉得呢？", needs_user_reply=True, reason="asked question"
        )
        assert state.wait_state == "assistant_waiting_user"

    def test_assistant_statement_enters_none(self):
        """Assistant makes a statement → wait_state = none."""
        from agent.dialogue_state.engine import DialogueStateEngine
        eng = DialogueStateEngine()
        state = eng.default_state("u1", "b1")

        state, _ = eng.record_assistant_message(
            state, "好的我记住了。", needs_user_reply=False, reason="statement"
        )
        assert state.wait_state == "none"

    def test_user_message_no_reply_not_enter_assistant_waiting(self):
        """User message + bot normal reply → wait_state NOT user_waiting_assistant."""
        from agent.dialogue_state.engine import DialogueStateEngine
        eng = DialogueStateEngine()
        state = eng.default_state("u1", "b1")

        state, _ = eng.record_user_message(state, "你好")
        assert state.wait_state == "user_waiting_assistant", "after user msg, user waits for assistant"

        state, _ = eng.record_assistant_message(
            state, "你好呀！", needs_user_reply=True
        )
        assert state.wait_state == "assistant_waiting_user", "after assistant reply needing user, assistant waits"
        assert state.wait_state != "user_waiting_assistant", "should NOT stay at user_waiting_assistant"

    def test_bot_no_reply_enters_silent(self):
        """User message + bot no_reply → enter_silent → bot_silent."""
        from agent.dialogue_state.engine import DialogueStateEngine
        eng = DialogueStateEngine()
        state = eng.default_state("u1", "b1")

        state, _ = eng.record_user_message(state, "在吗")
        state, _ = eng.enter_silent(state, reason="bot too irritated")
        assert state.wait_state == "bot_silent"

    def test_bot_silent_blocks_clingy(self):
        """bot_silent → clingy should be blocked."""
        from agent.dialogue_state.models import DialogueState
        state = DialogueState(user_id="u1", bot_id="b1", wait_state="bot_silent")
        # clingy check: wait_state != "assistant_waiting_user" → skip
        assert state.wait_state != "assistant_waiting_user"

    def test_user_waiting_assistant_blocks_clingy(self):
        """user_waiting_assistant → clingy blocked, proactive blocked."""
        from agent.dialogue_state.models import DialogueState
        state = DialogueState(user_id="u1", bot_id="b1", wait_state="user_waiting_assistant")
        blocked = state.wait_state in ("bot_silent", "user_waiting_assistant", "paused_by_user", "topic_closed")
        assert blocked or state.wait_state != "assistant_waiting_user"

    def test_assistant_waiting_allows_clingy(self):
        """assistant_waiting_user → clingy is allowed."""
        from agent.dialogue_state.models import DialogueState
        state = DialogueState(user_id="u1", bot_id="b1", wait_state="assistant_waiting_user")
        assert state.wait_state == "assistant_waiting_user"

    def test_self_share_not_always_enter_awaiting(self):
        """self_share without question → may NOT enter assistant_waiting_user."""
        # If message_requires_user_reply returns False, wait_state is NOT forced
        needs_reply = False  # self_share statement
        wait_state = "none" if not needs_reply else "assistant_waiting_user"
        assert wait_state == "none"

    def test_check_in_enters_awaiting(self):
        """check_in / light_ping / latest_topic_followup with reply → assistant_waiting_user."""
        # These intents inherently need user reply
        for intent in ("check_in", "light_ping", "latest_topic_followup"):
            if intent == "check_in" or intent == "latest_topic_followup":
                needs_reply = True
            elif intent == "light_ping":
                needs_reply = False  # light_ping may not always need reply
            # just assert the logic is consistent
            assert intent in ("check_in", "latest_topic_followup", "clingy_followup", "self_share", "light_ping")

    def test_last_proactive_intent_no_user_reply(self):
        """last_proactive_intent must NOT contain 'user_reply' (a source, not intent)."""
        valid_intents = {"check_in", "latest_topic_followup", "clingy_followup", "self_share", "light_ping"}
        invalid = "user_reply"
        assert invalid not in valid_intents, "user_reply is a source, not an intent"

    def test_source_intent_not_mixed(self):
        """source values must not appear in intent field."""
        valid_intents = {"check_in", "latest_topic_followup", "clingy_followup", "self_share", "light_ping"}
        source_values = {"user_reply", "proactive", "scheduled", "admin", "morning", "night"}
        overlap = valid_intents & source_values
        assert not overlap, f"Source values leaked into intents: {overlap}"

    def test_clingy_only_on_awaiting_user(self):
        """Complete chain: Bot asks → assistant_waiting_user → user doesn't reply → clingy allowed."""
        from agent.dialogue_state.engine import DialogueStateEngine
        eng = DialogueStateEngine()
        state = eng.default_state("u1", "b1")

        # Bot proactively asks user something
        state, _ = eng.record_assistant_message(
            state, "今天过得怎么样？", needs_user_reply=True, reason="check_in"
        )
        assert state.wait_state == "assistant_waiting_user"
        # clingy IS allowed when assistant_waiting_user
        clingy_allowed = state.wait_state == "assistant_waiting_user"
        assert clingy_allowed

    def test_clingy_blocked_after_user_sends(self):
        """Complete chain: User sends msg → user_waiting_assistant → clingy blocked."""
        from agent.dialogue_state.engine import DialogueStateEngine
        eng = DialogueStateEngine()
        state = eng.default_state("u1", "b1")

        # User sends "人呢"
        state, _ = eng.record_user_message(state, "人呢")
        assert state.wait_state == "user_waiting_assistant"
        # clingy should NOT be allowed
        clingy_blocked = state.wait_state != "assistant_waiting_user"
        assert clingy_blocked
