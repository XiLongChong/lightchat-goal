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
    from agent.tools import execute_tool, configure_tools

    # Reset TOOL_RUNTIME_CONFIG to empty so that _default_weather_city()
    # returns "" regardless of whether main.py's module-level
    # configure_tools() ran earlier with a real config.
    configure_tools({})

    result = asyncio.run(execute_tool("get_weather", {}))
    assert "指定城市" in result


def test_get_weather_uses_default_city(monkeypatch):
    """Weather without explicit city can use configured default city."""
    from agent import tools
    from agent.tools import configure_tools, execute_tool

    class FakeResponse:
        status_code = 200
        text = "多云 +22°C 80% 6km/h"

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            assert "%E6%88%90%E9%83%BD" in url
            return FakeResponse()

    tools._WEATHER_CACHE.clear()
    monkeypatch.setattr(tools.httpx, "AsyncClient", FakeAsyncClient)
    configure_tools({
        "weather": {
            "default_city": "成都",
            "timeout_seconds": 4,
            "cache_ttl_seconds": 600,
        }
    })

    result = asyncio.run(execute_tool("get_weather", {}))

    assert result.startswith("成都天气：")
    assert "多云" in result
    configure_tools({})


def test_tool_definitions_are_valid():
    """All whitelisted tools have descriptions and args."""
    from agent.tool_registry import TOOL_REGISTRY
    from agent.tools import TOOL_WHITELIST

    for name in TOOL_WHITELIST:
        tool = TOOL_REGISTRY.get(name)
        assert tool is not None
        assert tool.description
        assert isinstance(tool.args_schema, dict)


def test_propose_persona_change_is_removed():
    """v0.2: propose_persona_change tool removed from registry.
    Calling it returns a not-available error."""
    from agent.tools import execute_tool

    result = asyncio.run(execute_tool(
        "propose_persona_change",
        {"content": "新人设", "reason": "测试"},
    ))
    assert "不在可用列表中" in result


if __name__ == "__main__":
    test_whitelist_blocks_unknown_tool()
    test_get_time_returns_valid_format()
    test_get_time_unknown_timezone_falls_back()
    test_get_weather_without_city()
    test_tool_definitions_are_valid()
    test_propose_persona_change_is_removed()
    print("[OK] All tools tests passed")
