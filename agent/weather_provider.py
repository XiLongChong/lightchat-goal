"""Weather provider abstraction and QWeather implementation.

Provides:
- WeatherProvider protocol for future provider swapping
- QWeatherProvider: 和风天气 API client (GeoAPI + real-time + daily forecast)
- normalize_date(): convert natural language dates to YYYY-MM-DD
- format_weather_result/error(): structured result → compact text for LLM

v1 uses API Key auth (X-QW-Api-Key header).
JWT is the long-term preferred auth; subclass and override _build_headers().
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(slots=True, frozen=True)
class LocationInfo:
    """Minimal GeoAPI result subset for caching (EULA-compliant: no bulk data)."""
    name: str
    id: str
    adm1: str       # e.g. "四川省"
    adm2: str       # e.g. "成都"
    country: str    # e.g. "中国"


@dataclass(slots=True, frozen=True)
class WeatherResult:
    """Structured weather result returned to the tool layer."""
    provider: str                      # "qweather"
    location: LocationInfo
    kind: str                          # "observation" | "forecast"
    date: str                          # ISO date, e.g. "2026-06-12"
    temp_c: float | None = None
    feels_like_c: float | None = None
    temp_min_c: float | None = None    # forecast only
    temp_max_c: float | None = None    # forecast only
    condition_day: str = ""
    condition_night: str | None = None # forecast only
    humidity_percent: float | None = None
    wind: str = ""                     # e.g. "东北风 2级"
    precip_mm: float | None = None
    updated_at: str = ""               # QWeather updateTime / obsTime


@dataclass(slots=True)
class WeatherError(Exception):
    """Structured weather error."""
    code: str       # auth_failed, no_data, location_ambiguous, date_out_of_range,
                    # provider_error, network_error, timeout, invalid_date,
                    # quota_exceeded, rate_limited
    message: str = ""
    candidates: list[LocationInfo] | None = None


# ---------------------------------------------------------------------------
# Date normalization
# ---------------------------------------------------------------------------

_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def normalize_date(raw: str | None, tz_name: str = "Asia/Shanghai") -> str | None:
    """Convert natural language date to ``YYYY-MM-DD`` or ``None``.

    Returns ``None`` for real-time observation (no date).
    Raises :class:`WeatherError` on unrecognised input.
    """
    if not raw:
        return None

    raw = raw.strip()
    if not raw or raw.lower() in ("now",):
        return None

    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    now = datetime.now(tz)

    lower = raw.lower()
    if lower in ("today", "今天", "今天天气"):
        return now.strftime("%Y-%m-%d")
    if lower in ("tomorrow", "明天", "明天天气"):
        return (now + timedelta(days=1)).strftime("%Y-%m-%d")
    if lower in ("后天", "后天天气", "day after tomorrow"):
        return (now + timedelta(days=2)).strftime("%Y-%m-%d")
    if lower in ("大后天",):
        return (now + timedelta(days=3)).strftime("%Y-%m-%d")

    m = _ISO_DATE_RE.match(raw)
    if m:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            datetime(year, month, day)  # validate calendar date
        except ValueError:
            raise WeatherError("invalid_date", f"Invalid date: {raw}")
        return raw

    raise WeatherError("invalid_date", f"Unrecognised date: {raw}")


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def format_weather_result(result: WeatherResult) -> str:
    """Convert structured result to compact text for the LLM."""
    loc = result.location
    loc_label = f"{loc.name}（{loc.adm1}）" if loc.adm1 else loc.name

    if result.kind == "observation":
        parts = [f"{loc_label}实时天气：{result.condition_day}"]
        if result.temp_c is not None:
            parts.append(f"{result.temp_c:.0f}°C")
        if result.feels_like_c is not None:
            parts.append(f"体感 {result.feels_like_c:.0f}°C")
        if result.humidity_percent is not None:
            parts.append(f"湿度 {result.humidity_percent:.0f}%")
        if result.wind:
            parts.append(result.wind)
        if result.precip_mm is not None:
            parts.append(f"降水量 {result.precip_mm}mm")
        if result.updated_at:
            parts.append(f"观测时间 {result.updated_at}")
        parts.append("[数据来源: qweather 观测]")
        return " ".join(parts)

    # forecast
    parts = [f"{loc_label}{result.date} 预报："]
    parts.append(f"白天{result.condition_day}")
    if result.condition_night:
        parts.append(f"夜间{result.condition_night}")
    if result.temp_min_c is not None and result.temp_max_c is not None:
        parts.append(f"{result.temp_min_c:.0f}~{result.temp_max_c:.0f}°C")
    if result.humidity_percent is not None:
        parts.append(f"湿度 {result.humidity_percent:.0f}%")
    if result.wind:
        parts.append(result.wind)
    if result.precip_mm is not None:
        parts.append(f"降水量 {result.precip_mm}mm")
    if result.updated_at:
        parts.append(f"更新时间 {result.updated_at}")
    parts.append("[数据来源: qweather 预报]")
    return " ".join(parts)


def format_weather_error(error: WeatherError) -> str:
    """Convert :class:`WeatherError` to user-facing string."""
    if error.code == "location_ambiguous" and error.candidates:
        names = []
        for i, c in enumerate(error.candidates, 1):
            names.append(f"{i}. {c.name}（{c.adm1}，{c.country}）")
        return f"「{error.message}」匹配多个城市，请指定省份：{'；'.join(names)}"
    if error.code == "auth_failed":
        msg = error.message or ""
        if "INVALID_HOST" in msg or "host" in msg.lower():
            return "天气服务认证失败：API Host 不匹配。请在和风天气控制台-设置中查看你的独立 API Host，并设置环境变量 QWEATHER_API_HOST。"
        return "天气服务认证失败，请检查 API Key 和 API Host 配置。"
    if error.code == "no_data":
        return f"未找到该地区的天气数据：{error.message}"
    if error.code == "date_out_of_range":
        return f"无法查询该日期的天气预报：{error.message}"
    if error.code == "invalid_date":
        return f"日期格式无效：{error.message}"
    if error.code == "quota_exceeded":
        return "天气服务额度不足，请稍后再试。"
    if error.code == "rate_limited":
        return "天气服务请求过于频繁，请稍后再试。"
    if error.code == "timeout":
        return f"天气服务响应超时：{error.message}"
    if error.code == "network_error":
        return f"天气服务网络错误：{error.message}"
    # provider_error or unknown
    return f"天气查询失败：{error.message or error.code}"


# ---------------------------------------------------------------------------
# Provider protocol
# ---------------------------------------------------------------------------

class WeatherProvider(Protocol):
    async def get_weather(self, location: str, date: str | None) -> WeatherResult: ...


# ---------------------------------------------------------------------------
# QWeather implementation
# ---------------------------------------------------------------------------

class QWeatherProvider:
    """和风天气 API client using API Key auth (v1).

    JWT is the long-term preferred auth method.  To switch, subclass and
    override ``_build_headers()``.
    """

    def __init__(
        self,
        *,
        api_key: str = "",
        api_host: str = "",
        timeout_seconds: float = 5.0,
        observation_cache_ttl: float = 600.0,
        forecast_cache_ttl: float = 1800.0,
        location_cache_ttl: float = 86400.0,
        lang: str = "zh",
    ) -> None:
        self._api_key = api_key
        self._api_host = api_host.rstrip("/") if api_host else "api.qweather.com"
        self._timeout = timeout_seconds
        self._observation_cache_ttl = observation_cache_ttl
        self._forecast_cache_ttl = forecast_cache_ttl
        self._location_cache_ttl = location_cache_ttl
        self._lang = lang

        # Three independent caches: key → (timestamp, value)
        self._location_cache: dict[str, tuple[float, LocationInfo]] = {}
        self._observation_cache: dict[str, tuple[float, WeatherResult]] = {}
        self._forecast_cache: dict[str, tuple[float, list[WeatherResult]]] = {}

    # -- auth ----------------------------------------------------------------

    def _build_headers(self) -> dict[str, str]:
        """Return HTTP headers including authentication.

        Override in a subclass for JWT auth.
        """
        return {"X-QW-Api-Key": self._api_key}

    # -- public API ----------------------------------------------------------

    async def get_weather(self, location: str, date: str | None) -> WeatherResult:
        loc = await self._resolve_location(location)
        if date is None:
            return await self._fetch_observation(loc)
        return await self._fetch_forecast(loc, date)

    # -- GeoAPI --------------------------------------------------------------

    async def _resolve_location(self, location_text: str) -> LocationInfo:
        cache_key = location_text.casefold()
        now = time.time()
        cached = self._location_cache.get(cache_key)
        if cached and now - cached[0] < self._location_cache_ttl:
            return cached[1]

        url = f"https://{self._api_host}/geo/v2/city/lookup"
        params = {
            "location": location_text,
            "lang": self._lang,
            "number": 10,
        }
        data = await self._request(url, params)

        locations = data.get("location") or []
        if not locations:
            raise WeatherError("no_data", f"未找到城市「{location_text}」")

        if len(locations) == 1:
            loc = self._parse_location(locations[0])
            self._location_cache[cache_key] = (now, loc)
            return loc

        # Disambiguation heuristics
        first = locations[0]
        first_loc = self._parse_location(first)
        first_rank = _safe_int(first.get("rank", 999))
        second_rank = _safe_int(locations[1].get("rank", 999))

        # Heuristic 1: first result rank is clearly dominant
        if first_rank <= 10 and second_rank > 50:
            self._location_cache[cache_key] = (now, first_loc)
            return first_loc

        # Heuristic 2: first result rank is at least 3x better
        if first_rank <= 15 and second_rank > first_rank * 3:
            self._location_cache[cache_key] = (now, first_loc)
            return first_loc

        # Heuristic 3: all results share the same adm1 (province) AND adm2,
        # meaning they are a city + its subordinate districts — not ambiguous.
        # e.g. "成都" → 成都, 龙泉驿, 新都, 温江, 金堂
        # (all adm1=四川省, adm2=成都)
        first_adm1 = first.get("adm1", "")
        first_adm2 = first.get("adm2", "")
        if first_adm1 and first_adm2 and all(
            loc.get("adm1") == first_adm1 and loc.get("adm2") == first_adm2
            for loc in locations
        ):
            self._location_cache[cache_key] = (now, first_loc)
            return first_loc

        # Genuinely ambiguous (e.g. "朝阳" → 北京朝阳 vs 辽宁朝阳,
        # different adm1 provinces)
        candidates = [self._parse_location(l) for l in locations[:5]]
        raise WeatherError(
            "location_ambiguous",
            location_text,
            candidates=candidates,
        )

    # -- Observation ---------------------------------------------------------

    async def _fetch_observation(self, loc: LocationInfo) -> WeatherResult:
        now = time.time()
        cached = self._observation_cache.get(loc.id)
        if cached and now - cached[0] < self._observation_cache_ttl:
            return cached[1]

        url = f"https://{self._api_host}/v7/weather/now"
        params = {"location": loc.id, "lang": self._lang, "unit": "m"}
        data = await self._request(url, params)

        obs = data.get("now") or {}
        result = WeatherResult(
            provider="qweather",
            location=loc,
            kind="observation",
            date=_extract_date_from_time(data.get("updateTime", "")),
            temp_c=_safe_float(obs.get("temp")),
            feels_like_c=_safe_float(obs.get("feelsLike")),
            condition_day=str(obs.get("text", "")),
            humidity_percent=_safe_float(obs.get("humidity")),
            wind=_format_wind(obs.get("windDir", ""), obs.get("windScale", "")),
            precip_mm=_safe_float(obs.get("precip")),
            updated_at=obs.get("obsTime") or data.get("updateTime", ""),
        )
        self._observation_cache[loc.id] = (now, result)
        return result

    # -- Forecast ------------------------------------------------------------

    async def _fetch_forecast(self, loc: LocationInfo, target_date: str) -> WeatherResult:
        now = time.time()

        # Check forecast cache first
        cached = self._forecast_cache.get(loc.id)
        if cached and now - cached[0] < self._forecast_cache_ttl:
            for r in cached[1]:
                if r.date == target_date:
                    return r

        # Determine forecast range
        days = _forecast_days_for(target_date)
        url = f"https://{self._api_host}/v7/weather/{days}"
        params = {"location": loc.id, "lang": self._lang, "unit": "m"}
        data = await self._request(url, params)

        daily = data.get("daily") or []
        update_time = data.get("updateTime", "")

        # Parse all days and cache
        results: list[WeatherResult] = []
        matched: WeatherResult | None = None
        for d in daily:
            fx_date = str(d.get("fxDate", ""))
            r = WeatherResult(
                provider="qweather",
                location=loc,
                kind="forecast",
                date=fx_date,
                temp_min_c=_safe_float(d.get("tempMin")),
                temp_max_c=_safe_float(d.get("tempMax")),
                condition_day=str(d.get("textDay", "")),
                condition_night=str(d.get("textNight", "")),
                humidity_percent=_safe_float(d.get("humidity")),
                wind=_format_wind(d.get("windDirDay", ""), d.get("windScaleDay", "")),
                precip_mm=_safe_float(d.get("precip")),
                updated_at=update_time,
            )
            results.append(r)
            if fx_date == target_date:
                matched = r

        if results:
            self._forecast_cache[loc.id] = (now, results)

        if matched is None:
            raise WeatherError(
                "date_out_of_range",
                f"无法获取 {target_date} 的天气预报（可能超出预报范围）",
            )
        return matched

    # -- HTTP helper ---------------------------------------------------------

    async def _request(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        """Make a GET request and return parsed JSON body."""
        headers = self._build_headers()
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(url, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise WeatherError("timeout", str(exc)) from exc
        except httpx.ConnectError as exc:
            raise WeatherError("network_error", str(exc)) from exc

        # Try to parse JSON body even on non-200 — QWeather v2 returns
        # structured errors in the body (e.g. 403 INVALID HOST).
        try:
            body = resp.json()
        except Exception:
            if resp.status_code != 200:
                raise WeatherError("provider_error", f"HTTP {resp.status_code}") from None
            raise WeatherError("provider_error", "Invalid JSON response") from None

        if resp.status_code != 200:
            # QWeather v2 error: body has error.type / error.title
            v2_type = str((body.get("error") or {}).get("type", ""))
            v2_detail = str((body.get("error") or {}).get("detail", ""))
            v2_code_map: dict[str, str] = {
                "INVALID_PARAMETER": "invalid_parameter",
                "MISSING_PARAMETER": "invalid_parameter",
                "NO_SUCH_LOCATION": "no_data",
                "DATA_NOT_AVAILABLE": "no_data",
                "UNAUTHORIZED": "auth_failed",
                "NO_CREDIT": "quota_exceeded",
                "OVERDUE": "quota_exceeded",
                "SECURITY_RESTRICTION": "provider_error",
                "INVALID_HOST": "auth_failed",
                "ACCOUNT_SUSPENSION": "provider_error",
                "FORBIDDEN": "auth_failed",
                "TOO_MANY_REQUESTS": "rate_limited",
                "OVER_MONTHLY_LIMIT": "rate_limited",
            }
            error_code = v2_code_map.get(v2_type, "provider_error")
            msg = v2_detail or v2_type or f"HTTP {resp.status_code}"
            raise WeatherError(error_code, f"QWeather: {msg}")

        code = str(body.get("code", ""))
        if code != "200":
            error_map: dict[str, str] = {
                "204": "no_data",
                "400": "invalid_parameter",
                "401": "auth_failed",
                "402": "quota_exceeded",
                "403": "auth_failed",
                "404": "no_data",
                "429": "rate_limited",
            }
            error_code = error_map.get(code, "provider_error")
            raise WeatherError(error_code, f"QWeather code {code}")

        return body

    # -- Parse helpers -------------------------------------------------------

    @staticmethod
    def _parse_location(obj: dict[str, Any]) -> LocationInfo:
        return LocationInfo(
            name=str(obj.get("name", "")),
            id=str(obj.get("id", "")),
            adm1=str(obj.get("adm1", "")),
            adm2=str(obj.get("adm2", "")),
            country=str(obj.get("country", "")),
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _safe_float(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_int(val: Any) -> int:
    if val is None or val == "":
        return 999
    try:
        return int(val)
    except (ValueError, TypeError):
        return 999


def _format_wind(direction: Any, scale: Any) -> str:
    d = str(direction or "").strip()
    s = str(scale or "").strip()
    if not d:
        return ""
    if s:
        return f"{d} {s}级"
    return d


def _extract_date_from_time(time_str: str) -> str:
    """Extract YYYY-MM-DD from an ISO time string like '2026-06-12T14:00+08:00'."""
    if not time_str:
        return ""
    return time_str[:10]


def _forecast_days_for(target_date: str) -> str:
    """Choose the shortest forecast range that covers *target_date*."""
    try:
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        target = datetime.strptime(target_date, "%Y-%m-%d").date()
        delta = (target - today).days
    except Exception:
        delta = 7
    if delta <= 7:
        return "7d"
    if delta <= 15:
        return "15d"
    return "30d"
