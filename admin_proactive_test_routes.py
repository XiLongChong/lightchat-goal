"""Admin proactive test routes — v3.1 Phase 2.

Dependency-injection pattern: create_proactive_test_router(deps) returns
a configured APIRouter.  Does NOT import main.py globals.
"""

from __future__ import annotations

import json as _json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)

_TZ = timezone(timedelta(hours=8))


def create_proactive_test_router(
    *,
    require_admin: Any,                              # Callable[[Request], None]
    proactive_scheduler: Any,                        # ProactiveScheduler
    chat_agent: Any,                                 # ChatAgent
    memory_store: Any,                               # MemoryStore
    get_history_async: Any,                          # Callable[[str, str], Coroutine]
    build_enriched_system_prompt: Any,               # Callable[[str, str], Coroutine]
    wechat_sender: Any,                              # Callable[[str, str, list], Coroutine]
    make_interrupt_checker: Any,                     # Callable[[str, int], InterruptChecker]
    bot_id: str,
    build_proactive_post_turn_hooks: Any,             # Callable[[str, str, dict], list]
    build_protocol_event_writer: Any = None,
    get_current_inbound_seq: Any = None,             # Callable[[str], int]
) -> APIRouter:
    """Build and return a router for /admin/proactive-test/* endpoints."""

    router = APIRouter(prefix="/admin/proactive-test")

    # ── Helpers ───────────────────────────────────────────────────

    def _parse_debug_now(raw: str | None) -> datetime | None:
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw)
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=_TZ)
            return dt
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail=f"Invalid debug_now: {raw}")

    def _interrupt_baseline(uid: str) -> int:
        if callable(get_current_inbound_seq):
            try:
                return int(get_current_inbound_seq(uid) or 0)
            except Exception:
                return 0
        return 0

    # ══════════════════════════════════════════════════════════════
    # GET /admin/proactive-test/bootstrap
    # ══════════════════════════════════════════════════════════════

    @router.get("/bootstrap")
    async def bootstrap(request: Request):
        require_admin(request)

        contact = await memory_store.get_contact_state()
        user_id = str(contact.get("user_id", ""))
        bridge_online = _check_bridge(proactive_scheduler)

        features_cfg = (proactive_scheduler._config.get("features") or {}) if proactive_scheduler else {}
        proactive_on = bool(features_cfg.get("proactive", False))

        today_sent = 0
        last_attempt_at = None
        if user_id:
            try:
                tz = _TZ
                now_dt = datetime.now(tz)
                today_start = datetime.combine(
                    now_dt.date(), datetime.min.time(), tzinfo=tz,
                ).timestamp()
                today_sent = await memory_store.count_today_proactive_sent(today_start)
            except Exception:
                pass
            try:
                ps = await memory_store.get_proactive_schedule_state(user_id, bot_id)
                if ps and ps.get("last_attempt_ts"):
                    last_attempt_at = datetime.fromtimestamp(
                        float(ps["last_attempt_ts"]), tz=_TZ,
                    ).isoformat()
            except Exception:
                pass

        return {
            "ok": True,
            "current_user_id": user_id or "default_user",
            "current_bot_id": bot_id,
            "bridge": {
                "online": bridge_online,
                "logged_in": bool(user_id),
            },
            "features": {
                "proactive": proactive_on,
            },
            "proactive": {
                "heartbeat_enabled": proactive_on,
                "last_attempt_at": last_attempt_at,
                "today_real_send_count": today_sent,
            },
        }

    # ══════════════════════════════════════════════════════════════
    # POST /admin/proactive-test/check
    # ══════════════════════════════════════════════════════════════

    @router.post("/check")
    async def check_eligibility(request: Request):
        require_admin(request)
        body = await _parse_json_body(request)
        user_id = str(body.get("user_id") or _contact_user_id(memory_store))
        bot = str(body.get("bot_id") or bot_id)
        debug_now = _parse_debug_now(body.get("debug_now"))
        sim_silent = _parse_optional_int(body.get("simulate_user_silent_seconds"))
        sim_last = _parse_optional_int(body.get("simulate_last_proactive_seconds"))

        result = await proactive_scheduler.run_debug_eligibility(
            user_id, bot,
            debug_now=debug_now,
            simulate_user_silent_seconds=sim_silent,
            simulate_last_proactive_seconds=sim_last,
        )
        return {"ok": True, **result}

    # ══════════════════════════════════════════════════════════════
    # POST /admin/proactive-test/impulses
    # ══════════════════════════════════════════════════════════════

    @router.post("/impulses")
    async def impulses(request: Request):
        require_admin(request)
        body = await _parse_json_body(request)
        user_id = str(body.get("user_id") or _contact_user_id(memory_store))
        bot = str(body.get("bot_id") or bot_id)
        forced = str(body.get("forced_impulse_type") or "AUTO")
        debug_now = _parse_debug_now(body.get("debug_now"))

        result = await proactive_scheduler.run_debug_impulse_pipeline(
            user_id, bot,
            forced_impulse_type=forced,
            debug_now=debug_now,
        )
        return {"ok": True, **result}

    # ══════════════════════════════════════════════════════════════
    # POST /admin/proactive-test/run
    # ══════════════════════════════════════════════════════════════

    @router.post("/run")
    async def run_test(request: Request):
        require_admin(request)
        body = await _parse_json_body(request)

        mode = str(body.get("mode", "dry_run"))
        if mode not in ("dry_run", "real_send"):
            raise HTTPException(status_code=400, detail=f"mode must be dry_run or real_send, got {mode!r}")

        user_id = str(body.get("user_id") or _contact_user_id(memory_store))
        bot = str(body.get("bot_id") or bot_id)

        if not user_id:
            raise HTTPException(status_code=400, detail="no user_id available")

        forced = str(body.get("forced_impulse_type") or "AUTO")
        debug_now = _parse_debug_now(body.get("debug_now"))
        sim_silent = _parse_optional_int(body.get("simulate_user_silent_seconds"))
        sim_last = _parse_optional_int(body.get("simulate_last_proactive_seconds"))

        ctx_kwargs = {
            "mode": mode,
            "user_id": user_id,
            "bot_id": bot,
            "forced_impulse_type": forced,
            "debug_now": debug_now,
            "simulate_user_silent_seconds": sim_silent,
            "simulate_last_proactive_seconds": sim_last,
        }

        from agent.proactive_debug import ProactiveDebugContext, run_dry_run, run_real_send

        ctx = ProactiveDebugContext(**ctx_kwargs)

        if mode == "dry_run":
            # Providers for dry-run
            async def _hist_provider(uid: str, bid: str):
                return await get_history_async(uid, bot_id=bid)

            async def _sys_prompt_provider(uid: str):
                return await build_enriched_system_prompt(uid, "")

            def _int_checker_provider(uid: str):
                return make_interrupt_checker(uid, _interrupt_baseline(uid))

            baseline = _interrupt_baseline(user_id)

            report = await run_dry_run(
                ctx,
                scheduler=proactive_scheduler,
                chat_agent=chat_agent,
                memory_store=memory_store,
                history_provider=_hist_provider,
                system_prompt_provider=_sys_prompt_provider,
                interrupt_checker_provider=_int_checker_provider(user_id),
                input_latest_seq=baseline,
            )
        else:
            # Real-send: use production providers
            async def _hist_provider(uid: str, bid: str):
                return await get_history_async(uid, bot_id=bid)

            async def _sys_prompt_provider(uid: str):
                return await build_enriched_system_prompt(uid, "")

            async def _ws_provider(messages: list):
                # Resolve context_token from contact state
                contact = await memory_store.get_contact_state()
                ctx_token = str(contact.get("context_token", ""))
                return await wechat_sender(user_id, ctx_token, messages)

            def _int_checker_provider(uid: str):
                return make_interrupt_checker(uid, _interrupt_baseline(uid))

            baseline = _interrupt_baseline(user_id)

            hooks = build_proactive_post_turn_hooks(user_id, bot, {})
            protocol_writer = (
                build_protocol_event_writer(user_id, bot)
                if callable(build_protocol_event_writer)
                else None
            )

            report = await run_real_send(
                ctx,
                scheduler=proactive_scheduler,
                memory_store=memory_store,
                history_provider=_hist_provider,
                system_prompt_provider=_sys_prompt_provider,
                wechat_sender_provider=_ws_provider,
                interrupt_checker_provider=_int_checker_provider(user_id),
                input_latest_seq=baseline,
                post_turn_hooks=hooks,
                protocol_event_writer=protocol_writer,
            )

        return {"ok": True, **report.to_dict()}

    # ══════════════════════════════════════════════════════════════
    # GET /admin/proactive-test/runs
    # ══════════════════════════════════════════════════════════════

    @router.get("/runs")
    async def list_runs(
        request: Request,
        limit: int = 20,
        mode: str = "all",
        terminal_state: str = "",
        send_simulated: str = "",
        real_send_attempted: str = "",
        impulse_type: str = "",
    ):
        require_admin(request)
        contact = await memory_store.get_contact_state()
        user_id = str(contact.get("user_id", ""))
        bot = bot_id

        sim_filter: bool | None = None
        if send_simulated.lower() in ("true", "1", "yes"):
            sim_filter = True
        elif send_simulated.lower() in ("false", "0", "no"):
            sim_filter = False

        real_filter: bool | None = None
        if real_send_attempted.lower() in ("true", "1", "yes"):
            real_filter = True
        elif real_send_attempted.lower() in ("false", "0", "no"):
            real_filter = False

        items = await memory_store.list_debug_runs(
            user_id=user_id,
            bot_id=bot,
            limit=limit,
            mode=mode,
            terminal_state=terminal_state,
            send_simulated=sim_filter,
            real_send_attempted=real_filter,
            impulse_type=impulse_type,
        )
        return {"ok": True, "items": items}

    # ══════════════════════════════════════════════════════════════
    # GET /admin/proactive-test/runs/{run_id}
    # ══════════════════════════════════════════════════════════════

    @router.get("/runs/{run_id}")
    async def get_run(run_id: str, request: Request):
        require_admin(request)
        run = await memory_store.get_debug_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
        return {"ok": True, "run": run}

    return router


# ══════════════════════════════════════════════════════════════════
# Internal helpers
# ══════════════════════════════════════════════════════════════════

async def _parse_json_body(request: Request) -> dict[str, Any]:
    """Parse JSON body, returning empty dict if missing."""
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


def _parse_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _check_bridge(scheduler: Any) -> bool:
    """Check bridge online via scheduler's bridge_online_check."""
    if scheduler and scheduler._bridge_online_check and callable(scheduler._bridge_online_check):
        try:
            return bool(scheduler._bridge_online_check())
        except Exception:
            pass
    return False


async def _contact_user_id(store: Any) -> str:
    """Resolve current contact user_id from store."""
    try:
        contact = await store.get_contact_state()
        return str(contact.get("user_id", ""))
    except Exception:
        return ""
