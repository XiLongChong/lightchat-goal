"""Observability post-turn hooks — dev logs and model attempt logging.

Extracted from main.py — batch D of main.py responsibility splitting.
"""

from __future__ import annotations

import logging
from typing import Any

from agent.state_machine import (
    PostTerminalCallback,
    TurnScratchpad,
    TurnState,
)
from runtime.admin_labels import visible_messages_as_dicts

logger = logging.getLogger("goal")

_IS_DELIVERED: set[str] = {"delivered", "sent_confirmed"}


def build_observability_hooks(
    *,
    user_id: str,
    bot_id: str = "",
    route: str = "",
    context_policy: str = "",
    raw_messages: list[str] | None = None,
    memory_store: Any = None,
) -> list[PostTerminalCallback]:
    """Return the two generic observability hooks: dev-logs and model-attempt-log.

    The proactive-scheduling hook is *not* included — it stays in ``main.py``
    because it captures ``task_scheduler`` and proactive-task metadata.
    """

    async def _hook_dev_logs(
        turn: TurnScratchpad,
        terminal_state: TurnState,
    ) -> None:
        visible = visible_messages_as_dicts(turn)
        delivered_count = sum(
            1
            for m in visible
            if (
                m.get("send_status") in _IS_DELIVERED
                or m.get("delivery_status") in _IS_DELIVERED
            )
        )
        token_count = turn.token_usage.get("total_tokens", 0)
        logger.info(
            "[%s] turn=%s terminal=%s delivered=%d "
            "tool_results=%d iterations=%d elapsed_ms=%d tokens=%d",
            user_id,
            turn.turn_id,
            terminal_state.value,
            delivered_count,
            len(turn.tool_results),
            turn.llm_call_count,
            turn.elapsed_ms,
            token_count,
        )

    async def _hook_model_attempt_log(
        turn: TurnScratchpad,
        terminal_state: TurnState,
    ) -> None:
        for attempt in turn.model_attempts:
            logger.info(
                "model_attempt: index=%d model=%s/%s mode=%s reason=%s "
                "latency=%dms result=%s error=%s upgraded_from=%s",
                attempt.attempt_index,
                attempt.selected_model,
                attempt.model_name,
                attempt.routing_mode,
                attempt.routing_reason,
                attempt.latency_ms,
                attempt.result_type,
                attempt.error_type,
                attempt.upgraded_from,
            )

    return [_hook_dev_logs, _hook_model_attempt_log]
