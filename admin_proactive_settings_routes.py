"""Admin proactive settings routes — V1.6-A.

Dependency-injection pattern: create_proactive_settings_router(deps) returns
a configured APIRouter. Does NOT import main.py globals.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request

from agent.proactive_settings import (
    load_proactive_settings,
    save_proactive_settings,
    ProactiveSettings,
)

logger = logging.getLogger(__name__)


def create_proactive_settings_router(
    *,
    require_admin: Callable[[Request], None],
    config: dict[str, Any],
) -> APIRouter:
    """Build and return a router for /admin/proactive/settings endpoints."""

    router = APIRouter(prefix="/admin/proactive")

    @router.get("/settings")
    async def get_settings(request: Request):
        require_admin(request)
        try:
            settings = load_proactive_settings(config)
            return settings.to_dict()
        except Exception as e:
            logger.error("Failed to load proactive settings: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/settings")
    async def save_settings(request: Request):
        require_admin(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Request body must be valid JSON")

        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object")

        try:
            settings = ProactiveSettings.from_dict(body)
            save_proactive_settings(settings)
            logger.info("ProactiveSettings updated via API: %s", json.dumps(body, ensure_ascii=False))
            return {"status": "ok", "settings": settings.to_dict()}
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        except Exception as e:
            logger.error("Failed to save proactive settings: %s", e)
            raise HTTPException(status_code=500, detail=str(e))

    return router
