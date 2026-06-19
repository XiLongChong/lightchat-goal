"""Configuration loading and runtime settings management.

Extracted from main.py — batch G of main.py responsibility splitting.

This module is intentionally low-level: it only reads, merges, and
persists configuration.  It knows nothing about bridges, chat agents,
task schedulers, tools, or FastAPI — component synchronisation after a
config change stays in ``main.py``'s ``apply_runtime_settings``.

Public API:
    env_replace(value)            — ${VAR:-default} substitution
    load_config(path)             — load config.yaml with env substitution
    deep_update(base, updates)    — recursive dict merge
    load_runtime_settings(path)   — load data/runtime_settings.json ({} on missing/invalid)
    save_runtime_settings(path, settings) — write UTF-8 JSON (ensure_ascii=False, indent=2)
    RuntimeConfig                 — encapsulates cfg + runtime_settings lifecycle
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("goal")

# Pre-compiled pattern for ${VAR} and ${VAR:-default}
_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")


def env_replace(value: str) -> str:
    """Replace ``${VAR:-default}`` patterns with env values.

    - ``${VAR:-fallback}`` → env value, or ``fallback`` if unset.
    - ``${VAR}`` → env value, or empty string if unset.
    """

    def replacer(match: re.Match[str]) -> str:
        var_expr = match.group(1)
        if ":-" in var_expr:
            var, default = var_expr.split(":-", 1)
            return os.getenv(var.strip(), default.strip())
        return os.getenv(var_expr.strip(), "")

    return _ENV_PATTERN.sub(replacer, value)


def load_config(path: str | Path = "config.yaml") -> dict[str, Any]:
    """Load a YAML config file with ``${VAR:-default}`` env substitution."""
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    raw = env_replace(raw)
    return yaml.safe_load(raw)


def deep_update(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``updates`` into ``base`` (in-place)."""
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_update(base[key], value)
        else:
            base[key] = value
    return base


def load_runtime_settings(path: str | Path) -> dict[str, Any]:
    """Load runtime settings JSON.  Returns ``{}`` on missing or invalid file."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.warning("Failed to load runtime settings", exc_info=True)
        return {}


def save_runtime_settings(path: str | Path, settings: dict[str, Any]) -> None:
    """Write runtime settings as UTF-8 JSON (``ensure_ascii=False``, ``indent=2``)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


class RuntimeConfig:
    """Encapsulates config.yaml + runtime_settings.json lifecycle.

    On construction, loads ``config.yaml`` (with env substitution) and
    ``runtime_settings.json``, then deep-merges runtime settings into
    the config dict — matching the original ``main.py`` load sequence.

    ``apply_updates`` persists updates to the runtime settings file and
    merges them into the live config dict.  It does **not** synchronise
    runtime components (bridges, agents, etc.) — that remains the
    caller's responsibility.
    """

    def __init__(
        self,
        config_path: str | Path,
        runtime_settings_path: str | Path,
    ) -> None:
        self._config_path = Path(config_path)
        self._runtime_settings_path = Path(runtime_settings_path)
        self._cfg: dict[str, Any] = load_config(self._config_path)
        self._runtime_settings: dict[str, Any] = load_runtime_settings(
            self._runtime_settings_path
        )
        deep_update(self._cfg, self._runtime_settings)

    @property
    def cfg(self) -> dict[str, Any]:
        return self._cfg

    @property
    def runtime_settings(self) -> dict[str, Any]:
        return self._runtime_settings

    def apply_updates(self, updates: dict[str, Any]) -> dict[str, Any]:
        """Persist ``updates`` to runtime settings and merge into cfg.

        Returns the updated ``cfg`` dict (also accessible via ``self.cfg``).
        """
        deep_update(self._runtime_settings, updates)
        save_runtime_settings(self._runtime_settings_path, self._runtime_settings)
        deep_update(self._cfg, updates)
        return self._cfg
