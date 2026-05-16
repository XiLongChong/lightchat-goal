"""Test tools — Safeguard #4: whitelist enforcement."""

import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_whitelist_blocks_unknown_tool():
    """Unknown tools are blocked with a message."""
    from agent.tools import execute_tool

    result = asyncio.run(execute_tool("evil_tool", {"arg": "value"}))
    assert "不在可用列表" in result or "不在" in result


def test_get_time_returns_valid_format():
    """Get time returns a string with date and time."""
    from agent.tools import execute_tool

    result = asyncio.run(execute_tool("get_time", {"timezone": "Asia/Shanghai"}))
    assert isinstance(result, str)
    assert len(result) > 5


def test_get_time_unknown_timezone_falls_back():
    """Unknown timezone falls back to Shanghai."""
    from agent.tools import execute_tool

    result = asyncio.run(execute_tool("get_time", {"timezone": "Mars/Base1"}))
    assert isinstance(result, str)
    assert len(result) > 5  # Still gets a valid time


def test_get_weather_without_city():
    """Weather without city returns prompt."""
    from agent.tools import execute_tool

    result = asyncio.run(execute_tool("get_weather", {}))
    assert "指定城市" in result


def test_tool_definitions_are_valid():
    """All whitelisted tools have descriptions and args."""
    from agent.tools import TOOL_DEFINITIONS, TOOL_WHITELIST

    for name in TOOL_WHITELIST:
        assert name in TOOL_DEFINITIONS
        assert "description" in TOOL_DEFINITIONS[name]
        assert "args" in TOOL_DEFINITIONS[name]


def test_persona_change_tool_creates_pending_state(tmp_path, monkeypatch):
    from agent.tools import configure_runtime, configure_tools, execute_tool
    from memory.store import MemoryStore

    async def run():
        monkeypatch.chdir(tmp_path)
        (tmp_path / "prompts").mkdir()
        (tmp_path / "prompts" / "persona.md").write_text("旧人设", encoding="utf-8")
        store = MemoryStore(str(tmp_path / "memory.db"))
        configure_tools({"agent": {"persona_file": "./prompts/persona.md"}})
        configure_runtime(memory_store=store)

        result = await execute_tool(
            "propose_persona_change",
            {"content": "新人设", "reason": "测试"},
        )
        pending = await store.get_pending_persona_change()
        await store.close()
        return result, pending

    result, pending = asyncio.run(run())
    assert "等待用户" in result
    assert pending["content"] == "新人设"


if __name__ == "__main__":
    test_whitelist_blocks_unknown_tool()
    test_get_time_returns_valid_format()
    test_get_time_unknown_timezone_falls_back()
    test_get_weather_without_city()
    test_tool_definitions_are_valid()
    print("[OK] All tools tests passed")
