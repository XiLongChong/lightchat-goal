"""ToolExposurePolicy — single entry point for tool exposure decisions.

Phase 2: Facade that determines which tools the model can see in a given turn.
Uses new canonical tool names (send_text_message, finish_turn, skip_initiative)
with migration mapping to old registry names (wechat_reply, done, skip_proactive).

Spec: docs/runtime-specs/02-tool-exposure-policy-spec.zh.md
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from agent.tool_registry import TOOL_REGISTRY, ToolDef
from runtime_contracts import (
    DeniedTool,
    NativeToolSchema,
    ToolExposure,
    TurnPhase,
    TurnType,
    UserIntent,
    ToolCategory as RTCategory,
    ToolSideEffect,
)

logger = logging.getLogger(__name__)

# ── Migration map: new canonical name → old registry name ──────────
# Phase 11: delete this map after old names are removed from registry.
_MIGRATION_MAP: dict[str, str] = {
    "send_text_message": "wechat_reply",
    "finish_turn": "done",
    "skip_initiative": "skip_proactive",
    "schedule_user_reminder": "schedule_wake_up",
}

# Reverse: old → new (for denied tool rendering)
_OLD_TO_NEW: dict[str, str] = {v: k for k, v in _MIGRATION_MAP.items()}

# Tool names in new semantics
_SEND_TEXT = "send_text_message"
_FINISH_TURN = "finish_turn"
_SKIP_INITIATIVE = "skip_initiative"
_GET_TIME = "get_time"
_GET_WEATHER = "get_weather"
_GENERATE_IMAGE = "generate_image_message"
_SEND_IMAGE = "send_image_message"
_SEND_STICKER = "send_sticker"
_SCHEDULE_REMINDER = "schedule_user_reminder"
_MARK_REMINDER_DELIVERED = "mark_reminder_delivered"
_APPLY_FORGET = "apply_forget_directive"

# Registry names that don't need migration
_NO_MIGRATION = {_GET_TIME, _GET_WEATHER}


# ── Phase 2 input type ──────────────────────────────────────────────


@dataclass(slots=True)
class ToolExposureRequest:
    turn_type: TurnType
    trigger_source: str  # "user" | "scheduler" | "admin" | "bridge"
    user_intent: UserIntent = UserIntent.UNKNOWN
    system_time_available: bool = True
    # ── Initiative state (only for proactive turns) ──
    last_decision: str | None = None  # "spoke" | "skipped" | None
    consecutive_skip_count: int = 0
    delivered_message_count: int = 0
    # ── Runtime config ──
    runtime_config: dict[str, Any] | None = None


# ── ToolExposurePolicy ──────────────────────────────────────────────


class ToolExposurePolicy:
    """Single entry point for per-turn tool exposure decisions.

    One resolve() call produces one ToolExposure object.
    Caller (TurnEngine) calls resolve() before each CALL_MODEL.
    When phase changes mid-turn (e.g. after sending a message),
    re-resolve is required.
    """

    def __init__(self) -> None:
        self._registry = TOOL_REGISTRY

    # ── Public API ─────────────────────────────────────────────────

    def resolve(self, request: ToolExposureRequest) -> ToolExposure:
        phase = self._determine_phase(request)
        candidates = self._compute_candidate_tools(request, phase)
        denied: list[DeniedTool] = []

        # get_time special decision
        if not self._should_expose_get_time(request):
            candidates.discard(_GET_TIME)
            denied.append(DeniedTool(
                tool_name=_GET_TIME,
                reason="当前时间已通过系统上下文注入，不需要模型查询",
                would_expose_if="用户询问其他时区时间，或进行时间计算",
            ))

        # get_weather special decision
        if not self._should_expose_get_weather(request):
            candidates.discard(_GET_WEATHER)
            denied.append(DeniedTool(
                tool_name=_GET_WEATHER,
                reason="get_weather 仅在用户有天气意图时暴露，不作为默认工具",
                would_expose_if="用户询问天气或提及地点+天气",
            ))

        # Build denied list for all registry tools not in candidates
        denied = self._build_denied_list(candidates, denied, request)

        exposed = sorted(candidates)
        schemas = self._build_native_schemas(exposed)

        # Validate every exposed tool has executor
        for s in schemas:
            if not self._has_executor(s.name):
                raise ToolExposureBuildError(
                    reason=f"executor_missing: {s.name}",
                    details={"tool_name": s.name},
                )

        contract_block = self._build_contract_block(exposed, phase, request)
        self._validate_consistency(exposed, schemas, contract_block)

        return ToolExposure(
            exposure_id=_generate_exposure_id(),
            turn_id="",  # filled by caller
            phase=phase,
            exposed_tool_names=exposed,
            native_tool_schemas=schemas,
            tool_contract_block=contract_block,
            denied_tools=denied,
            reasons=self._build_reasons(exposed, denied, phase, request),
        )

    # ── Phase determination ────────────────────────────────────────

    def _determine_phase(self, request: ToolExposureRequest) -> TurnPhase:
        if request.turn_type == TurnType.RELATIONSHIP_PROACTIVE:
            if request.delivered_message_count > 0:
                return TurnPhase.AFTER_FIRST_MESSAGE
            if request.last_decision == "skipped":
                return TurnPhase.FORCED_SPEAK
            return TurnPhase.SKIPPABLE

        if request.turn_type == TurnType.USER_REMINDER_DUE:
            return TurnPhase.REMINDER_DELIVERY

        if request.turn_type == TurnType.MEMORY_MANAGEMENT:
            return TurnPhase.MEMORY_MANAGEMENT

        if request.turn_type == TurnType.ADMIN_MANUAL_TRIGGER:
            return TurnPhase.ADMIN

        return TurnPhase.NORMAL_CHAT

    # ── Tool candidate computation ──────────────────────────────────

    def _compute_candidate_tools(
        self, request: ToolExposureRequest, phase: TurnPhase,
    ) -> set[str]:
        tt = request.turn_type
        tools: set[str] = {_SEND_TEXT, _FINISH_TURN}

        if tt in (TurnType.USER_CHAT, TurnType.USER_MEDIA_CHAT):
            if request.user_intent == UserIntent.REQUEST_REMINDER:
                tools.add(_SCHEDULE_REMINDER)
            if request.user_intent == UserIntent.REQUEST_IMAGE and request.trigger_source == "user":
                tools.add(_GENERATE_IMAGE)
                tools.add(_SEND_IMAGE)
                tools.add(_SEND_STICKER)
            if request.user_intent == UserIntent.FORGET_DIRECTIVE:
                tools.add(_APPLY_FORGET)
            tools.add(_GET_TIME)
            tools.add(_GET_WEATHER)

        elif tt == TurnType.USER_REMINDER_REQUEST:
            tools.add(_SCHEDULE_REMINDER)
            tools.add(_GET_TIME)
            tools.add(_GET_WEATHER)

        elif tt == TurnType.USER_REMINDER_DUE:
            tools.add(_MARK_REMINDER_DELIVERED)
            tools.add(_GET_TIME)

        elif tt == TurnType.RELATIONSHIP_PROACTIVE:
            if phase == TurnPhase.SKIPPABLE:
                tools.add(_SKIP_INITIATIVE)

        elif tt == TurnType.SCHEDULED_RITUAL:
            tools.add(_GET_WEATHER)

        elif tt == TurnType.MEMORY_MANAGEMENT:
            tools.add(_APPLY_FORGET)

        elif tt == TurnType.ADMIN_MANUAL_TRIGGER:
            tools.add(_GET_WEATHER)

        # fallback (unknown): keep minimum safe set {send_text, finish_turn}
        return tools

    # ── get_time decision ───────────────────────────────────────────

    def _should_expose_get_time(self, request: ToolExposureRequest) -> bool:
        # Background turn types that must NOT expose get_time
        BACKGROUND_TURNS = {
            TurnType.RELATIONSHIP_PROACTIVE,
            TurnType.SCHEDULED_RITUAL,
            TurnType.ADMIN_MANUAL_TRIGGER,
        }

        if not request.system_time_available:
            if request.turn_type in BACKGROUND_TURNS or request.turn_type == TurnType.USER_REMINDER_DUE:
                return False
            return True

        if request.turn_type in (TurnType.USER_REMINDER_REQUEST, TurnType.USER_REMINDER_DUE):
            return True

        rc = request.runtime_config or {}
        if rc.get("user_has_timezone_query"):
            return True

        if request.user_intent == UserIntent.TIME_QUERY:
            return True

        return False

    # ── get_weather decision ────────────────────────────────────────

    def _should_expose_get_weather(self, request: ToolExposureRequest) -> bool:
        if request.user_intent == UserIntent.REQUEST_WEATHER:
            return True

        if request.turn_type not in (
            TurnType.USER_CHAT, TurnType.USER_MEDIA_CHAT,
            TurnType.SCHEDULED_RITUAL, TurnType.ADMIN_MANUAL_TRIGGER,
            TurnType.USER_REMINDER_REQUEST,
        ):
            return False

        if request.turn_type == TurnType.SCHEDULED_RITUAL:
            rc = request.runtime_config or {}
            return bool(rc.get("user_has_weather_habit", False))

        # USER_REMINDER_REQUEST: get_weather not exposed (spec W5)
        if request.turn_type == TurnType.USER_REMINDER_REQUEST:
            return False

        return False

    # ── Native schema construction ──────────────────────────────────

    def _registry_name(self, new_name: str) -> str:
        """Map new canonical name to old registry name."""
        if new_name in _NO_MIGRATION:
            return new_name
        return _MIGRATION_MAP.get(new_name, new_name)

    def _has_executor(self, new_name: str) -> bool:
        reg_name = self._registry_name(new_name)
        return self._registry.has(reg_name)

    def _build_native_schemas(self, exposed: list[str]) -> list[NativeToolSchema]:
        schemas: list[NativeToolSchema] = []
        for name in exposed:
            reg_name = self._registry_name(name)
            tool_def = self._registry.get(reg_name)
            if tool_def is None:
                raise ToolExposureBuildError(
                    reason=f"registry_missing: {name} (looked up as {reg_name})",
                    details={"tool_name": name},
                )
            schemas.append(self._tool_def_to_schema(name, tool_def))
        return schemas

    def _tool_def_to_schema(self, new_name: str, tool_def: ToolDef) -> NativeToolSchema:
        params = self._build_parameters_schema(tool_def)
        terminal = tool_def.tool_category in ("terminal_tool",)
        side_effect = self._classify_side_effect(tool_def)
        category = self._classify_category(tool_def)
        return NativeToolSchema(
            name=new_name,
            description=tool_def.description,
            parameters_schema=params,
            terminal=terminal,
            side_effect=side_effect,
            category=category,
        )

    @staticmethod
    def _build_parameters_schema(tool_def: ToolDef) -> dict[str, Any]:
        args = tool_def.args_schema
        if isinstance(args, dict):
            parameters: dict[str, Any] = {
                "type": "object",
                "properties": args.get("properties", {}),
            }
            if args.get("required"):
                parameters["required"] = args["required"]
            return parameters
        props: dict[str, Any] = {}
        for arg in args:
            props[arg] = {"type": "string", "description": arg}
        return {"type": "object", "properties": props}

    @staticmethod
    def _classify_side_effect(tool_def: ToolDef) -> ToolSideEffect:
        se = tool_def.side_effect_level
        if se == "high":
            return ToolSideEffect.SENDS_MESSAGE
        if se == "internal_message":
            return ToolSideEffect.WRITES_STATE
        if tool_def.tool_category in ("state_tool",):
            return ToolSideEffect.WRITES_STATE
        if tool_def.tool_category in ("fact_tool",):
            return ToolSideEffect.READS_EXTERNAL
        return ToolSideEffect.NONE

    @staticmethod
    def _classify_category(tool_def: ToolDef) -> ToolCategory:
        tc = tool_def.tool_category
        if tc == "action_tool":
            return RTCategory.OUTPUT
        if tc == "terminal_tool":
            return RTCategory.TURN_CONTROL
        if tc == "state_tool":
            return RTCategory.STATE
        if tc == "fact_tool":
            return RTCategory.INFORMATION
        return RTCategory.OUTPUT

    # ── Contract block ──────────────────────────────────────────────

    def _build_contract_block(
        self, exposed: list[str], phase: TurnPhase,
        request: ToolExposureRequest,
    ) -> str:
        lines: list[str] = []

        if phase == TurnPhase.FORCED_SPEAK:
            lines.append("本轮是上次跳过后的强制开口回合。你不能跳过。必须至少发送一条消息。")
        elif phase == TurnPhase.SKIPPABLE:
            lines.append(
                "你可以通过 send_text_message 发送消息，"
                "或通过 skip_initiative 声明本轮跳过。"
                "无论选择哪种，最后必须调用 finish_turn。"
            )
        elif phase == TurnPhase.AFTER_FIRST_MESSAGE:
            lines.append("你已经发送了消息。可以继续发送，或调用 finish_turn 结束本轮。")
        elif phase == TurnPhase.REMINDER_DELIVERY:
            lines.append("这是一个用户授权提醒。发送消息后必须调用 mark_reminder_delivered 标记送达。")

        lines.append("")
        lines.append("本轮可用工具：")

        for name in exposed:
            reg_name = self._registry_name(name)
            tool_def = self._registry.get(reg_name)
            if tool_def is None:
                continue
            desc = tool_def.description.split("。")[0]
            if name == _GET_TIME and name in exposed:
                desc += "。当前本地时间已通过系统消息提供，只在需要非默认时区或时间计算时使用"
            lines.append(f"  {name}: {desc}")

        if phase in (TurnPhase.SKIPPABLE, TurnPhase.FORCED_SPEAK, TurnPhase.AFTER_FIRST_MESSAGE):
            lines.append("")
            lines.append("finish_turn 必须携带 next_wake_minutes 参数指定下次唤醒分钟数。")

        return "\n".join(lines)

    # ── Validation ──────────────────────────────────────────────────

    def _validate_consistency(
        self, exposed: list[str], schemas: list[NativeToolSchema], contract_block: str,
    ) -> None:
        schema_names = {s.name for s in schemas}
        exposed_set = set(exposed)
        if schema_names != exposed_set:
            raise ToolExposureBuildError(
                reason="prompt_schema_divergence",
                details={
                    "exposed_not_in_schema": list(exposed_set - schema_names),
                    "schema_not_in_exposed": list(schema_names - exposed_set),
                },
            )
        for name in exposed:
            if name not in contract_block:
                logger.warning("Tool %s missing from contract block", name)

    # ── Denied list ─────────────────────────────────────────────────

    def _build_denied_list(
        self, candidates: set[str], pre_denied: list[DeniedTool],
        request: ToolExposureRequest,
    ) -> list[DeniedTool]:
        denied = list(pre_denied)
        all_registered_new = self._all_registered_new_names()
        for name in sorted(all_registered_new):
            if name in candidates:
                continue
            if any(d.tool_name == name for d in denied):
                continue
            reason, would_if = self._deny_reason(name, request)
            denied.append(DeniedTool(tool_name=name, reason=reason, would_expose_if=would_if))
        return denied

    def _all_registered_new_names(self) -> set[str]:
        names: set[str] = set()
        for old_name in self._registry.names():
            new_name = _OLD_TO_NEW.get(old_name, old_name)
            # Skip deprecated tools
            td = self._registry.get(old_name)
            if td is not None and not td.native_tool_call_eligible:
                continue
            names.add(new_name)
        return names

    def _deny_reason(
        self, tool_name: str, request: ToolExposureRequest,
    ) -> tuple[str, str | None]:
        image_tools = {_GENERATE_IMAGE, _SEND_IMAGE, _SEND_STICKER}
        if tool_name in image_tools:
            if request.turn_type == TurnType.RELATIONSHIP_PROACTIVE:
                return ("主动消息禁止发图片/表情", "用户回合且用户明确要求图片时")
            return ("用户未要求图片", "user_intent=request_image")

        if tool_name == _SKIP_INITIATIVE:
            if request.turn_type != TurnType.RELATIONSHIP_PROACTIVE:
                return ("非主动回合不可跳过", "relationship_proactive turn")
            return ("当前 phase 不允许跳过", "skippable phase")

        if tool_name == _SCHEDULE_REMINDER:
            return ("非提醒请求回合", "user_reminder_request turn")

        if tool_name == _MARK_REMINDER_DELIVERED:
            return ("非提醒到期回合", "user_reminder_due turn")

        if tool_name == _APPLY_FORGET:
            return ("非记忆管理回合", "memory_management turn 或 forget_directive intent")

        return ("由当前 turn 配置决定不暴露", None)

    # ── Reasons ─────────────────────────────────────────────────────

    def _build_reasons(
        self, exposed: list[str], denied: list[DeniedTool],
        phase: TurnPhase, request: ToolExposureRequest,
    ) -> list[str]:
        reasons: list[str] = []
        reasons.append(f"phase={phase.value}")
        reasons.append(f"exposed={len(exposed)} tools: {', '.join(exposed)}")
        reasons.append(f"denied={len(denied)} tools")
        if request.turn_type == TurnType.RELATIONSHIP_PROACTIVE:
            reasons.append(f"last_decision={request.last_decision or 'none'}")
        return reasons


# ── Error type ──────────────────────────────────────────────────────


class ToolExposureBuildError(Exception):
    def __init__(self, reason: str, details: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.details = details


# ── Helpers ─────────────────────────────────────────────────────────

import uuid

def _generate_exposure_id() -> str:
    return f"exp_{uuid.uuid4().hex[:12]}"
