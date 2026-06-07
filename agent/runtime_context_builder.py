"""ContextBuilder — single entry point for all model input construction.

Phase 3: Facade that assembles provider_messages in fixed 16-block order.
Wraps existing context construction; does not rewrite model behavior.

Spec: docs/runtime-specs/01-context-builder-spec.zh.md
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from agent.temporal_context import (
    TemporalContextBuilder,
    TemporalContextResult,
    _format_time_gap,
    CONTINUOUS_CHAT,
    SHORT_RESUME,
    SAME_DAY_RESUME,
    NEXT_DAY_RESUME,
    LONG_GAP_RESUME,
)
from runtime_contracts import (
    BuildContextError,
    BuildContextRequest,
    BuiltContext,
    CacheClass,
    ContextBlock,
    ContextBlockKind,
    ContextBuildWarning,
    ContextSnapshot,
    ConversationHistoryMessage,
    DeliveryStatus,
    MessageKind,
    ProviderMessage,
    ResumeType,
    SystemTime,
    SystemTimeBlock,
    TerminalPolicy,
    TokenBudgetReport,
    TurnPhase,
    TurnType,
)

logger = logging.getLogger(__name__)

# Thresholds from temporal_context.py
_CONTINUOUS_THRESHOLD = 10 * 60
_SHORT_THRESHOLD = 45 * 60
_DAY_THRESHOLD = 7 * 24 * 60 * 60


class ContextBuilder:
    """All model input MUST pass through ContextBuilder.build().

    Call once per model call. Never cache or reuse BuiltContext.
    """

    def __init__(self, persona_text: str = "", protocol_dir: str = "prompts") -> None:
        self._persona_fallback = persona_text
        self._protocol_dir = protocol_dir
        self._temporal = TemporalContextBuilder()

    # ── Public API ─────────────────────────────────────────────────

    def build(self, request: BuildContextRequest) -> BuiltContext:
        self._validate_request(request)
        blocks: list[ContextBlock] = []
        messages: list[ProviderMessage] = []
        warnings: list[ContextBuildWarning] = []

        # ── Phase 1: Fixed blocks (cacheable prefix) ───────────────
        # 1. fixed_system_protocol
        msg, blk = self._make_fixed_system_block(request, request.fixed_system_protocol, "system")
        messages.append(msg); blocks.append(blk)

        # 2. fixed_output_protocol
        msg, blk = self._make_fixed_system_block(request, request.fixed_output_protocol, "output")
        messages.append(msg); blocks.append(blk)

        # 3. fixed_tool_protocol
        msg, blk = self._make_fixed_system_block(request, request.fixed_tool_protocol, "tool")
        messages.append(msg); blocks.append(blk)

        # 4. persona
        msg, blk = self._make_persona_block(request)
        messages.append(msg); blocks.append(blk)

        # 5. relationship_state (optional)
        if request.relationship_state is not None:
            msg, blk = self._make_relationship_state_block(request)
            messages.append(msg); blocks.append(blk)
        else:
            warnings.append(ContextBuildWarning(
                source="relationship_state_missing",
                detail="relationship_state is None",
                severity="warning",
                block_affected="relationship_state",
            ))

        # 6. system_time_block (before memory/history)
        if request.system_time is not None:
            msg, blk = self._make_system_time_block(request, warnings)
            messages.append(msg); blocks.append(blk)

        # ── Phase 2: Memory blocks ────────────────────────────────
        mem_msgs, mem_blks = self._make_all_memory_blocks(request)
        for m, b in zip(mem_msgs, mem_blks):
            messages.append(m); blocks.append(b)

        # ── Phase 3: Dynamic blocks ───────────────────────────────
        # 11. recent_conversation_history (rendered as individual user/assistant msgs)
        hist_msgs, hist_blk = self._make_recent_history_block(request)
        if hist_msgs:
            messages.extend(hist_msgs)
            if hist_blk:
                blocks.append(hist_blk)
        elif request.recent_history is not None:
            warnings.append(ContextBuildWarning(
                source="empty_history",
                detail="no recent conversation history available",
                severity="info",
                block_affected="recent_conversation_history",
            ))

        # 12. current_turn_local_state
        turn_local = self._make_turn_local_block(request)
        if turn_local is not None:
            msg, blk = turn_local
            messages.append(msg); blocks.append(blk)

        # 13. media_context (optional)
        if request.media_context:
            msg, blk = self._make_media_context_block(request)
            messages.append(msg); blocks.append(blk)

        # 14. current_input
        current_input = self._make_current_input_block(request)
        if current_input is not None:
            msg, blk = current_input
            messages.append(msg); blocks.append(blk)

        # ── Phase 4: Tool contract and terminal policy ────────────
        # 15. exposed_tools_contract
        if request.tool_exposure is not None:
            msg, blk = self._make_tool_contract_block(request)
            messages.append(msg); blocks.append(blk)

        # 16. terminal_policy
        if request.terminal_policy is not None:
            msg, blk = self._make_terminal_policy_block(request)
            messages.append(msg); blocks.append(blk)

        # ── Snapshot ─────────────────────────────────────────────
        snapshot = self._build_snapshot(request, blocks)

        # ── Validation ───────────────────────────────────────────
        self._verify_tool_results_not_user_role(messages, warnings)
        self._verify_media_not_user_text(blocks, warnings)

        # ── Token budget (simple estimate) ────────────────────────
        provider_system, provider_messages = self._split_provider_payload(messages)
        budget_report = self._estimate_token_budget(messages, request.token_budget)

        return BuiltContext(
            turn_id=request.turn_id,
            call_id=request.call_id,
            provider_messages=provider_messages,
            provider_system=provider_system,
            exposed_tool_schemas=(
                request.tool_exposure.native_tool_schemas
                if request.tool_exposure else []
            ),
            context_blocks=blocks,
            context_snapshot=snapshot,
            token_budget_report=budget_report,
            terminal_policy=request.terminal_policy,
            warnings=warnings,
        )

    # ── Validation ─────────────────────────────────────────────────

    def _split_provider_payload(
        self, messages: list[ProviderMessage],
    ) -> tuple[list[dict[str, Any]], list[ProviderMessage]]:
        provider_system: list[dict[str, Any]] = []
        provider_messages: list[ProviderMessage] = []
        for msg in messages:
            if msg.role == "system":
                text = _provider_content_to_text(msg.content)
                if text.strip():
                    provider_system.append({"type": "text", "text": text})
                continue
            if msg.role in ("user", "assistant"):
                provider_messages.append(msg)
        return provider_system, provider_messages

    def _validate_request(self, request: BuildContextRequest) -> None:
        missing: list[str] = []
        for field in ("turn_id", "call_id", "turn_type", "user_id", "bot_id"):
            if not getattr(request, field, None):
                missing.append(field)
        if missing:
            raise BuildContextError(
                reason=f"missing required fields: {missing}",
                missing_fields=missing,
            )

    # ── Fixed blocks ───────────────────────────────────────────────

    def _make_fixed_system_block(
        self, request: BuildContextRequest, content: str, label: str,
    ) -> tuple[ProviderMessage, ContextBlock]:
        kind = ContextBlockKind.FIXED_PROTOCOL
        return (
            ProviderMessage(role="system", content=content),
            ContextBlock(
                block_id=f"fixed_{label}_protocol",
                kind=kind, cache_class=CacheClass.FIXED,
                content_summary=f"{label} protocol ({_est_tokens(content)} tokens)",
                source="ContextBuilder",
            ),
        )

    def _make_persona_block(
        self, request: BuildContextRequest,
    ) -> tuple[ProviderMessage, ContextBlock]:
        content = request.persona or self._persona_fallback
        return (
            ProviderMessage(role="system", content=content),
            ContextBlock(
                block_id="persona",
                kind=ContextBlockKind.PERSONA,
                cache_class=CacheClass.LOW_FREQUENCY,
                content_summary=f"persona ({_est_tokens(content)} tokens)",
                source="ContextBuilder",
            ),
        )

    def _make_relationship_state_block(
        self, request: BuildContextRequest,
    ) -> tuple[ProviderMessage, ContextBlock]:
        rs = request.relationship_state
        content = str(rs) if rs else "[关系状态未知]"
        return (
            ProviderMessage(role="system", content=content),
            ContextBlock(
                block_id="relationship_state",
                kind=ContextBlockKind.RELATIONSHIP_STATE,
                cache_class=CacheClass.LOW_FREQUENCY,
                content_summary=f"relationship state ({_est_tokens(content)} tokens)",
                source="ContextBuilder",
            ),
        )

    # ── SystemTimeBlock ────────────────────────────────────────────

    def _make_system_time_block(
        self, request: BuildContextRequest, warnings: list[ContextBuildWarning],
    ) -> tuple[ProviderMessage, ContextBlock]:
        if request.system_time is None:
            warnings.append(ContextBuildWarning(
                source="system_time_unavailable",
                detail="system_time is None, entering degraded mode",
                severity="warning",
                block_affected="system_time_block",
            ))
            content = "系统时间不可用。如果需要时间信息，使用 get_time 工具获取。"
            stb = SystemTimeBlock(
                system_time=None, resume_type=ResumeType.COLD_START,
                has_previous=False,
            )
        else:
            st = request.system_time
            # Derive temporal info from recent_history
            history_dicts = _conv_history_to_dicts(request.recent_history)
            now_dt = st.dt
            current_ts = st.dt.timestamp()

            # Use TemporalContextBuilder for formatting
            temporal: TemporalContextResult = self._temporal.build(
                now=now_dt, history=history_dicts, current_user_ts=current_ts,
            )
            diag = temporal.diagnostics
            has_prev = diag.get("has_previous_interaction", False)
            gap = diag.get("gap_seconds", 0.0)
            resume_type_str = diag.get("resume_type", "cold_start")

            stb = SystemTimeBlock(
                system_time=st,
                last_user_ts=diag.get("last_user_message_at") or None,
                last_assistant_ts=diag.get("last_assistant_visible_message_at") or None,
                gap_seconds=gap if has_prev else None,
                resume_type=_map_resume_type(resume_type_str),
                has_previous=has_prev,
            )
            content = self._format_system_time_content(stb)

        return (
            ProviderMessage(role="system", content=content),
            ContextBlock(
                block_id="system_time_block",
                kind=ContextBlockKind.SYSTEM_TIME,
                cache_class=CacheClass.LOW_FREQUENCY,
                content_summary=f"system time ({stb.resume_type.value})",
                source="ContextBuilder",
            ),
        )

    def _format_system_time_content(self, stb: SystemTimeBlock) -> str:
        lines: list[str] = []
        if stb.system_time:
            st = stb.system_time
            lines.append(
                f"当前时间：{st.dt.year}年{st.dt.month:02d}月{st.dt.day:02d}日 "
                f"{st.dt.hour:02d}:{st.dt.minute:02d} {st.weekday_cn}"
            )

        if not stb.has_previous:
            lines.append("这是首次对话，无历史互动记录。")
            lines.append(f"resume_type: {stb.resume_type.value}")
            return "\n".join(lines)

        if stb.last_user_ts and stb.system_time:
            tz = timezone(timedelta(hours=8))
            dt = datetime.fromtimestamp(stb.last_user_ts, tz=tz)
            lines.append(f"上次用户消息：{dt.month:02d}月{dt.day:02d}日 {dt.hour:02d}:{dt.minute:02d}")

        if stb.last_assistant_ts and stb.system_time:
            tz = timezone(timedelta(hours=8))
            dt = datetime.fromtimestamp(stb.last_assistant_ts, tz=tz)
            lines.append(f"上次你的回复：{dt.month:02d}月{dt.day:02d}日 {dt.hour:02d}:{dt.minute:02d}")

        if stb.gap_seconds is not None:
            lines.append(f"距离上次互动：{_format_time_gap(stb.gap_seconds)}")

        lines.append(f"resume_type: {stb.resume_type.value}")
        lines.append(
            "使用规则：时间差只是事实，只有和用户当前消息有关时才自然提及，"
            "不要刻意表演'我一直在等你'。"
        )
        return "\n".join(lines)

    # ── Memory blocks ──────────────────────────────────────────────

    def _make_all_memory_blocks(
        self, request: BuildContextRequest,
    ) -> tuple[list[ProviderMessage], list[ContextBlock]]:
        msgs: list[ProviderMessage] = []
        blks: list[ContextBlock] = []
        if request.memory_selection is None:
            return msgs, blks

        ms = request.memory_selection

        # 7. boundary_memory (always first among memory)
        if ms.boundary_memory:
            msgs.append(ProviderMessage(
                role="system",
                content=_format_memory_list("用户明确边界", ms.boundary_memory),
            ))
            blks.append(ContextBlock(
                block_id="boundary_memory",
                kind=ContextBlockKind.MEMORY,
                cache_class=CacheClass.LOW_FREQUENCY,
                content_summary=f"{len(ms.boundary_memory)} boundary memories",
                source="MemorySystem",
            ))

        # 8. profile_memory
        if ms.profile_memory:
            msgs.append(ProviderMessage(
                role="system",
                content=_format_memory_list("用户画像", ms.profile_memory),
            ))
            blks.append(ContextBlock(
                block_id="selected_profile_memory",
                kind=ContextBlockKind.MEMORY,
                cache_class=CacheClass.LOW_FREQUENCY,
                content_summary=f"{len(ms.profile_memory)} profile memories",
                source="MemorySystem",
            ))

        # 9. episode_memory
        if ms.episode_memory:
            msgs.append(ProviderMessage(
                role="system",
                content=_format_memory_list("近期重要事件", ms.episode_memory),
            ))
            blks.append(ContextBlock(
                block_id="selected_episode_memory",
                kind=ContextBlockKind.MEMORY,
                cache_class=CacheClass.LOW_FREQUENCY,
                content_summary=f"{len(ms.episode_memory)} episode memories",
                source="MemorySystem",
            ))

        # 10. open_thread_memory
        if ms.open_thread_memory:
            msgs.append(ProviderMessage(
                role="system",
                content=_format_memory_list("未完成上下文线索", ms.open_thread_memory),
            ))
            blks.append(ContextBlock(
                block_id="selected_open_threads",
                kind=ContextBlockKind.MEMORY,
                cache_class=CacheClass.LOW_FREQUENCY,
                content_summary=f"{len(ms.open_thread_memory)} open threads",
                source="MemorySystem",
            ))

        return msgs, blks

    # ── Recent history ─────────────────────────────────────────────

    def _make_recent_history_block(
        self, request: BuildContextRequest,
    ) -> tuple[list[ProviderMessage], ContextBlock | None]:
        """Render conversation history as individual user/assistant messages.

        Only includes delivered assistant messages. failed/unknown excluded.
        Returns individual ProviderMessages (not wrapped in system block).
        """
        history = request.recent_history
        if not history:
            return [], None

        msgs: list[ProviderMessage] = []
        msg_ids: list[str] = []
        for h in history:
            if h.role == "user":
                msgs.append(ProviderMessage(role="user", content=h.content))
                msg_ids.append(h.message_id)
            elif h.role == "assistant" and h.delivery_status == DeliveryStatus.DELIVERED:
                msgs.append(ProviderMessage(role="assistant", content=h.content))
                msg_ids.append(h.message_id)
            # failed/unknown assistant messages are excluded (I-5)

        if not msgs:
            return [], None

        return msgs, ContextBlock(
            block_id="recent_conversation_history",
            kind=ContextBlockKind.RECENT_HISTORY,
            cache_class=CacheClass.TURN_DYNAMIC,
            content_summary=f"{len(msg_ids)} history messages from {len(history)} stored",
            source="ContextBuilder",
        )

    # ── Turn-local state ───────────────────────────────────────────

    def _make_turn_local_block(
        self, request: BuildContextRequest,
    ) -> tuple[ProviderMessage, ContextBlock] | None:
        tls = request.current_turn_local_state
        if (
            tls is None
            or (
                tls.model_call_count == 0
                and not tls.sent_messages_this_turn
                and not tls.tool_results_this_turn
            )
        ):
            return None
        lines: list[str] = ["[本轮状态]"]

        lines.append(f"模型调用次数: {tls.model_call_count}")
        if tls.sent_messages_this_turn:
            lines.append("")
            lines.append("[本轮已发送消息]")
            for i, m in enumerate(tls.sent_messages_this_turn, 1):
                if m.status == DeliveryStatus.DELIVERED:
                    lines.append(f"{i}. [delivered] \"{m.text}\"")
                elif m.status == DeliveryStatus.FAILED:
                    lines.append(f"{i}. [failed - 用户未看到] \"{m.text}\"")
                else:
                    lines.append(f"{i}. [{m.status.value}] \"{m.text}\"")
        if tls.tool_results_this_turn:
            lines.append("")
            lines.append("[本轮工具结果]")
            for r in tls.tool_results_this_turn:
                lines.append(f"- {r.tool_name}: {r.result_payload}")
        lines.append("")
        lines.append(f"已发送: {tls.delivered_message_count} (送达), {tls.failed_send_attempt_count} (失败)")

        content = "\n".join(lines)
        return (
            ProviderMessage(role="system", content=content),
            ContextBlock(
                block_id="current_turn_local_state",
                kind=ContextBlockKind.TURN_LOCAL,
                cache_class=CacheClass.TURN_DYNAMIC,
                content_summary=f"turn local (calls={tls.model_call_count if tls else 0})",
                source="TurnEngine",
            ),
        )

    # ── Media context ──────────────────────────────────────────────

    def _make_media_context_block(
        self, request: BuildContextRequest,
    ) -> tuple[ProviderMessage, ContextBlock]:
        lines: list[str] = []
        for item in request.media_context:
            kind_label = "图片" if item.kind == "image" else "语音"
            summary = item.summary or "(summary unavailable)"
            lines.append(f"[用户发送了一张{kind_label}]")
            lines.append(f"summary: {summary}")

        content = "\n".join(lines)
        return (
            ProviderMessage(role="system", content=content),
            ContextBlock(
                block_id="media_context",
                kind=ContextBlockKind.MEDIA,
                cache_class=CacheClass.TURN_DYNAMIC,
                content_summary=f"{len(request.media_context)} media items",
                source="ContextBuilder",
            ),
        )

    # ── Current input ──────────────────────────────────────────────

    def _make_current_input_block(
        self, request: BuildContextRequest,
    ) -> tuple[ProviderMessage, ContextBlock] | None:
        # Non-user triggers: system role
        if request.inbound_batch is None and request.trigger is not None:
            trigger_text = request.trigger.reason or "后台触发"
            content = f"[系统触发] {trigger_text}"
            role = "system"
        elif request.inbound_batch is not None:
            texts: list[str] = []
            for item in request.inbound_batch.items:
                if item.kind == MessageKind.TEXT and item.text:
                    texts.append(item.text)
                elif item.kind == MessageKind.IMAGE:
                    texts.append("[用户发送了图片]")
                elif item.kind == MessageKind.VOICE:
                    texts.append("[用户发送了语音]")
            content = "\n".join(texts) if texts else "[无文本输入]"
            role = "user" if request.turn_type in (
                TurnType.USER_CHAT, TurnType.USER_MEDIA_CHAT,
                TurnType.USER_REMINDER_REQUEST, TurnType.MEMORY_MANAGEMENT,
            ) else "system"
        else:
            return None

        return (
            ProviderMessage(role=role, content=content),
            ContextBlock(
                block_id="current_input",
                kind=ContextBlockKind.CURRENT_INPUT,
                cache_class=CacheClass.TURN_DYNAMIC,
                content_summary=f"current input (role={role})",
                source="ContextBuilder",
            ),
        )

    # ── Tool contract ──────────────────────────────────────────────

    def _make_tool_contract_block(
        self, request: BuildContextRequest,
    ) -> tuple[ProviderMessage, ContextBlock]:
        te = request.tool_exposure
        content = te.tool_contract_block if te else ""
        return (
            ProviderMessage(role="system", content=content),
            ContextBlock(
                block_id="exposed_tools_contract",
                kind=ContextBlockKind.TOOL_CONTRACT,
                cache_class=CacheClass.TURN_DYNAMIC,
                content_summary=f"exposed tools: {te.exposed_tool_names if te else 'none'}",
                source="ToolExposurePolicy",
            ),
        )

    # ── Terminal policy ────────────────────────────────────────────

    def _make_terminal_policy_block(
        self, request: BuildContextRequest,
    ) -> tuple[ProviderMessage, ContextBlock]:
        tp = request.terminal_policy
        lines: list[str] = ["[终态规则]"]
        if tp:
            lines.append(f"必须调用的终态工具: {', '.join(tp.required_terminal_tools)}")
            if tp.allows_skip:
                lines.append("阶段: 可跳过")
            elif tp.forced_speak:
                lines.append("阶段: 强制开口（上一轮已选择跳过）")
                lines.append("必须发送至少 1 条消息。不允许再次跳过。")
            else:
                lines.append("正常结束: 调用 finish_turn()")
            if tp.requires_next_wake:
                lines.append("finish_turn 必须携带 next_wake_minutes 参数。")
            lines.append(f"最少送达消息: {tp.min_delivered_messages}")

        content = "\n".join(lines)
        return (
            ProviderMessage(role="system", content=content),
            ContextBlock(
                block_id="terminal_policy",
                kind=ContextBlockKind.TERMINAL_POLICY,
                cache_class=CacheClass.TURN_DYNAMIC,
                content_summary=f"terminal: skip={tp.allows_skip if tp else 'N/A'}, "
                               f"forced={tp.forced_speak if tp else 'N/A'}",
                source="TurnEngine",
            ),
        )

    # ── Snapshot ───────────────────────────────────────────────────

    def _build_snapshot(
        self, request: BuildContextRequest, blocks: list[ContextBlock],
    ) -> ContextSnapshot:
        now = datetime.utcnow()
        snap_id = f"snap_{request.turn_id}_{request.call_id}"
        history_ids: list[str] = [
            h.message_id for h in request.recent_history
        ] if request.recent_history else []
        memory_ids: list[str] = []
        if request.memory_selection:
            for mem_list in (
                request.memory_selection.boundary_memory,
                request.memory_selection.profile_memory,
                request.memory_selection.episode_memory,
                request.memory_selection.open_thread_memory,
            ):
                for mem in mem_list:
                    mid = getattr(mem, "memory_id", None) or getattr(mem, "id", None)
                    if mid:
                        memory_ids.append(str(mid))

        tls = request.current_turn_local_state
        turn_summary: dict[str, int] = {
            "delivered_count": tls.delivered_message_count if tls else 0,
            "failed_count": tls.failed_send_attempt_count if tls else 0,
            "tool_results_count": len(tls.tool_results_this_turn) if tls else 0,
            "rejected_count": len(tls.rejected_tool_calls) if tls else 0,
        }

        exposed = (
            request.tool_exposure.exposed_tool_names
            if request.tool_exposure else []
        )

        return ContextSnapshot(
            snapshot_id=snap_id,
            turn_id=request.turn_id,
            call_id=request.call_id,
            created_at=now,
            block_summaries=list(blocks),
            exposed_tool_names=list(exposed),
            history_message_ids=history_ids,
            memory_ids=memory_ids,
            turn_local_summary=turn_summary,
            terminal_policy=request.terminal_policy,
            full_payload_ref=(
                f"snapshot:{snap_id}:full_payload" if request.debug_mode else None
            ),
        )

    # ── Token budget ───────────────────────────────────────────────

    def _estimate_token_budget(
        self, messages: list[ProviderMessage], budget: int,
    ) -> TokenBudgetReport:
        total = 0
        for m in messages:
            content = m.content
            if isinstance(content, str):
                total += _est_tokens(content)
            elif isinstance(content, list):
                for part in content:
                    total += _est_tokens(getattr(part, "text", "") or "")
        remaining = budget - total
        return TokenBudgetReport(
            budget=budget, used=total, remaining=remaining,
            budget_exhausted=remaining <= 0,
        )

    # ── Validation helpers ─────────────────────────────────────────

    def _verify_tool_results_not_user_role(
        self, messages: list[ProviderMessage], warnings: list[ContextBuildWarning],
    ) -> None:
        for m in messages:
            if m.role == "tool" and m.tool_call_id:
                continue  # valid tool result
            # Sanity: no provider message with role=user should contain tool-like content
            if m.role == "user" and isinstance(m.content, str):
                if m.content.strip().startswith("{"):
                    warnings.append(ContextBuildWarning(
                        source="role_check",
                        detail="user-role message starts with '{' — possible tool result leak",
                        severity="warning",
                        block_affected="current_input",
                    ))

    def _verify_media_not_user_text(
        self, blocks: list[ContextBlock], warnings: list[ContextBuildWarning],
    ) -> None:
        for blk in blocks:
            if blk.kind == ContextBlockKind.MEDIA and blk.enters_model:
                # media block is system role — correct
                pass


# ── Module helpers ─────────────────────────────────────────────────


def _est_tokens(text: str) -> int:
    """Rough token estimate: ~2 chars per token for CJK, ~4 for English."""
    if not text:
        return 0
    cjk = sum(1 for c in text if '一' <= c <= '鿿')
    other = len(text) - cjk
    return max(1, (cjk // 2) + (other // 4))


def _map_resume_type(rt: str) -> ResumeType:
    mapping = {
        CONTINUOUS_CHAT: ResumeType.CONTINUOUS_CHAT,
        SHORT_RESUME: ResumeType.SHORT_RESUME,
        SAME_DAY_RESUME: ResumeType.SAME_DAY_RESUME,
        NEXT_DAY_RESUME: ResumeType.NEXT_DAY_RESUME,
        LONG_GAP_RESUME: ResumeType.LONG_GAP_RESUME,
        "cold_start": ResumeType.COLD_START,
    }
    return mapping.get(rt, ResumeType.COLD_START)


def _conv_history_to_dicts(
    history: list[ConversationHistoryMessage],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for h in history:
        d: dict[str, Any] = {
            "role": h.role,
            "content": h.content,
            "source": h.source,
        }
        if h.created_at:
            d["ts"] = h.created_at.timestamp()
        result.append(d)
    return result


def _format_memory_list(title: str, memories: list[Any]) -> str:
    lines = [f"[{title}]"]
    for i, m in enumerate(memories, 1):
        text = getattr(m, "content", str(m))
        lines.append(f"{i}. {text}")
    return "\n".join(lines)


def _provider_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for part in content:
            text = getattr(part, "text", None)
            if text:
                out.append(str(text))
        return "\n".join(out)
    return str(content)


def _serialize_history(msgs: list[ProviderMessage]) -> str:
    """Convert provider messages back to a serialized history format.

    For recent_conversation_history, we emit user/assistant messages directly
    as individual ProviderMessages in the built context — but the spec says
    recent_history should be user/assistant alternating, not a single system message.

    This method is used when we want a single system block summarizing history.
    Actually, per spec §6.1, recent_history is user/assistant交替 messages directly
    in provider_messages, not wrapped in a system block.
    """
    lines: list[str] = []
    for m in msgs:
        if m.role == "user":
            lines.append(f"用户: {m.content}")
        elif m.role == "assistant":
            lines.append(f"助手: {m.content}")
    return "\n".join(lines)
