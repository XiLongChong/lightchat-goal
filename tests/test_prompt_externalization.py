from pathlib import Path


PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


def test_task_prompt_files_exist():
    required = [
        "scheduler_intent.md",
        "scheduler_message.md",
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


def test_scheduler_prompt_templates_format():
    from agent.scheduler import SchedulerAgent

    agent = SchedulerAgent(store=None, client=None, model="test")  # type: ignore[arg-type]

    intent_prompt = agent.intent_prompt_template.format(
        now_str="2026年05月26日 15:00 周2",
        topic_text="",
        topic_status="none",
        followup_count=0,
    )
    message_prompt = agent.message_prompt_template.format(
        intent="check_in",
        reason="test",
        now_str="2026年05月26日 15:00 周2",
        topic_text="",
        recent_context="用户: 刚在忙项目",
        familiarity=5,
        intent_rules="短一点",
    )

    assert "消息调度器" in intent_prompt
    assert "主动消息生成器" in message_prompt
    assert "你刚才说" in message_prompt


def test_memory_worker_loads_external_extraction_prompt():
    from agent.memory_worker import MemoryWorker

    worker = MemoryWorker(store=None, client=None, model="test")  # type: ignore[arg-type]

    assert "从以下对话中提取" in worker.extraction_prompt
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
    assert "正常 V2.1 路径" in prompt
    assert len(prompt) < 1500
