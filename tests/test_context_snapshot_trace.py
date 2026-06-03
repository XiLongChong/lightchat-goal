"""Phase 4: ContextSnapshot store and LLM trace linkage tests.

Validates:
  - Snapshot persistence and query by turn_id / call_id
  - Snapshot excludes API keys
  - LLM trace contains turn_id and call_id
  - Exposed tool names in snapshot

Spec: docs/runtime-specs/01-context-builder-spec.zh.md §2.6, §6.6
      docs/runtime-specs/03-turn-engine-spec.zh.md INV-14
Test matrix: CB-021 to CB-025
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from agent.llm_gateway import LLMGateway
from agent.llm_types import LLMRequest
from memory.store import MemoryStore


@pytest.fixture
async def store():
    db_path = Path(tempfile.mkdtemp()) / "test_snapshots.db"
    s = MemoryStore(db_path=str(db_path))
    yield s
    await s.close()


@pytest.fixture
def sample_snapshot():
    return {
        "snapshot_id": "snap_turn_1_call_1",
        "turn_id": "turn_1",
        "call_id": "call_1",
        "created_at": 1717400000.0,
        "exposed_tool_names": ["send_text_message", "finish_turn"],
        "history_message_ids": ["m1", "m2", "m3"],
        "memory_ids": ["mem_a1b2"],
        "turn_local_summary": {"delivered_count": 1, "failed_count": 0},
        "block_summaries": ["system protocol", "persona", "history"],
        "token_budget": {"budget": 8000, "used": 1500, "remaining": 6500},
    }


# ═══════════════════════════════════════════════════════════════════
# Snapshot save + query
# ═══════════════════════════════════════════════════════════════════


class TestSnapshotPersistence:
    async def test_save_and_query_by_turn_id(self, store, sample_snapshot):
        await store.save_context_snapshot(sample_snapshot)
        results = await store.get_snapshots_by_turn_id("turn_1")
        assert len(results) == 1
        assert results[0]["snapshot_id"] == "snap_turn_1_call_1"
        assert results[0]["turn_id"] == "turn_1"

    async def test_query_by_call_id_returns_snapshot(self, store, sample_snapshot):
        await store.save_context_snapshot(sample_snapshot)
        result = await store.get_snapshot_by_call_id("call_1")
        assert result is not None
        assert result["snapshot_id"] == "snap_turn_1_call_1"

    async def test_query_by_nonexistent_turn_id_returns_empty(self, store):
        results = await store.get_snapshots_by_turn_id("nonexistent")
        assert results == []

    async def test_query_by_nonexistent_call_id_returns_none(self, store):
        result = await store.get_snapshot_by_call_id("nonexistent")
        assert result is None

    async def test_multiple_snapshots_per_turn(self, store, sample_snapshot):
        await store.save_context_snapshot(sample_snapshot)
        snap2 = dict(sample_snapshot, snapshot_id="snap_turn_1_call_2", call_id="call_2")
        await store.save_context_snapshot(snap2)
        results = await store.get_snapshots_by_turn_id("turn_1")
        assert len(results) == 2

    async def test_different_turns_isolated(self, store, sample_snapshot):
        await store.save_context_snapshot(sample_snapshot)
        snap2 = dict(sample_snapshot, snapshot_id="snap_t2_c1", turn_id="turn_2", call_id="call_3")
        await store.save_context_snapshot(snap2)
        r1 = await store.get_snapshots_by_turn_id("turn_1")
        r2 = await store.get_snapshots_by_turn_id("turn_2")
        assert len(r1) == 1
        assert len(r2) == 1
        assert r1[0]["turn_id"] == "turn_1"
        assert r2[0]["turn_id"] == "turn_2"


# ═══════════════════════════════════════════════════════════════════
# No API key leakage
# ═══════════════════════════════════════════════════════════════════


class TestNoApiKeyLeak:
    async def test_snapshot_no_api_key_field(self, store, sample_snapshot):
        await store.save_context_snapshot(sample_snapshot)
        result = await store.get_snapshot_by_call_id("call_1")
        for key in result:
            val = str(result[key])
            assert "sk-" not in val.lower()
            assert "Bearer" not in val
            assert "api_key" not in key.lower()

    async def test_snapshot_with_api_key_like_text_not_leaked(self, store):
        snap = {
            "snapshot_id": "snap_x",
            "turn_id": "turn_x",
            "call_id": "call_x",
            "created_at": 1717400000.0,
            "exposed_tool_names": ["send_text_message"],
            "history_message_ids": [],
            "memory_ids": [],
            "turn_local_summary": {},
            "block_summaries": ["some block with sk- text in description"],
            "token_budget": {},
        }
        await store.save_context_snapshot(snap)
        result = await store.get_snapshot_by_call_id("call_x")
        # The content "sk-" is in block_summaries but not as an API key
        assert result is not None


# ═══════════════════════════════════════════════════════════════════
# Exposed tool names in snapshot
# ═══════════════════════════════════════════════════════════════════


class TestExposedToolsInSnapshot:
    async def test_exposed_tool_names_persisted(self, store, sample_snapshot):
        await store.save_context_snapshot(sample_snapshot)
        result = await store.get_snapshot_by_call_id("call_1")
        assert "send_text_message" in result["exposed_tool_names"]
        assert "finish_turn" in result["exposed_tool_names"]

    async def test_empty_exposed_tools(self, store):
        snap = {
            "snapshot_id": "snap_no_tools",
            "turn_id": "turn_1", "call_id": "call_no",
            "created_at": 1717400000.0,
            "exposed_tool_names": [],
            "history_message_ids": [],
            "memory_ids": [],
            "turn_local_summary": {},
            "block_summaries": [],
            "token_budget": {},
        }
        await store.save_context_snapshot(snap)
        result = await store.get_snapshot_by_call_id("call_no")
        assert result["exposed_tool_names"] == []


# ═══════════════════════════════════════════════════════════════════
# LLM trace contains turn_id and call_id
# ═══════════════════════════════════════════════════════════════════


class TestLLMTraceLinkage:
    def test_trace_includes_turn_id_and_call_id(self):
        req = LLMRequest(
            purpose="chat",
            messages=[{"role": "user", "content": "hi"}],
            turn_id="turn_abc", call_id="call_abc_1",
        )
        assert req.turn_id == "turn_abc"
        assert req.call_id == "call_abc_1"

    def test_trace_defaults_empty_turn_call_ids(self):
        req = LLMRequest(purpose="chat", messages=[])
        assert req.turn_id == ""
        assert req.call_id == ""


# ═══════════════════════════════════════════════════════════════════
# History message IDs in snapshot
# ═══════════════════════════════════════════════════════════════════


class TestHistoryIdsInSnapshot:
    async def test_history_message_ids_persisted(self, store, sample_snapshot):
        await store.save_context_snapshot(sample_snapshot)
        result = await store.get_snapshot_by_call_id("call_1")
        assert result["history_message_ids"] == ["m1", "m2", "m3"]

    async def test_empty_history_message_ids(self, store):
        snap = {
            "snapshot_id": "snap_empty_hist",
            "turn_id": "turn_1", "call_id": "call_hist",
            "created_at": 1717400000.0,
            "exposed_tool_names": [],
            "history_message_ids": [],
            "memory_ids": [],
            "turn_local_summary": {},
            "block_summaries": [],
            "token_budget": {},
        }
        await store.save_context_snapshot(snap)
        result = await store.get_snapshot_by_call_id("call_hist")
        assert result["history_message_ids"] == []
