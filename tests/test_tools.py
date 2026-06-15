import asyncio

from agent import tools
from agent.tool_registry import TOOL_REGISTRY
from agent.tools import configure_tools, execute_tool
from agent.weather_provider import LocationInfo, WeatherError, WeatherResult
from memory.long_term import LongTermMemoryService


# ---------------------------------------------------------------------------
# Fake provider (conforms to WeatherProvider protocol)
# ---------------------------------------------------------------------------

class FakeWeatherProvider:
    def __init__(self, result: WeatherResult | None = None, error: WeatherError | None = None):
        self.result = result
        self.error = error
        self.calls: list[tuple[str, str | None]] = []

    async def get_weather(self, location: str, date: str | None) -> WeatherResult:
        self.calls.append((location, date))
        if self.error:
            raise self.error
        if self.result is None:
            return WeatherResult(
                provider="qweather",
                location=LocationInfo(name=location, id="101010100", adm1="四川省", adm2=location, country="中国"),
                kind="observation",
                date="2026-06-12",
                temp_c=22.0,
                condition_day="多云",
                humidity_percent=65.0,
                wind="东北风 2级",
                updated_at="2026-06-12T14:00+08:00",
            )
        return self.result


def _make_observation(location: str = "成都") -> WeatherResult:
    return WeatherResult(
        provider="qweather",
        location=LocationInfo(name=location, id="101270101", adm1="四川省", adm2=location, country="中国"),
        kind="observation",
        date="2026-06-12",
        temp_c=22.0,
        feels_like_c=20.0,
        condition_day="多云",
        humidity_percent=65.0,
        wind="东北风 2级",
        precip_mm=0.0,
        updated_at="2026-06-12T14:00+08:00",
    )


def _make_forecast(location: str = "成都", date: str = "2026-06-13") -> WeatherResult:
    return WeatherResult(
        provider="qweather",
        location=LocationInfo(name=location, id="101270101", adm1="四川省", adm2=location, country="中国"),
        kind="forecast",
        date=date,
        temp_min_c=21.0,
        temp_max_c=29.0,
        condition_day="多云",
        condition_night="小雨",
        humidity_percent=76.0,
        wind="东北风 2级",
        precip_mm=2.4,
        updated_at="2026-06-12T20:00+08:00",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_unknown_tool_is_rejected():
    result = asyncio.run(execute_tool("unknown", {}))
    assert result == "Tool is unavailable: unknown"


def test_get_time_returns_timestamp():
    result = asyncio.run(execute_tool("get_time", {"timezone": "Asia/Shanghai"}))
    assert "Asia/Shanghai" in result


def test_get_weather_requires_location_without_default():
    configure_tools({})
    tools._weather_provider = None  # type: ignore[attr-defined]
    result = asyncio.run(execute_tool("get_weather", {}))
    assert result == "A location is required."


def test_get_weather_uses_default_location():
    fake = FakeWeatherProvider(result=_make_observation("成都"))
    configure_tools({"weather": {"default_city": "成都", "provider": "qweather"}})
    tools._weather_provider = fake  # type: ignore[attr-defined]

    result = asyncio.run(execute_tool("get_weather", {}))

    assert "成都" in result
    assert "观测" in result
    assert fake.calls[0][0] == "成都"
    assert fake.calls[0][1] is None  # no date → observation


def test_get_weather_observation():
    fake = FakeWeatherProvider(result=_make_observation("北京"))
    configure_tools({"weather": {"provider": "qweather"}})
    tools._weather_provider = fake  # type: ignore[attr-defined]

    result = asyncio.run(execute_tool("get_weather", {"location": "北京"}))
    assert "实时天气" in result
    assert "观测" in result
    assert "22°C" in result


def test_get_weather_forecast_tomorrow():
    fake = FakeWeatherProvider(result=_make_forecast())
    configure_tools({"weather": {"provider": "qweather"}})
    tools._weather_provider = fake  # type: ignore[attr-defined]

    result = asyncio.run(execute_tool("get_weather", {"location": "成都", "date": "明天"}))
    assert "预报" in result
    assert "多云" in result
    assert "小雨" in result
    # normalize_date should have converted "明天" to a YYYY-MM-DD string
    assert fake.calls[0][1] is not None


def test_get_weather_forecast_specific_date():
    fake = FakeWeatherProvider(result=_make_forecast(date="2026-07-01"))
    configure_tools({"weather": {"provider": "qweather"}})
    tools._weather_provider = fake  # type: ignore[attr-defined]

    result = asyncio.run(execute_tool("get_weather", {"location": "成都", "date": "2026-07-01"}))
    assert "预报" in result
    assert "2026-07-01" in result


def test_get_weather_date_out_of_range():
    fake = FakeWeatherProvider()
    fake.error = WeatherError("date_out_of_range", "超出预报范围")
    configure_tools({"weather": {"provider": "qweather"}})
    tools._weather_provider = fake  # type: ignore[attr-defined]

    result = asyncio.run(execute_tool("get_weather", {"location": "成都", "date": "2099-01-01"}))
    assert "无法查询" in result or "超出" in result


def test_get_weather_location_ambiguous():
    fake = FakeWeatherProvider()
    fake.error = WeatherError(
        "location_ambiguous",
        "朝阳",
        candidates=[
            LocationInfo(name="朝阳", id="1", adm1="北京市", adm2="朝阳", country="中国"),
            LocationInfo(name="朝阳", id="2", adm1="辽宁省", adm2="朝阳", country="中国"),
        ],
    )
    configure_tools({"weather": {"provider": "qweather"}})
    tools._weather_provider = fake  # type: ignore[attr-defined]

    result = asyncio.run(execute_tool("get_weather", {"location": "朝阳"}))
    assert "多个城市" in result
    assert "北京市" in result


def test_get_weather_provider_not_configured():
    configure_tools({"weather": {"provider": "qweather"}})
    tools._weather_provider = None  # type: ignore[attr-defined]

    result = asyncio.run(execute_tool("get_weather", {"location": "成都"}))
    assert "not configured" in result or "QWEATHER_API_KEY" in result


def test_get_weather_invalid_date():
    configure_tools({"weather": {"provider": "qweather"}})
    tools._weather_provider = FakeWeatherProvider()  # type: ignore[attr-defined]

    result = asyncio.run(execute_tool("get_weather", {"location": "成都", "date": "foobar"}))
    assert "日期格式无效" in result


def test_tool_definitions_are_complete():
    assert TOOL_REGISTRY.names() == {
        "send_text_message",
        "get_time",
        "get_weather",
        "memory",
        "web_search",
        "todo_list_lists",
        "todo_list_tasks",
        "todo_create_task",
        "todo_complete_task",
        "create_reminder",
        "list_reminders",
        "cancel_reminder",
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
