"""Proactive Debug — v3.1 proactive test console backend.

Orchestrates dry-run and real-send proactive turn tests.
Does NOT construct business impulses — all impulse logic goes through
ProactiveScheduler.  This module only: assembles debug context, calls
scheduler, injects callbacks, collects decision chain, generates reports.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import StrEnum
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════
# DecisionChainStage — stable stage enum
# ══════════════════════════════════════════════════════════════════

class DecisionChainStage(StrEnum):
    """Stable decision chain stages. Tests verify key stages exist, not count."""
    BOOTSTRAP = "bootstrap"
    ELIGIBILITY = "eligibility"
    IMPULSE_GENERATION = "impulse_generation"
    IMPULSE_FILTERING = "impulse_filtering"
    IMPULSE_SELECTION = "impulse_selection"
    PROACTIVE_CONTEXT_BUILD = "proactive_context_build"
    LLM_STEP = "llm_step"
    MODEL_TOOL_CALL = "model_tool_call"
    GUARD = "guard"
    SEND = "send"
    HISTORY_WRITE = "history_write"
    TERMINAL = "terminal"


# IMPULSE_FILTERING.raw must contain these sub-keys
IMPULSE_FILTERING_SUB_KEYS = ("dedup", "expire", "score", "threshold")


# ══════════════════════════════════════════════════════════════════
# ProactiveDebugContext — input parameters
# ══════════════════════════════════════════════════════════════════

_VALID_MODES = frozenset({"dry_run", "real_send"})
_VALID_IMPULSE_TYPES = frozenset({
    "AUTO", "ECHO", "TOPIC_CONTINUE", "PRESENCE", "SCHEDULED_RITUAL",
})


@dataclass
class ProactiveDebugContext:
    """Test console input parameters.

    v2.1: mode and forced_impulse_type are validated at construction.
    """

    mode: str                          # "dry_run" | "real_send"
    user_id: str
    bot_id: str
    forced_impulse_type: str = "AUTO"  # "AUTO" | "ECHO" | ...
    debug_now: datetime | None = None
    simulate_user_silent_seconds: int | None = None
    simulate_last_proactive_seconds: int | None = None

    def __post_init__(self):
        if self.mode not in _VALID_MODES:
            raise ValueError(f"mode must be one of {sorted(_VALID_MODES)}, got {self.mode!r}")
        if self.forced_impulse_type not in _VALID_IMPULSE_TYPES:
            raise ValueError(
                f"forced_impulse_type must be one of {sorted(_VALID_IMPULSE_TYPES)}, "
                f"got {self.forced_impulse_type!r}"
            )


# ══════════════════════════════════════════════════════════════════
# ProactiveDebugReport — output structure
# ══════════════════════════════════════════════════════════════════

@dataclass
class ProactiveDebugReport:
    """Complete debug report. Serialisable to JSON for API responses."""

    run_id: str
    source: str = "admin_proactive_test"
    mode: str = ""
    user_id: str = ""
    bot_id: str = ""
    turn_id: str | None = None
    forced_impulse_type: str = ""
    actual_impulse_type: str | None = None
    terminal_state: str = ""
    send_status: str = ""

    # ── Send / history flags ──
    real_send_attempted: bool = False
    send_simulated: bool = False
    real_delivered_count: int = 0
    simulated_delivered_count: int = 0
    history_written_count: int = 0
    error_code: str = ""
    error_message: str = ""

    summary: str = ""

    # ── v2.1: Precheck vs execution separation ──
    precheck_eligibility: dict[str, Any] | None = None
    precheck_selected_impulse: dict[str, Any] | None = None
    precheck_passed: bool = False
    execution_result: dict[str, Any] | None = None
    execution_skip_reason: str = ""

    # ── v2.3 readability: generated messages + suggestions ──
    generated_messages: list[str] = field(default_factory=list)
    next_step_suggestions: list[str] = field(default_factory=list)
    guard_summary: dict[str, Any] = field(default_factory=dict)

    # ── Detail payloads (JSON-serialisable) ──
    input_overrides: dict[str, Any] = field(default_factory=dict)
    eligibility: dict[str, Any] = field(default_factory=dict)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    selected_impulse: dict[str, Any] | None = None
    proactive_context_summary: dict[str, Any] = field(default_factory=dict)
    llm_steps: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    guard: dict[str, Any] = field(default_factory=dict)
    send: dict[str, Any] = field(default_factory=dict)
    history_write: dict[str, Any] = field(default_factory=dict)
    decision_chain: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-safe dict for API responses and DB storage."""
        return {
            "run_id": self.run_id,
            "source": self.source,
            "mode": self.mode,
            "user_id": self.user_id,
            "bot_id": self.bot_id,
            "turn_id": self.turn_id,
            "forced_impulse_type": self.forced_impulse_type,
            "actual_impulse_type": self.actual_impulse_type,
            "terminal_state": self.terminal_state,
            "send_status": self.send_status,
            "real_send_attempted": self.real_send_attempted,
            "send_simulated": self.send_simulated,
            "real_delivered_count": self.real_delivered_count,
            "simulated_delivered_count": self.simulated_delivered_count,
            "history_written_count": self.history_written_count,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "summary": self.summary,
            "precheck_eligibility": self.precheck_eligibility,
            "precheck_selected_impulse": self.precheck_selected_impulse,
            "precheck_passed": self.precheck_passed,
            "execution_result": self.execution_result,
            "execution_skip_reason": self.execution_skip_reason,
            "input_overrides": self.input_overrides,
            "eligibility": self.eligibility,
            "candidates": self.candidates,
            "selected_impulse": self.selected_impulse,
            "proactive_context_summary": self.proactive_context_summary,
            "generated_messages": self.generated_messages,
            "next_step_suggestions": self.next_step_suggestions,
            "guard_summary": self.guard_summary,
            "llm_steps": self.llm_steps,
            "tool_calls": self.tool_calls,
            "guard": self.guard,
            "send": self.send,
            "history_write": self.history_write,
            "decision_chain": self.decision_chain,
        }


