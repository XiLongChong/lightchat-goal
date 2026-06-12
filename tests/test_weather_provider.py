"""Unit tests for agent.weather_provider — QWeather provider internals."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from agent.weather_provider import (
    LocationInfo,
    QWeatherProvider,
    WeatherError,
    WeatherResult,
    format_weather_error,
    format_weather_result,
    normalize_date,
)


# ---------------------------------------------------------------------------
# Helpers: fake httpx responses
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, body: dict[str, Any], status_code: int = 200):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _FakeAsyncClient:
    """httpx.AsyncClient mock that returns canned responses."""

    def __init__(self, responses: dict[str, _FakeResponse] | None = None, timeout: float = 5):
        self.timeout = timeout
        self._responses = responses or {}
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None):
        self.requests.append((url, params or {}))
        # Match by URL prefix
        for key, resp in self._responses.items():
            if key in url:
                return resp
        return _FakeResponse({"code": "404"}, 200)


def _geo_response(locations: list[dict[str, Any]]) -> _FakeResponse:
    return _FakeResponse({"code": "200", "location": locations})


def _make_location(
    name: str = "成都",
    lid: str = "101270101",
    adm1: str = "四川省",
    adm2: str = "成都",
    country: str = "中国",
    rank: int = 10,
) -> dict[str, Any]:
    return {
        "name": name, "id": lid, "adm1": adm1, "adm2": adm2,
        "country": country, "rank": str(rank),
        "lat": "30.57", "lon": "104.07", "tz": "Asia/Shanghai",
        "utcOffset": "+08:00", "isDst": "0", "type": "city", "fxLink": "",
    }


def _now_response(
    temp: str = "22",
    text: str = "多云",
    feels_like: str = "20",
    humidity: str = "65",
    wind_dir: str = "东北风",
    wind_scale: str = "2",
    precip: str = "0.0",
    obs_time: str = "2026-06-12T14:00+08:00",
) -> _FakeResponse:
    return _FakeResponse({
        "code": "200",
        "updateTime": "2026-06-12T14:05+08:00",
        "now": {
            "obsTime": obs_time,
            "temp": temp,
            "feelsLike": feels_like,
            "icon": "101",
            "text": text,
            "wind360": "45",
            "windDir": wind_dir,
            "windScale": wind_scale,
            "windSpeed": "8",
            "humidity": humidity,
            "precip": precip,
            "pressure": "1010",
            "vis": "20",
            "cloud": "30",
            "dew": "15",
        },
    })


def _daily_response(days: list[dict[str, Any]]) -> _FakeResponse:
    return _FakeResponse({
        "code": "200",
        "updateTime": "2026-06-12T20:00+08:00",
        "daily": days,
    })


def _make_daily(
    fx_date: str,
    temp_max: str = "29",
    temp_min: str = "21",
    text_day: str = "多云",
    text_night: str = "小雨",
    humidity: str = "76",
    wind_dir_day: str = "东北风",
    wind_scale_day: str = "2",
    precip: str = "2.4",
) -> dict[str, Any]:
    return {
        "fxDate": fx_date,
        "tempMax": temp_max, "tempMin": temp_min,
        "textDay": text_day, "textNight": text_night,
        "iconDay": "101", "iconNight": "305",
        "windDirDay": wind_dir_day, "windScaleDay": wind_scale_day,
        "windSpeedDay": "8",
        "windDirNight": "北风", "windScaleNight": "1", "windSpeedNight": "3",
        "humidity": humidity, "precip": precip,
        "pressure": "1005", "vis": "15", "cloud": "60", "uvIndex": "5",
        "sunrise": "06:10", "sunset": "20:00",
    }


def _make_provider(
    responses: dict[str, _FakeResponse] | None = None,
    **kwargs: Any,
) -> QWeatherProvider:
    p = QWeatherProvider(api_key="test-key", api_host="api.qweather.com", **kwargs)
    # Patch httpx.AsyncClient
    fake_client = _FakeAsyncClient(responses)
    import agent.weather_provider as wp
    wp.httpx.AsyncClient = lambda timeout=5: fake_client  # type: ignore[attr-defined]
    p._fake_client = fake_client  # type: ignore[attr-defined]
    return p


# ---------------------------------------------------------------------------
# normalize_date
# ---------------------------------------------------------------------------

class TestNormalizeDate:
    def test_none_returns_none(self):
        assert normalize_date(None) is None

    def test_empty_returns_none(self):
        assert normalize_date("") is None
        assert normalize_date("  ") is None

    def test_now_returns_none(self):
        assert normalize_date("now") is None

    def test_today(self):
        tz = ZoneInfo("Asia/Shanghai")
        expected = datetime.now(tz).strftime("%Y-%m-%d")
        assert normalize_date("today") == expected
        assert normalize_date("今天") == expected

    def test_tomorrow(self):
        tz = ZoneInfo("Asia/Shanghai")
        expected = (datetime.now(tz) + timedelta(days=1)).strftime("%Y-%m-%d")
        assert normalize_date("tomorrow") == expected
        assert normalize_date("明天") == expected

    def test_day_after_tomorrow(self):
        tz = ZoneInfo("Asia/Shanghai")
        expected = (datetime.now(tz) + timedelta(days=2)).strftime("%Y-%m-%d")
        assert normalize_date("后天") == expected

    def test_iso_date_passthrough(self):
        assert normalize_date("2026-07-01") == "2026-07-01"

    def test_invalid_iso_date(self):
        with pytest.raises(WeatherError) as exc_info:
            normalize_date("2026-13-45")
        assert exc_info.value.code == "invalid_date"

    def test_unrecognised_date(self):
        with pytest.raises(WeatherError) as exc_info:
            normalize_date("next week")
        assert exc_info.value.code == "invalid_date"


# ---------------------------------------------------------------------------
# QWeatherProvider: resolve location
# ---------------------------------------------------------------------------

class TestResolveLocation:
    def test_single_result(self):
        p = _make_provider({
            "/geo/v2/city/lookup": _geo_response([_make_location()]),
        })
        loc = asyncio.run(p._resolve_location("成都"))
        assert loc.name == "成都"
        assert loc.id == "101270101"

    def test_city_and_districts_same_adm2_not_ambiguous(self):
        """成都 + 龙泉驿/新都/温江/金堂 all share adm2=成都 → not ambiguous."""
        p = _make_provider({
            "/geo/v2/city/lookup": _geo_response([
                _make_location(name="成都", lid="101270101", adm2="成都", rank=10),
                _make_location(name="龙泉驿", lid="101270200", adm2="成都", rank=25),
                _make_location(name="新都", lid="101270300", adm2="成都", rank=28),
                _make_location(name="温江", lid="101270400", adm2="成都", rank=30),
                _make_location(name="金堂", lid="101270500", adm2="成都", rank=35),
            ]),
        })
        loc = asyncio.run(p._resolve_location("成都"))
        assert loc.name == "成都"
        assert loc.id == "101270101"

    def test_ambiguous_different_provinces(self):
        """朝阳(北京市) vs 朝阳(辽宁省) — different adm1 → genuinely ambiguous."""
        p = _make_provider({
            "/geo/v2/city/lookup": _geo_response([
                _make_location(name="朝阳", lid="1", adm1="北京市", adm2="朝阳", rank=30),
                _make_location(name="朝阳", lid="2", adm1="辽宁省", adm2="朝阳", rank=35),
            ]),
        })
        with pytest.raises(WeatherError) as exc_info:
            asyncio.run(p._resolve_location("朝阳"))
        assert exc_info.value.code == "location_ambiguous"
        assert len(exc_info.value.candidates) == 2

    def test_single_dominant_result(self):
        p = _make_provider({
            "/geo/v2/city/lookup": _geo_response([
                _make_location(name="成都", rank=8),
                _make_location(name="成都路", lid="999", adm1="上海市", rank=80),
            ]),
        })
        loc = asyncio.run(p._resolve_location("成都"))
        assert loc.id == "101270101"  # picked the dominant one

    def test_rank_3x_better(self):
        """First result rank 3x better than second → use first."""
        p = _make_provider({
            "/geo/v2/city/lookup": _geo_response([
                _make_location(name="成都", rank=10, adm2="成都"),
                _make_location(name="成都镇", lid="999", adm1="甘肃省", adm2="兰州", rank=40),
            ]),
        })
        loc = asyncio.run(p._resolve_location("成都"))
        assert loc.name == "成都"

    def test_no_results(self):
        p = _make_provider({
            "/geo/v2/city/lookup": _geo_response([]),
        })
        with pytest.raises(WeatherError) as exc_info:
            asyncio.run(p._resolve_location("不存在的城市"))
        assert exc_info.value.code == "no_data"

    def test_cached_location(self):
        p = _make_provider({
            "/geo/v2/city/lookup": _geo_response([_make_location()]),
        })
        loc1 = asyncio.run(p._resolve_location("成都"))
        # Second call should use cache (same fake client records 2 requests)
        client = p._fake_client  # type: ignore[attr-defined]
        client.requests.clear()
        loc2 = asyncio.run(p._resolve_location("成都"))
        assert loc2.id == loc1.id
        assert len(client.requests) == 0  # no new HTTP request


# ---------------------------------------------------------------------------
# QWeatherProvider: observation
# ---------------------------------------------------------------------------

class TestFetchObservation:
    def test_success(self):
        p = _make_provider({
            "/geo/v2/city/lookup": _geo_response([_make_location()]),
            "/v7/weather/now": _now_response(),
        })
        result = asyncio.run(p.get_weather("成都", None))
        assert result.kind == "observation"
        assert result.temp_c == 22.0
        assert result.feels_like_c == 20.0
        assert result.condition_day == "多云"
        assert result.humidity_percent == 65.0
        assert result.wind == "东北风 2级"
        assert result.precip_mm == 0.0
        assert "2026-06-12" in result.updated_at


# ---------------------------------------------------------------------------
# QWeatherProvider: forecast
# ---------------------------------------------------------------------------

class TestFetchForecast:
    def test_matches_by_fxdate(self):
        today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
        tomorrow = (datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=1)).strftime("%Y-%m-%d")
        day3 = (datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=2)).strftime("%Y-%m-%d")

        p = _make_provider({
            "/geo/v2/city/lookup": _geo_response([_make_location()]),
            "/v7/weather/7d": _daily_response([
                _make_daily(fx_date=today, text_day="晴"),
                _make_daily(fx_date=tomorrow, text_day="多云", text_night="小雨"),
                _make_daily(fx_date=day3, text_day="阴"),
            ]),
        })
        result = asyncio.run(p.get_weather("成都", tomorrow))
        assert result.kind == "forecast"
        assert result.date == tomorrow
        assert result.condition_day == "多云"
        assert result.condition_night == "小雨"
        assert result.temp_min_c == 21.0
        assert result.temp_max_c == 29.0

    def test_date_not_in_forecast(self):
        # 2099-01-01 triggers the 30d endpoint, provide empty response there
        p = _make_provider({
            "/geo/v2/city/lookup": _geo_response([_make_location()]),
            "/v7/weather/30d": _daily_response([]),
        })
        with pytest.raises(WeatherError) as exc_info:
            asyncio.run(p.get_weather("成都", "2099-01-01"))
        assert exc_info.value.code == "date_out_of_range"


# ---------------------------------------------------------------------------
# QWeatherProvider: error handling
# ---------------------------------------------------------------------------

class TestQWeatherErrors:
    def test_auth_error(self):
        p = _make_provider({
            "/geo/v2/city/lookup": _FakeResponse({"code": "401"}),
            "/v7/weather/now": _now_response(),
        })
        with pytest.raises(WeatherError) as exc_info:
            asyncio.run(p.get_weather("成都", None))
        assert exc_info.value.code == "auth_failed"

    def test_rate_limited(self):
        p = _make_provider({
            "/geo/v2/city/lookup": _FakeResponse({"code": "429"}),
            "/v7/weather/now": _now_response(),
        })
        with pytest.raises(WeatherError) as exc_info:
            asyncio.run(p.get_weather("成都", None))
        assert exc_info.value.code == "rate_limited"


# ---------------------------------------------------------------------------
# QWeatherProvider: forecast cache shared across dates
# ---------------------------------------------------------------------------

class TestForecastCacheShared:
    def test_second_date_uses_cache(self):
        today = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
        tomorrow = (datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=1)).strftime("%Y-%m-%d")

        p = _make_provider({
            "/geo/v2/city/lookup": _geo_response([_make_location()]),
            "/v7/weather/7d": _daily_response([
                _make_daily(fx_date=today),
                _make_daily(fx_date=tomorrow),
            ]),
        })

        # First request: fetches forecast
        r1 = asyncio.run(p.get_weather("成都", today))
        assert r1.date == today

        # Clear the fake client request log, keep the same responses
        client = p._fake_client  # type: ignore[attr-defined]
        client.requests.clear()

        # Second request: should hit forecast cache (different date, same location)
        r2 = asyncio.run(p.get_weather("成都", tomorrow))
        assert r2.date == tomorrow

        # The second request should NOT have called the forecast API again
        # It may have called GeoAPI again if location cache expired, but not forecast
        forecast_requests = [r for r in client.requests if "/v7/weather/" in r[0]]
        assert len(forecast_requests) == 0


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

class TestFormatting:
    def test_observation_format(self):
        result = WeatherResult(
            provider="qweather",
            location=LocationInfo(name="成都", id="101270101", adm1="四川省", adm2="成都", country="中国"),
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
        text = format_weather_result(result)
        assert "实时天气" in text
        assert "观测" in text
        assert "22°C" in text
        assert "东北风 2级" in text

    def test_forecast_format(self):
        result = WeatherResult(
            provider="qweather",
            location=LocationInfo(name="成都", id="101270101", adm1="四川省", adm2="成都", country="中国"),
            kind="forecast",
            date="2026-06-13",
            temp_min_c=21.0,
            temp_max_c=29.0,
            condition_day="多云",
            condition_night="小雨",
            humidity_percent=76.0,
            wind="东北风 2级",
            precip_mm=2.4,
            updated_at="2026-06-12T20:00+08:00",
        )
        text = format_weather_result(result)
        assert "预报" in text
        assert "2026-06-13" in text
        assert "多云" in text
        assert "小雨" in text
        assert "21~29°C" in text

    def test_ambiguous_error_format(self):
        error = WeatherError(
            "location_ambiguous",
            "朝阳",
            candidates=[
                LocationInfo(name="朝阳", id="1", adm1="北京市", adm2="朝阳", country="中国"),
                LocationInfo(name="朝阳", id="2", adm1="辽宁省", adm2="朝阳", country="中国"),
            ],
        )
        text = format_weather_error(error)
        assert "多个城市" in text
        assert "北京市" in text
        assert "辽宁省" in text
