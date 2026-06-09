import asyncio

from agent import tools
from agent.tool_registry import TOOL_REGISTRY
from agent.tools import configure_tools, execute_tool


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
    }
    for definition in TOOL_REGISTRY.iter_defs():
        assert definition.description
        assert isinstance(definition.args_schema, dict)
