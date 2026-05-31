"""ProactiveSettings — V1.6-A proactive message settings data structure.

Persistence: data/runtime_settings.json, "proactive" key.
First load inherits config.features.proactive; after first save,
runtime_settings.json is authoritative.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Intensity profiles (constant, not persisted) ──────────────

INTENSITY_PROFILES: dict[str, dict[str, int]] = {
    "quiet":     {"daily_trigger_cap": 2,  "optional_after_minutes": 360, "required_after_minutes": 480},
    "daily":     {"daily_trigger_cap": 5,  "optional_after_minutes": 120, "required_after_minutes": 180},
    "caring":    {"daily_trigger_cap": 10, "optional_after_minutes": 30,  "required_after_minutes": 60},
    "companion": {"daily_trigger_cap": 20, "optional_after_minutes": 5,   "required_after_minutes": 20},
}

VALID_INTENSITIES = frozenset(INTENSITY_PROFILES.keys())
INTENSITY_LABELS: dict[str, str] = {
    "quiet": "安静", "daily": "日常", "caring": "关心", "companion": "陪伴",
}

# ── Data structures ───────────────────────────────────────────

@dataclass
class ProactiveSettings:
    """V1.6-A proactive message settings.

    V1.6-B will add `scheduled` when implemented.
    """
    enabled: bool = False
    autonomous_enabled: bool = False
    autonomous_intensity: str = "daily"
    quiet_hours_enabled: bool = True
    quiet_hours_start: str = "00:00"
    quiet_hours_end: str = "08:00"
    daily_active_message_cap: int = 50

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ProactiveSettings":
        return cls(
            enabled=bool(d.get("enabled", False)),
            autonomous_enabled=bool(d.get("autonomous_enabled", False)),
            autonomous_intensity=str(d.get("autonomous_intensity", "daily")),
            quiet_hours_enabled=bool(d.get("quiet_hours_enabled", True)),
            quiet_hours_start=str(d.get("quiet_hours_start", "00:00")),
            quiet_hours_end=str(d.get("quiet_hours_end", "08:00")),
            daily_active_message_cap=int(d.get("daily_active_message_cap", 50)),
        )


# ── Validation ────────────────────────────────────────────────

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

def _validate_settings(d: dict[str, Any]) -> None:
    """Validate settings dict. Raises ValueError with field path on failure."""
    # enabled: must be bool
    if not isinstance(d.get("enabled"), bool):
        raise ValueError("enabled must be a boolean")

    # autonomous_enabled: must be bool
    if not isinstance(d.get("autonomous_enabled"), bool):
        raise ValueError("autonomous_enabled must be a boolean")

    # autonomous_intensity: must be valid
    intensity = str(d.get("autonomous_intensity", ""))
    if intensity not in VALID_INTENSITIES:
        raise ValueError(
            f"autonomous_intensity must be one of {sorted(VALID_INTENSITIES)}, got {intensity!r}"
        )

    # quiet_hours_enabled: must be bool
    if not isinstance(d.get("quiet_hours_enabled"), bool):
        raise ValueError("quiet_hours_enabled must be a boolean")

    # quiet_hours times: must be HH:mm
    for field in ("quiet_hours_start", "quiet_hours_end"):
        val = str(d.get(field, ""))
        if not _TIME_RE.match(val):
            raise ValueError(f"{field} must be HH:mm format, got {val!r}")

    # daily_active_message_cap: must be int in [1, 200]
    cap = d.get("daily_active_message_cap")
    if not isinstance(cap, (int, float)):
        raise ValueError("daily_active_message_cap must be a number")
    cap = int(cap)
    if cap < 1 or cap > 200:
        raise ValueError(f"daily_active_message_cap must be 1-200, got {cap}")


# ── Persistence ───────────────────────────────────────────────

_RUNTIME_SETTINGS_PATH: Path | None = None


def set_runtime_settings_path(path: str | Path) -> None:
    """Set the path to runtime_settings.json (called from main.py)."""
    global _RUNTIME_SETTINGS_PATH
    _RUNTIME_SETTINGS_PATH = Path(path)


def _read_runtime_settings() -> dict | None:
    """Read full runtime_settings.json. Returns None if no proactive key exists."""
    if _RUNTIME_SETTINGS_PATH is None:
        return None
    try:
        if not _RUNTIME_SETTINGS_PATH.exists():
            return None
        with open(_RUNTIME_SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        logger.warning("Failed to read runtime_settings.json", exc_info=True)
        return None
    if "proactive" not in data:
        return None
    return data["proactive"]


def _write_runtime_settings(settings_dict: dict[str, Any]) -> None:
    """Write proactive key into runtime_settings.json, preserving other keys."""
    if _RUNTIME_SETTINGS_PATH is None:
        raise RuntimeError("runtime_settings_path not set")
    _RUNTIME_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if _RUNTIME_SETTINGS_PATH.exists():
        try:
            with open(_RUNTIME_SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    data["proactive"] = settings_dict
    with open(_RUNTIME_SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_proactive_settings(config: dict[str, Any]) -> ProactiveSettings:
    """Load proactive settings.

    First call (no saved data): inherits config.features.proactive.
    After save: runtime_settings.json is authoritative.
    """
    saved = _read_runtime_settings()
    if saved is not None:
        _validate_settings(saved)
        return ProactiveSettings.from_dict(saved)

    # First time: inherit from config.yaml
    cfg_enabled = bool((config.get("features") or {}).get("proactive", False))
    settings = ProactiveSettings(
        enabled=cfg_enabled,
        autonomous_enabled=cfg_enabled,
        autonomous_intensity="daily",
        quiet_hours_enabled=True,
        quiet_hours_start="00:00",
        quiet_hours_end="08:00",
        daily_active_message_cap=50,
    )
    return settings


def save_proactive_settings(settings: ProactiveSettings) -> None:
    """Save proactive settings to runtime_settings.json after validation."""
    d = settings.to_dict()
    _validate_settings(d)
    _write_runtime_settings(d)
    logger.info("ProactiveSettings saved: enabled=%s intensity=%s cap=%d",
                 d["enabled"], d["autonomous_intensity"], d["daily_active_message_cap"])


def get_intensity_profile(intensity: str) -> dict[str, int]:
    """Return the intensity profile dict for the given intensity key.
    Falls back to 'daily' if intensity is invalid.
    """
    return INTENSITY_PROFILES.get(intensity, INTENSITY_PROFILES["daily"])
