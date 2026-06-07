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


def test_context_builder_produces_reasonable_prompt():
    """build_chat_system_prompt produces a persona-only prompt."""
    from agent.context_builder import build_chat_system_prompt

    prompt = build_chat_system_prompt(
        persona="test persona",
        personality="",
        tool_names=["wechat_reply", "done", "get_time", "get_weather"],
    )

    assert "test persona" in prompt
    assert "wechat_reply" not in prompt
    assert "done" not in prompt
    assert "finish_turn" not in prompt
    assert "get_time" not in prompt
    assert "可用工具" not in prompt
    assert len(prompt) < 1000
