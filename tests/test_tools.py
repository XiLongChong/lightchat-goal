import asyncio

from agent import tools
from agent.tool_registry import TOOL_REGISTRY
from agent.tools import configure_tools, execute_tool
from memory.long_term import LongTermMemoryService


def test_unknown_tool_is_rejected():
    result = asyncio.run(execute_tool("unknown", {}))
    assert result == "Tool is unavailable: unknown"


def test_get_time_returns_timestamp():
    result = asyncio.run(execute_tool("get_time", {"timezone": "Asia/Shanghai"}))
    assert "Asia/Shanghai" in result


def test_get_weather_requires_location_without_default():
    configure_tools({})
    result = asyncio.run(execute_tool("get_weather", {}))
    assert result == "A location is required."


def test_get_weather_uses_default_location(monkeypatch):
    class FakeResponse:
        status_code = 200
        text = "多云 +22C"

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
    configure_tools({"weather": {"default_city": "成都"}})

    result = asyncio.run(execute_tool("get_weather", {}))

    assert result == "成都: 多云 +22C"


def test_tool_definitions_are_complete():
    assert TOOL_REGISTRY.names() == {
        "send_text_message",
        "get_time",
        "get_weather",
        "memory",
    }
    for definition in TOOL_REGISTRY.iter_defs():
        assert definition.description
        assert isinstance(definition.args_schema, dict)


def test_memory_tool_uses_real_user_id_and_has_no_delete(tmp_path):
    tools.configure_runtime(long_term_memory=LongTermMemoryService(tmp_path))
    created = asyncio.run(execute_tool(
        "memory",
        {
            "command": "create",
            "path": "user-background.md",
            "file_text": "# 用户背景\n\n更新时间：2026-06-11\n\n住在成都。\n",
        },
        user_id="real-user",
    ))
    assert '"ok": true' in created
    viewed = asyncio.run(execute_tool(
        "memory",
        {"command": "view", "path": "user-background.md"},
        user_id="real-user",
    ))
    assert "成都" in viewed
    rejected = asyncio.run(execute_tool(
        "memory",
        {"command": "delete", "path": "user-background.md"},
        user_id="real-user",
    ))
    assert "memory_invalid_command" in rejected
