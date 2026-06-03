"""Temporal Context Layer — 让模型感知对话中的时间流逝。

替代简单的"当前时间：xxx"，在不破坏主 system prompt 缓存的前提下，
提供时间感知能力：上次互动时间、时间差、resume_type。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

# ── Time gap thresholds (seconds) ───────────────────────────────
_CONTINUOUS_THRESHOLD = 10 * 60       # 10 分钟
_SHORT_THRESHOLD = 45 * 60            # 45 分钟（≥45 分钟升级为 same_day_resume）
_DAY_THRESHOLD = 7 * 24 * 60 * 60     # 7 天

# ── Time divider thresholds (seconds) ───────────────────────────
_DIVIDER_MIN_THRESHOLD = 10 * 60      # 10 分钟，低于此不插分割线

# ── Resume types ────────────────────────────────────────────────
CONTINUOUS_CHAT = "continuous_chat"
SHORT_RESUME = "short_resume"
SAME_DAY_RESUME = "same_day_resume"
NEXT_DAY_RESUME = "next_day_resume"
LONG_GAP_RESUME = "long_gap_resume"


@dataclass
class TemporalContextResult:
    """TemporalContextBuilder 输出。"""
    temporal_message: str                     # system role message，替代原 "当前时间：xxx"
    augmented_history: list[dict[str, Any]]   # history + 时间分割线
    diagnostics: dict[str, Any] = field(default_factory=dict)


class TemporalContextBuilder:
    """构建时间感知上下文。

    职责：
    1. 生成 temporal system message（当前时间 + 上次互动信息 + resume_type）
    2. 在 history 中相邻可见消息的 gap 超阈值时插入 system role 时间分割线

    不做：
    - 不修改 MemoryStore
    - 不修改主 system prompt
    - 不进入 WRITE_MODEL_HISTORY
    - 不伪装成 user / assistant
    """

    def __init__(self, timezone_offset_hours: int = 8) -> None:
        """timezone_offset_hours: 默认 Asia/Shanghai = UTC+8"""
        self._tz = timezone(timedelta(hours=timezone_offset_hours))

    def build(
        self,
        now: datetime,
        history: list[dict[str, Any]],
        current_user_ts: float,
    ) -> TemporalContextResult:
        """构建 temporal context。

        Args:
            now: 当前时间 (datetime，带时区信息或 naive 视为本地时间)
            history: 历史消息列表，每条含 role/content/ts/source
            current_user_ts: 当前用户消息的 Unix 时间戳

        Returns:
            TemporalContextResult
        """
        # ── 提取上次互动时间 ─────────────────────────────────────
        last_user_ts = self._find_last_user_ts(history)
        last_assistant_ts = self._find_last_assistant_ts(history)

        has_previous = last_user_ts > 0 or last_assistant_ts > 0
        last_visible_ts = max(last_user_ts, last_assistant_ts, 0.0)

        # ── 计算 gap ────────────────────────────────────────────
        gap_seconds = 0.0
        if has_previous:
            gap_seconds = current_user_ts - last_visible_ts

        # ── 判定 resume_type ────────────────────────────────────
        resume_type = self._classify_resume_type(
            gap_seconds, last_visible_ts, current_user_ts, now,
        )

        # ── 构建 temporal system message ────────────────────────
        temporal_message = self._build_temporal_message(
            now=now,
            last_user_ts=last_user_ts,
            last_assistant_ts=last_assistant_ts,
            gap_seconds=gap_seconds,
            resume_type=resume_type,
            has_previous=has_previous,
        )

        # ── 构建带时间分割线的 history ──────────────────────────
        augmented = self._augment_history(history, current_user_ts=current_user_ts)

        # ── diagnostics ─────────────────────────────────────────
        diag: dict[str, Any] = {
            "has_previous_interaction": has_previous,
            "resume_type": resume_type,
            "last_user_message_at": last_user_ts,
            "last_assistant_visible_message_at": last_assistant_ts,
            "last_visible_interaction_at": last_visible_ts,
            "gap_seconds": round(gap_seconds, 1),
            "divider_count": sum(
                1 for m in augmented if m.get("role") == "system" and m.get("_temporal_divider")
            ),
        }

        return TemporalContextResult(
            temporal_message=temporal_message,
            augmented_history=augmented,
            diagnostics=diag,
        )

    # ── internal ────────────────────────────────────────────────

    def _find_last_user_ts(self, history: list[dict[str, Any]]) -> float:
        for item in reversed(history):
            if item.get("role") == "user" and item.get("ts", 0) > 0:
                return float(item["ts"])
        return 0.0

    def _find_last_assistant_ts(self, history: list[dict[str, Any]]) -> float:
        for item in reversed(history):
            if item.get("role") == "assistant" and item.get("ts", 0) > 0:
                content = str(item.get("content", ""))
                if content.strip():
                    return float(item["ts"])
        return 0.0

    def _classify_resume_type(
        self,
        gap_seconds: float,
        last_visible_ts: float,
        current_user_ts: float,
        now: datetime,
    ) -> str:
        if gap_seconds < _CONTINUOUS_THRESHOLD:
            return CONTINUOUS_CHAT

        if gap_seconds < _SHORT_THRESHOLD:
            return SHORT_RESUME

        # 跨天判定：基于日历日而非纯秒数
        last_dt = datetime.fromtimestamp(last_visible_ts, tz=self._tz)
        current_dt = datetime.fromtimestamp(current_user_ts, tz=self._tz)

        if last_dt.date() != current_dt.date():
            if gap_seconds <= _DAY_THRESHOLD:
                return NEXT_DAY_RESUME
            return LONG_GAP_RESUME

        # 同天但 ≥ 1 小时
        return SAME_DAY_RESUME

    def _build_temporal_message(
        self,
        now: datetime,
        last_user_ts: float,
        last_assistant_ts: float,
        gap_seconds: float,
        resume_type: str,
        has_previous: bool,
    ) -> str:
        lines: list[str] = []

        # 当前时间
        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        wd = weekday_names[now.weekday()]
        lines.append(
            f"当前时间：{now.year}年{now.month:02d}月{now.day:02d}日 "
            f"{now.hour:02d}:{now.minute:02d} {wd}"
        )

        if not has_previous:
            lines.append("这是首次对话，无历史互动记录。")
            lines.append(f"resume_type: {resume_type}")
            return "\n".join(lines)

        # 上次用户消息时间
        if last_user_ts > 0:
            dt = datetime.fromtimestamp(last_user_ts, tz=self._tz)
            lines.append(
                f"上次用户消息：{dt.month:02d}月{dt.day:02d}日 "
                f"{dt.hour:02d}:{dt.minute:02d}"
            )

        # 上次 assistant 可见回复时间
        if last_assistant_ts > 0:
            dt = datetime.fromtimestamp(last_assistant_ts, tz=self._tz)
            lines.append(
                f"上次你的回复：{dt.month:02d}月{dt.day:02d}日 "
                f"{dt.hour:02d}:{dt.minute:02d}"
            )

        # 距上次可见互动的时间差
        lines.append(f"距离上次互动：{_format_time_gap(gap_seconds)}")

        # resume_type + 使用规则
        lines.append(f"resume_type: {resume_type}")
        lines.append(
            "使用规则：时间差只是事实，只有和用户当前消息有关时才自然提及，"
            "不要刻意表演'我一直在等你'。"
        )

        return "\n".join(lines)

    def _augment_history(
        self,
        history: list[dict[str, Any]],
        current_user_ts: float = 0.0,
    ) -> list[dict[str, Any]]:
        """在相邻可见消息 gap 超阈值时插入 system role 时间分割线。

        只在渲染阶段生成，不修改原始 history。
        如果提供 current_user_ts，还会在最后一条历史消息和当前用户消息之间检查 gap。
        """
        result: list[dict[str, Any]] = []
        last_visible_ts: float = 0.0

        for item in history:
            role = item.get("role", "")
            ts = float(item.get("ts", 0))
            content = str(item.get("content", ""))

            # 检查是否是可见消息（user 或有内容的 assistant）
            is_visible = (
                role == "user"
                or (role == "assistant" and content.strip())
            )

            if is_visible and ts > 0:
                if last_visible_ts > 0:
                    gap = ts - last_visible_ts
                    if gap >= _DIVIDER_MIN_THRESHOLD:
                        divider = self._make_time_divider(last_visible_ts, ts, gap)
                        result.append(divider)
                last_visible_ts = ts

            result.append(item)

        # 在最后一条历史消息和当前用户消息之间检查 gap
        if current_user_ts > 0 and last_visible_ts > 0:
            gap = current_user_ts - last_visible_ts
            if gap >= _DIVIDER_MIN_THRESHOLD:
                divider = self._make_time_divider(last_visible_ts, current_user_ts, gap)
                result.append(divider)

        return result

    def _make_time_divider(
        self,
        from_ts: float,
        to_ts: float,
        gap: float,
    ) -> dict[str, Any]:
        """创建 system role 时间分割线。"""
        from_dt = datetime.fromtimestamp(from_ts, tz=self._tz)
        to_dt = datetime.fromtimestamp(to_ts, tz=self._tz)

        text = self._format_divider_text(from_dt, to_dt, gap)

        return {
            "role": "system",
            "content": text,
            "_temporal_divider": True,  # 标记为时间分割线，方便测试和过滤
        }

    def _format_divider_text(
        self,
        from_dt: datetime,
        to_dt: datetime,
        gap: float,
    ) -> str:
        if gap < 3600:
            minutes = max(1, int(gap // 60))
            return f"[距离上次发言约 {minutes} 分钟]"

        if from_dt.date() == to_dt.date():
            # 同天
            hours = int(gap // 3600)
            minutes = int((gap % 3600) // 60)
            period = _time_period(to_dt)
            if minutes > 0:
                return f"[距离上次发言约 {hours} 小时 {minutes} 分钟，现在是{period}]"
            return f"[距离上次发言约 {hours} 小时，现在是{period}]"

        # 跨天
        day_diff = (to_dt.date() - from_dt.date()).days
        if day_diff <= 7:
            return f"[距离上次对话已过去 {day_diff} 天]"
        return "[距离上次对话已经过去较久]"


# ── module-level helpers ─────────────────────────────────────────

def _format_time_gap(gap_seconds: float) -> str:
    """格式化时间差为中文自然语言。"""
    if gap_seconds < 60:
        return "不到 1 分钟"
    if gap_seconds < 3600:
        minutes = int(gap_seconds // 60)
        return f"约 {minutes} 分钟"
    if gap_seconds < 86400:
        hours = int(gap_seconds // 3600)
        minutes = int((gap_seconds % 3600) // 60)
        if minutes > 0:
            return f"约 {hours} 小时 {minutes} 分钟"
        return f"约 {hours} 小时"
    days = int(gap_seconds // 86400)
    return f"{days} 天"


def _time_period(dt: datetime) -> str:
    """返回时段：上午/下午/晚上/凌晨。"""
    hour = dt.hour
    if 6 <= hour < 12:
        return "上午"
    if 12 <= hour < 18:
        return "下午"
    if 18 <= hour < 22:
        return "晚上"
    return "凌晨"
