from pathlib import Path
from unittest.mock import MagicMock


PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


def test_task_prompt_files_exist():
    required = [
        "memory_extract.md",
        "persona.md",
        "base_protocol.md",
        "trigger_context.md",
        "memory_policy.md",
        "tool_policy.md",
    ]

    for name in required:
        path = PROMPTS / name
        assert path.exists(), f"missing prompt file: {name}"
        assert path.read_text(encoding="utf-8").strip()


def test_scheduler_prompt_files_removed():
    removed = [
        "scheduler_intent.md",
        "scheduler_message.md",
        "proactive_message_v2.md",
        "proactive_plan.md",
        "scheduler_message.md",
    ]
    for name in removed:
        assert not (PROMPTS / name).exists(), f"removed prompt still exists: {name}"


def test_memory_worker_loads_external_extraction_prompt():
    from agent.memory_worker import MemoryWorker

    worker = MemoryWorker(store=None, model="test", llm_gateway=MagicMock())  # type: ignore[arg-type]

    assert "提取" in worker.extraction_prompt
    assert "输出格式" in worker.extraction_prompt


def test_legacy_context_builder_is_short_fallback():
    from agent.context_builder import build_system_prompt

    prompt = build_system_prompt(
        persona="test persona",
        personality="",
        memories=[],
        relationship={},
        tool_names=["wechat_reply"],
    )

    assert "Legacy fallback prompt" in prompt
    assert len(prompt) < 1500