# ══════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════

def _tz() -> timezone:
    return timezone(timedelta(hours=8))


def _now_iso() -> str:
    return datetime.now(tz=_tz()).strftime("%Y%m%d-%H%M%S")


def _short_hex(n: int = 6) -> str:
    return uuid.uuid4().hex[:n]


# ══════════════════════════════════════════════════════════════════
# Callback factories
# ══════════════════════════════════════════════════════════════════

def create_simulated_sender() -> Callable[[list[dict[str, Any]]], Coroutine[Any, Any, list[dict[str, Any]]]]:
    """Return a mock sender that simulates delivered without calling WeChat.

    Contract: matches WeChatSender = Callable[[list[dict]], Coroutine[...]].
    Sets send_status="delivered" on each message so ChatAgent follows the
    normal success path (sent_message_count++, VisibleMessage created, etc.).
    Report marks real_send_attempted=false, send_simulated=true.
    """
    async def _sender(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for m in messages:
            m["send_status"] = "delivered"
        return messages

    return _sender


def create_dry_run_write_history(
    capture: list[dict[str, Any]] | None = None,
) -> Any:
    """Return a no-op write_model_history callback for dry-run.

    Does NOT write protocol_messages. Instead records what would have
    been written into the `capture` list (if provided).
    """
    async def _cb(turn: Any, terminal_state: Any) -> None:
        from agent.state_machine import TurnState as _TS
        delivered_texts = getattr(turn, "proactive_delivered_texts", [])
        record = {
            "would_write_assistant_proactive": bool(delivered_texts),
            "texts": list(delivered_texts),
            "count": len(delivered_texts),
            "terminal_state": str(terminal_state),
            "reason": "dry_run_never_writes_visible_history",
        }
        if capture is not None:
            capture.append(record)
        logger.info("dry_run: history_write suppressed, would_write=%d texts", len(delivered_texts))

    return _cb


def create_dry_run_post_turn_hooks(
    capture: list[dict[str, Any]] | None = None,
) -> list[Any]:
    """Return no-op post_turn_hooks for dry-run.

    Does NOT write turns, outbound_messages, turn_diagnostics,
    usage_events, or memories.
    """
    async def _hook_log(turn: Any, terminal_state: Any) -> None:
        record = {
            "turn_id": getattr(turn, "turn_id", ""),
            "terminal_state": str(terminal_state),
            "proactive_delivered_count": getattr(turn, "proactive_delivered_count", 0),
            "proactive_attempt_count": getattr(turn, "proactive_attempt_count", 0),
            "proactive_send_attempt_count": getattr(turn, "proactive_send_attempt_count", 0),
            "note": "dry_run — no production tables written",
        }
        if capture is not None:
            capture.append(record)
        logger.info("dry_run: post_turn_hooks suppressed turn_id=%s", record["turn_id"])

    return [_hook_log]


def create_dry_run_trace_handler(run_id: str, capture: list[dict[str, Any]] | None = None) -> Any:
    """Return a trace handler that writes to debug_runs.llm_steps_json instead
    of the global llm_traces table.

    v2.1: request-scoped — passed as trace_handler to LLMGateway.complete().
    Does NOT mutate global state.
    """
    async def _handler(trace: dict[str, Any]) -> None:
        step = {
            "dry_run_id": run_id,
            "model": str(trace.get("model", "")),
            "latency_ms": int(trace.get("latency_ms", 0)),
            "prompt_tokens": int(trace.get("prompt_tokens", 0)),
            "completion_tokens": int(trace.get("completion_tokens", 0)),
            "total_tokens": int(trace.get("total_tokens", 0)),
            "success": bool(trace.get("success", False)),
            "error_type": str(trace.get("error_type", "")),
            "timestamp": time.time(),
        }
        if capture is not None:
            capture.append(step)
        logger.debug("dry_run: trace captured for run_id=%s model=%s", run_id, step["model"])

    return _handler


# ══════════════════════════════════════════════════════════════════
# Decision chain collection
# ══════════════════════════════════════════════════════════════════

def _chain_entry(
    stage: DecisionChainStage,
    status: str,
    reason: str,
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {"stage": str(stage), "status": status, "reason": reason, "raw": raw or {}}


def collect_decision_chain_from_turn_result(
    turn_result: Any,  # TurnResult
    mode: str,
    eligibility_result: dict[str, Any] | None = None,
    impulse_result: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Collect decision chain entries from a completed turn.

    Call AFTER chat_agent.chat() returns.
    """
    chain: list[dict[str, Any]] = []

    # Bootstrap
    chain.append(_chain_entry(DecisionChainStage.BOOTSTRAP, "ok", f"mode={mode}", {}))

    # Eligibility (from precheck)
    if eligibility_result:
        eligible = bool(eligibility_result.get("eligible"))
        chain.append(_chain_entry(
            DecisionChainStage.ELIGIBILITY,
            "pass" if eligible else "fail",
            eligibility_result.get("final_reason", ""),
            {"checks": eligibility_result.get("checks", [])},
        ))
        if not eligible:
            chain.append(_chain_entry(DecisionChainStage.TERMINAL, "end", "eligibility_failed", {}))
            return chain

    # Impulse generation + filtering + selection
    if impulse_result:
        candidates_raw = impulse_result.get("candidates", [])
        chain.append(_chain_entry(
            DecisionChainStage.IMPULSE_GENERATION,
            "ok" if candidates_raw else "empty",
            f"generated {len(candidates_raw)} candidates",
            {"count": len(candidates_raw)},
        ))

        # Build filtering raw with required sub-keys
        filtering_raw: dict[str, Any] = {"dedup": {}, "expire": {}, "score": {}, "threshold": {}}
        chain.append(_chain_entry(
            DecisionChainStage.IMPULSE_FILTERING,
            "ok",
            f"{len(candidates_raw)} candidates to filter",
            filtering_raw,
        ))

        selected = impulse_result.get("selected")
        if selected:
            chain.append(_chain_entry(
                DecisionChainStage.IMPULSE_SELECTION,
                "ok",
                f"selected {selected.get('impulse_type', '?')}",
                {"selected_type": selected.get("impulse_type")},
            ))
        else:
            chain.append(_chain_entry(
                DecisionChainStage.IMPULSE_SELECTION,
                "empty",
                impulse_result.get("selection_reason", "no candidate passed filters"),
                {},
            ))
            chain.append(_chain_entry(DecisionChainStage.TERMINAL, "end", "no_impulse_selected", {}))
            return chain

    # Guard
    guard_events = getattr(turn_result, "fallback_events", []) or []
    guard_blocked = any("guard_block" in str(e.get("type", "")) for e in guard_events)
    chain.append(_chain_entry(
        DecisionChainStage.GUARD,
        "block" if guard_blocked else "pass",
        "guard blocked" if guard_blocked else "all guards passed",
        {},
    ))

    # Send
    chain.append(_chain_entry(
        DecisionChainStage.SEND,
        "simulated" if mode == "dry_run" else "done",
        f"real_send_attempted={mode == 'real_send'}, send_simulated={mode == 'dry_run'}",
        {
            "real_send_attempted": mode == "real_send",
            "send_simulated": mode == "dry_run",
            "delivered_count": getattr(turn_result, "proactive_delivered_count", 0),
        },
    ))

    # History write
    history_count = getattr(turn_result, "proactive_delivered_count", 0) if mode == "real_send" else 0
    chain.append(_chain_entry(
        DecisionChainStage.HISTORY_WRITE,
        "written" if history_count > 0 else "not_written",
        f"{history_count} texts written" if history_count > 0 else f"dry_run: not written",
        {"history_written_count": history_count},
    ))

    # Terminal
    chain.append(_chain_entry(
        DecisionChainStage.TERMINAL,
        "end",
        f"terminal_state={turn_result.terminal_state}",
        {"terminal_state": str(turn_result.terminal_state)},
    ))

    return chain


# ══════════════════════════════════════════════════════════════════
# Report generation
# ══════════════════════════════════════════════════════════════════

def generate_report(
    ctx: ProactiveDebugContext,
    *,
    run_id: str,
    decision_chain: list[dict[str, Any]],
    turn_result: Any | None = None,
    eligibility_result: dict[str, Any] | None = None,
    impulse_result: dict[str, Any] | None = None,
    execution_skip_reason: str = "",
    error_code: str = "",
    error_message: str = "",
    llm_steps: list[dict[str, Any]] | None = None,
) -> ProactiveDebugReport:
    """Assemble a ProactiveDebugReport from collected data.

    v2.3: fixed guard=send_json bug, added generated_messages extraction,
    guard_summary, next_step_suggestions, improved summary.
    """
    t_state = ""
    delivered = 0
    send_status = ""
    turn_id = None
    history_count = 0
    send_json: dict[str, Any] = {}
    history_write_json: dict[str, Any] = {}
    guard_json: dict[str, Any] = {}

    if turn_result is not None:
        t_state = str(getattr(turn_result, "terminal_state", ""))
        delivered = int(getattr(turn_result, "proactive_delivered_count", 0))
        turn_id = str(getattr(turn_result, "turn_id", ""))
        send_json = {
            "real_send_attempted": ctx.mode == "real_send",
            "send_step_executed": ctx.mode == "real_send",
            "dry_run_mode": ctx.mode == "dry_run",
            "delivered_count": delivered,
            "real_delivery_status": "NOT_SENT_DRY_RUN" if ctx.mode == "dry_run" else "",
        }
        history_count = delivered if ctx.mode == "real_send" else 0
        history_write_json = {
            "written": ctx.mode == "real_send" and delivered > 0,
            "count": history_count,
            "reason": "dry_run_never_writes_visible_history" if ctx.mode == "dry_run" else "",
        }

    is_real = ctx.mode == "real_send"
    is_dry = ctx.mode == "dry_run"

    # ── Extract generated messages ───────────────────────────────
    generated_messages: list[str] = []
    if turn_result is not None:
        # Primary source: proactive_delivered_texts from TurnResult
        gen = getattr(turn_result, "proactive_delivered_texts", None)
        if gen:
            generated_messages = list(gen)
        # Fallback: extract wechat_reply texts from visible_messages
        if not generated_messages:
            vm = getattr(turn_result, "visible_messages", None) or []
            for v in vm:
                if isinstance(v, dict):
                    txt = v.get("guarded_text") or v.get("original_text") or v.get("text", "")
                else:
                    txt = getattr(v, "guarded_text", "") or getattr(v, "original_text", "")
                if txt and str(txt).strip():
                    generated_messages.append(str(txt))
        # Last fallback: from tool_results
        if not generated_messages:
            tr = getattr(turn_result, "tool_results", []) or []
            for t in tr:
                if isinstance(t, dict) and t.get("name") == "wechat_reply":
                    r = t.get("result", "")
                    if r and "delivered" in str(r).lower():
                        generated_messages.append("[wechat_reply text not captured]")

    # ── Build guard summary from chain ───────────────────────────
    guard_blocked = False
    guard_ok = True
    guard_stages = [s for s in decision_chain if s.get("stage") == "guard"]
    if guard_stages:
        gs = guard_stages[0].get("status", "")
        guard_blocked = gs in ("block", "fail")
        guard_ok = not guard_blocked
    else:
        # No guard stage = guard was not executed (e.g. skip before LLM)
        guard_ok = True

    guard_json = {
        "executed": len(guard_stages) > 0,
        "passed": guard_ok,
        "blocked": guard_blocked,
        "proactive_guard": "block" if guard_blocked else "pass",
        "general_guard": "pass" if guard_ok else "not_executed",
    }

    # ── Build summary ────────────────────────────────────────────
    if execution_skip_reason:
        summary = f"已跳过：{execution_skip_reason}"
    elif is_dry:
        guard_note = "guard 拦截" if guard_blocked else "guard 通过"
        summary = f"Dry-run 成功：生成 {len(generated_messages)} 条主动消息，{guard_note}，未真实发送微信，未写入可见历史。"
    elif is_real and t_state:
        if "REPLIED" in t_state.upper():
            summary = f"Real-send 成功：真实发送 {delivered} 条，写入历史 {history_count} 条。"
        elif delivered > 0:
            summary = f"Real-send 部分成功：送达 {delivered} 条，写入历史 {history_count} 条，终端 {_ts_label(t_state)}。"
        else:
            summary = f"Real-send 未送达：终端 {_ts_label(t_state)}，送达 0 条。"
    else:
        summary = "未执行"

    # ── Build suggestions ────────────────────────────────────────
    suggestions = _build_suggestions(
        ctx, eligibility_result, impulse_result, decision_chain,
        guard_blocked, execution_skip_reason, is_dry, is_real,
        delivered, history_count, generated_messages,
    )

    return ProactiveDebugReport(
        run_id=run_id,
        source="admin_proactive_test",
        mode=ctx.mode,
        user_id=ctx.user_id,
        bot_id=ctx.bot_id,
        turn_id=turn_id,
        forced_impulse_type=ctx.forced_impulse_type,
        actual_impulse_type=(
            impulse_result.get("selected", {}).get("impulse_type")
            if impulse_result and impulse_result.get("selected") else None
        ),
        terminal_state=t_state,
        send_status=send_status,
        real_send_attempted=is_real,
        send_simulated=is_dry,
        real_delivered_count=delivered if is_real else 0,
        simulated_delivered_count=delivered if is_dry else 0,
        history_written_count=history_count,
        error_code=error_code,
        error_message=error_message,
        summary=summary,
        precheck_eligibility=eligibility_result,
        precheck_selected_impulse=(
            impulse_result.get("selected") if impulse_result else None
        ),
        precheck_passed=bool(
            eligibility_result
            and eligibility_result.get("eligible")
            and impulse_result
            and impulse_result.get("selected")
        ),
        execution_result=(
            {"terminal_state": t_state, "delivered_count": delivered, "turn_id": turn_id}
            if turn_result else None
        ),
        execution_skip_reason=execution_skip_reason,
        input_overrides={
            "debug_now": ctx.debug_now.isoformat() if ctx.debug_now else None,
            "simulate_user_silent_seconds": ctx.simulate_user_silent_seconds,
            "simulate_last_proactive_seconds": ctx.simulate_last_proactive_seconds,
        },
        eligibility=eligibility_result or {},
        candidates=impulse_result.get("candidates", []) if impulse_result else [],
        selected_impulse=impulse_result.get("selected") if impulse_result else None,
        proactive_context_summary={},
        generated_messages=generated_messages,
        next_step_suggestions=suggestions,
        guard_summary=guard_json,
        llm_steps=llm_steps or [],
        tool_calls=getattr(turn_result, "tool_results", []) if turn_result else [],
        guard=guard_json,
        send=send_json,
        history_write=history_write_json,
        decision_chain=decision_chain,
    )


def _ts_label(ts: str) -> str:
    """Human-readable terminal state label."""
    return {
        "end_replied": "已回复",
        "end_no_reply": "未回复",
        "end_failed": "失败",
        "end_fallback_sent": "兜底已发送",
        "end_cancelled": "已取消（被新消息打断）",
        "end_superseded_before_send": "发送前被取代",
        "end_interrupted_during_reply_sequence": "回复序列中被中断",
        "end_max_limit": "达到最大限制",
        "end_protocol_error": "协议错误",
        "end_aborted_by_system": "被系统中止",
        "end_skipped_proactive": "主动跳过",
        "end_failed_no_fallback": "失败（已抑制兜底）",
        "end_proactive_guard_blocked": "Guard 拦截",
        "end_proactive_max_reply_limit": "达到最大回复条数",
        "end_proactive_max_llm_steps": "达到最大 LLM 步数",
    }.get(ts.replace("TurnState.", ""), ts)


def _build_suggestions(
    ctx: ProactiveDebugContext,
    eligibility_result: dict[str, Any] | None,
    impulse_result: dict[str, Any] | None,
    decision_chain: list[dict[str, Any]],
    guard_blocked: bool,
    execution_skip_reason: str,
    is_dry: bool,
    is_real: bool,
    delivered: int,
    history_count: int,
    generated_messages: list[str],
) -> list[str]:
    """Build next-step suggestions from report data."""
    suggestions: list[str] = []

    if eligibility_result:
        checks = eligibility_result.get("checks", [])
        for c in checks:
            name = str(c.get("name", ""))
            passed = bool(c.get("passed"))
            if name == "features.proactive" and not passed:
                suggestions.append("主动消息功能未开启：请将 config.yaml 中 features.proactive 设为 true 后重启服务")
            if name == "user_silent_window" and not passed:
                suggestions.append("用户静默窗口未达标：可填写 simulate_user_silent_seconds=28800 模拟 8 小时静默")
            if name == "sleep_hours" and not passed:
                suggestions.append("当前处于睡眠时段：可设置 debug_now 为白天时间再试")
            if name == "bridge_online" and not passed:
                suggestions.append("微信 bridge 离线：请先部署/启动微信桥接")
            if name == "daily_limit" and not passed:
                suggestions.append("今日发送已达上限：可修改 daily_cap 配置或等待明天")

    if eligibility_result and eligibility_result.get("eligible"):
        sel = impulse_result.get("selected") if impulse_result else None
        cands = impulse_result.get("candidates", []) if impulse_result else []
        if not sel:
            if not cands:
                suggestions.append("没有候选被生成：试试 forced_impulse_type=PRESENCE 并填写 simulate_user_silent_seconds=3600，或在微信中发几条带情绪的消息制造 ECHO 场景")
            else:
                suggestions.append("候选被过滤：展开「工程详情 → 主动理由过滤」查看 dedup/expire/score/threshold 详情")

    if guard_blocked:
        suggestions.append("文案被 guard 拦截：展开「工程详情 → 文案安全检查 raw」查看具体拦截原因（编造观察/系统味/通知味）")

    if is_dry and generated_messages:
        suggestions.append("Dry-run 成功：检查上方生成的文案，确认像真人聊天后可执行 real-send")
    elif is_dry and not generated_messages and not execution_skip_reason:
        suggestions.append("Dry-run 未生成消息：模型可能调用了 skip_proactive，检查 terminal_state")

    if is_real:
        if delivered > 0:
            suggestions.append("Real-send 成功：请去微信确认收到消息")
            if history_count > 0:
                suggestions.append(f"历史一致性正常：{history_count} 条已写入 assistant:proactive")
            else:
                suggestions.append("注意：送达但未写入历史，检查 history_written_count")
        elif history_count == 0 and not guard_blocked:
            suggestions.append("未送达且未写入历史：可能发送失败或 bridge 离线，检查 send 详情")

    if execution_skip_reason and "scheduler_busy" in execution_skip_reason:
        suggestions.append("Scheduler 正忙：另一个主动回合正在执行，稍后重试")

    if not suggestions:
        suggestions.append("一切正常，可在微信中验证实际效果")

    return suggestions


# ══════════════════════════════════════════════════════════════════
# Core orchestration
# ══════════════════════════════════════════════════════════════════

async def run_dry_run(
    ctx: ProactiveDebugContext,
    *,
    scheduler: Any,           # ProactiveScheduler
    chat_agent: Any,          # ChatAgent
    memory_store: Any,        # MemoryStore
    history_provider: Any = None,
    system_prompt_provider: Any = None,
    interrupt_checker_provider: Any = None,
    input_latest_seq: int = 0,
) -> ProactiveDebugReport:
    """Execute a complete dry-run proactive turn.

    v2.1: acquires scheduler lock via run_debug_turn().
    Does NOT write protocol_messages, outbound_messages, turns,
    turn_diagnostics, usage_events, or memories.
    Only writes proactive_debug_runs.
    """
    run_id = f"pro-dry-{_now_iso()}-{_short_hex()}"
    chain: list[dict[str, Any]] = []
    llm_steps_capture: list[dict[str, Any]] = []
    history_capture: list[dict[str, Any]] = []
    hooks_capture: list[dict[str, Any]] = []

    chain.append(_chain_entry(DecisionChainStage.BOOTSTRAP, "ok", f"mode=dry_run run_id={run_id}", {}))

    # ── 1. Eligibility precheck ──────────────────────────────────
    eligibility_result = None
    try:
        eligibility_result = await scheduler.run_debug_eligibility(
            ctx.user_id, ctx.bot_id,
            debug_now=ctx.debug_now,
            simulate_user_silent_seconds=ctx.simulate_user_silent_seconds,
            simulate_last_proactive_seconds=ctx.simulate_last_proactive_seconds,
        )
    except Exception as e:
        logger.error("dry_run: eligibility check failed: %s", e)
        return generate_report(ctx, run_id=run_id, decision_chain=chain,
                               error_code="eligibility_error", error_message=str(e)[:500])

    if eligibility_result and not eligibility_result.get("eligible"):
        chain.append(_chain_entry(DecisionChainStage.ELIGIBILITY, "fail",
                                  eligibility_result.get("final_reason", ""),
                                  {"checks": eligibility_result.get("checks", [])}))
        chain.append(_chain_entry(DecisionChainStage.TERMINAL, "end", "eligibility_failed", {}))
        report = generate_report(ctx, run_id=run_id, decision_chain=chain,
                                 eligibility_result=eligibility_result,
                                 execution_skip_reason=eligibility_result.get("final_reason", ""))
        await _save_debug_run(memory_store, report)
        return report

    chain.append(_chain_entry(DecisionChainStage.ELIGIBILITY, "pass", "eligible", {}))

    # ── 2. Impulse pipeline ──────────────────────────────────────
    impulse_result = None
    try:
        impulse_result = await scheduler.run_debug_impulse_pipeline(
            ctx.user_id, ctx.bot_id,
            forced_impulse_type=ctx.forced_impulse_type,
            debug_now=ctx.debug_now,
        )
    except Exception as e:
        logger.error("dry_run: impulse pipeline failed: %s", e)
        return generate_report(ctx, run_id=run_id, decision_chain=chain,
                               eligibility_result=eligibility_result,
                               error_code="impulse_error", error_message=str(e)[:500])

    chain.extend(_collect_impulse_chain_entries(impulse_result))

    if not impulse_result or not impulse_result.get("selected"):
        report = generate_report(ctx, run_id=run_id, decision_chain=chain,
                                 eligibility_result=eligibility_result,
                                 impulse_result=impulse_result,
                                 execution_skip_reason="no_impulse_selected")
        await _save_debug_run(memory_store, report)
        return report

    # ── 3. Resolve providers ─────────────────────────────────────
    history = await _resolve_provider(history_provider, ctx.user_id, ctx.bot_id) if history_provider else []
    system_prompt = await _resolve_provider(system_prompt_provider, ctx.user_id) if system_prompt_provider else ""
    interrupt_checker = interrupt_checker_provider if callable(interrupt_checker_provider) else None

    # ── 4. Execute via scheduler lock ────────────────────────────
    sim_sender = create_simulated_sender()
    noop_wmh = create_dry_run_write_history(capture=history_capture)
    noop_hooks = create_dry_run_post_turn_hooks(capture=hooks_capture)
    trace_handler = create_dry_run_trace_handler(run_id, capture=llm_steps_capture)

    scheduler_result = await scheduler.run_debug_turn(
        user_id=ctx.user_id,
        bot_id=ctx.bot_id,
        impulse_meta=impulse_result["selected"],
        input_latest_seq=input_latest_seq,
        chat_agent_kwargs={
            "history": history,
            "system_prompt": system_prompt,
            "wechat_sender": sim_sender,
            "write_model_history": noop_wmh,
            "post_turn_hooks": noop_hooks,
            "trigger": "proactive",
            "interrupt_checker": interrupt_checker,
        },
    )

    if scheduler_result.get("status") == "skipped":
        report = generate_report(ctx, run_id=run_id, decision_chain=chain,
                                 eligibility_result=eligibility_result,
                                 impulse_result=impulse_result,
                                 execution_skip_reason=scheduler_result.get("reason", "scheduler_busy"))
        await _save_debug_run(memory_store, report)
        return report

    # ── 5. Collect post-turn chain ───────────────────────────────
    turn_result = scheduler_result.get("result")
    post_chain = collect_decision_chain_from_turn_result(
        turn_result, mode="dry_run",
        eligibility_result=eligibility_result,
        impulse_result=impulse_result,
    )
    chain.extend(post_chain)

    # ── 6. Generate and save report ──────────────────────────────
    report = generate_report(ctx, run_id=run_id, decision_chain=chain,
                             turn_result=turn_result,
                             eligibility_result=eligibility_result,
                             impulse_result=impulse_result,
                             llm_steps=llm_steps_capture)
    await _save_debug_run(memory_store, report)
    return report


async def run_real_send(
    ctx: ProactiveDebugContext,
    *,
    scheduler: Any,           # ProactiveScheduler
    memory_store: Any,        # MemoryStore
    history_provider: Any = None,
    system_prompt_provider: Any = None,
    wechat_sender_provider: Any = None,
    interrupt_checker_provider: Any = None,
    input_latest_seq: int = 0,
    write_model_history: Any = None,
    post_turn_hooks: list[Any] | None = None,
    protocol_event_writer: Any = None,
) -> ProactiveDebugReport:
    """Execute a complete real-send proactive turn.

    v2.1: goes through ProactiveScheduler.run_manual_trigger()
    (NOT directly calling chat_agent.chat()).
    Uses production callbacks for sender, history, and hooks.
    """
    run_id = f"pro-real-{_now_iso()}-{_short_hex()}"
    chain: list[dict[str, Any]] = []

    chain.append(_chain_entry(DecisionChainStage.BOOTSTRAP, "ok", f"mode=real_send run_id={run_id}", {}))

    # ── 1. Eligibility precheck ──────────────────────────────────
    eligibility_result = None
    try:
        eligibility_result = await scheduler.run_debug_eligibility(
            ctx.user_id, ctx.bot_id,
            debug_now=ctx.debug_now,
            simulate_user_silent_seconds=ctx.simulate_user_silent_seconds,
            simulate_last_proactive_seconds=ctx.simulate_last_proactive_seconds,
        )
    except Exception as e:
        logger.error("real_send: eligibility check failed: %s", e)
        return generate_report(ctx, run_id=run_id, decision_chain=chain,
                               error_code="eligibility_error", error_message=str(e)[:500])

    if eligibility_result and not eligibility_result.get("eligible"):
        chain.append(_chain_entry(DecisionChainStage.ELIGIBILITY, "fail",
                                  eligibility_result.get("final_reason", ""),
                                  {"checks": eligibility_result.get("checks", [])}))
        chain.append(_chain_entry(DecisionChainStage.TERMINAL, "end", "eligibility_failed", {}))
        report = generate_report(ctx, run_id=run_id, decision_chain=chain,
                                 eligibility_result=eligibility_result,
                                 execution_skip_reason=eligibility_result.get("final_reason", ""))
        await _save_debug_run(memory_store, report)
        return report

    chain.append(_chain_entry(DecisionChainStage.ELIGIBILITY, "pass", "eligible", {}))

    # ── 2. Impulse pipeline ──────────────────────────────────────
    impulse_result = None
    try:
        impulse_result = await scheduler.run_debug_impulse_pipeline(
            ctx.user_id, ctx.bot_id,
            forced_impulse_type=ctx.forced_impulse_type,
            debug_now=ctx.debug_now,
        )
    except Exception as e:
        logger.error("real_send: impulse pipeline failed: %s", e)
        return generate_report(ctx, run_id=run_id, decision_chain=chain,
                               eligibility_result=eligibility_result,
                               error_code="impulse_error", error_message=str(e)[:500])

    chain.extend(_collect_impulse_chain_entries(impulse_result))

    if not impulse_result or not impulse_result.get("selected"):
        report = generate_report(ctx, run_id=run_id, decision_chain=chain,
                                 eligibility_result=eligibility_result,
                                 impulse_result=impulse_result,
                                 execution_skip_reason="no_impulse_selected")
        await _save_debug_run(memory_store, report)
        return report

    # ── 3. Execute via scheduler (production path) ───────────────
    scheduler_result = await scheduler.run_manual_trigger(
        user_id=ctx.user_id,
        impulse_meta=impulse_result["selected"],
        bot_id=ctx.bot_id,
        history_provider=history_provider,
        system_prompt_provider=system_prompt_provider,
        wechat_sender_provider=wechat_sender_provider,
        interrupt_checker_provider=interrupt_checker_provider,
        input_latest_seq=input_latest_seq,
        write_model_history=write_model_history,
        post_turn_hooks=post_turn_hooks or [],
        protocol_event_writer=protocol_event_writer,
    )

    if scheduler_result.get("status") == "skipped":
        report = generate_report(ctx, run_id=run_id, decision_chain=chain,
                                 eligibility_result=eligibility_result,
                                 impulse_result=impulse_result,
                                 execution_skip_reason=scheduler_result.get("reason", "scheduler_busy"))
        await _save_debug_run(memory_store, report)
        return report

    # ── 4. Collect post-turn chain ───────────────────────────────
    turn_result = scheduler_result.get("result")
    post_chain = collect_decision_chain_from_turn_result(
        turn_result, mode="real_send",
        eligibility_result=eligibility_result,
        impulse_result=impulse_result,
    )
    chain.extend(post_chain)

    # ── 5. Generate and save report ──────────────────────────────
    report = generate_report(ctx, run_id=run_id, decision_chain=chain,
                             turn_result=turn_result,
                             eligibility_result=eligibility_result,
                             impulse_result=impulse_result)
    await _save_debug_run(memory_store, report)
    return report


# ══════════════════════════════════════════════════════════════════
# Internal helpers
# ══════════════════════════════════════════════════════════════════

async def _resolve_provider(provider: Any, *args: Any) -> Any:
    """Resolve a provider that may be a callable or awaitable."""
    if callable(provider):
        result = provider(*args)
        if asyncio.iscoroutine(result):
            return await result
        return result
    return provider


def _collect_impulse_chain_entries(
    impulse_result: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Build IMPULSE_GENERATION / FILTERING / SELECTION chain entries."""
    entries: list[dict[str, Any]] = []
    if not impulse_result:
        return entries

    candidates_raw = impulse_result.get("candidates", [])
    entries.append(_chain_entry(
        DecisionChainStage.IMPULSE_GENERATION,
        "ok" if candidates_raw else "empty",
        f"generated {len(candidates_raw)} candidates",
        {"count": len(candidates_raw)},
    ))

    # IMPULSE_FILTERING.raw must contain dedup/expire/score/threshold
    filtering_raw: dict[str, Any] = {
        "dedup": impulse_result.get("filtering_dedup", {}),
        "expire": impulse_result.get("filtering_expire", {}),
        "score": impulse_result.get("filtering_score", {}),
        "threshold": impulse_result.get("filtering_threshold", {}),
    }
    entries.append(_chain_entry(
        DecisionChainStage.IMPULSE_FILTERING,
        "ok",
        f"{len(candidates_raw)} → filtered",
        filtering_raw,
    ))

    selected = impulse_result.get("selected")
    if selected:
        entries.append(_chain_entry(
            DecisionChainStage.IMPULSE_SELECTION,
            "ok",
            f"selected {selected.get('impulse_type', '?')}",
            {"selected_type": selected.get("impulse_type")},
        ))
    else:
        entries.append(_chain_entry(
            DecisionChainStage.IMPULSE_SELECTION,
            "empty",
            impulse_result.get("selection_reason", "no candidate passed filters"),
            {},
        ))

    return entries


async def _save_debug_run(memory_store: Any, report: ProactiveDebugReport) -> None:
    """Persist report to proactive_debug_runs table."""
    try:
        await memory_store.insert_debug_run(report.to_dict())
        logger.info("proactive_debug: saved run_id=%s mode=%s", report.run_id, report.mode)
    except Exception as e:
        logger.error("proactive_debug: failed to save debug run: %s", e)
