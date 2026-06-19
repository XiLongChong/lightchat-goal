"""Tests for runtime.config — configuration loading and runtime settings.

Covers:
  1. ${VAR:-default} env replacement — default used when env unset.
  2. ${VAR:-default} env replacement — env value used when set.
  3. ${VAR} (no default) → empty string when env missing.
  4. deep_update recursive merge — preserves uncovered fields.
  5. load_runtime_settings returns {} when file missing.
  6. load_runtime_settings returns {} on invalid JSON.
  7. save_runtime_settings writes UTF-8 JSON with ensure_ascii=False.
  8. RuntimeConfig.apply_updates deep-merges and persists.
  9. RuntimeConfig initial load merges runtime_settings into cfg.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from runtime.config import (
    RuntimeConfig,
    deep_update,
    env_replace,
    load_config,
    load_runtime_settings,
    save_runtime_settings,
)


# ---------------------------------------------------------------------------
# Fixture: writable temp directory (bypasses pytest tmp_path permission issue)
# ---------------------------------------------------------------------------


@pytest.fixture
def work_dir():
    """A writable temp directory that doesn't go through pytest's tmp_path."""
    d = tempfile.mkdtemp(prefix="goal_test_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# env_replace
# ---------------------------------------------------------------------------


def test_env_replace_uses_default_when_env_unset(monkeypatch):
    """${VAR:-default} → default when env var is unset."""
    monkeypatch.delenv("MY_TEST_VAR_UNSET", raising=False)
    result = env_replace("value is ${MY_TEST_VAR_UNSET:-fallback}")
    assert result == "value is fallback"


def test_env_replace_uses_env_when_set(monkeypatch):
    """${VAR:-default} → env value when env var is set."""
    monkeypatch.setenv("MY_TEST_VAR_SET", "from_env")
    result = env_replace("value is ${MY_TEST_VAR_SET:-fallback}")
    assert result == "value is from_env"


def test_env_replace_no_default_empty_when_missing(monkeypatch):
    """${VAR} (no :-default) → empty string when env var is missing."""
    monkeypatch.delenv("MY_MISSING_VAR", raising=False)
    result = env_replace("prefix${MY_MISSING_VAR}suffix")
    assert result == "prefixsuffix"


def test_env_replace_no_default_uses_env_when_set(monkeypatch):
    """${VAR} (no :-default) → env value when env var is set."""
    monkeypatch.setenv("MY_PRESENT_VAR", "present")
    result = env_replace("prefix${MY_PRESENT_VAR}suffix")
    assert result == "prefixpresentsuffix"


def test_env_replace_multiple_patterns(monkeypatch):
    """Multiple ${...} patterns in one string are all replaced."""
    monkeypatch.setenv("VAR_A", "alpha")
    monkeypatch.delenv("VAR_B", raising=False)
    result = env_replace("${VAR_A:-x} and ${VAR_B:-y}")
    assert result == "alpha and y"


def test_env_replace_no_patterns_returns_unchanged():
    """Strings without ${...} patterns are returned unchanged."""
    assert env_replace("plain text") == "plain text"


# ---------------------------------------------------------------------------
# deep_update
# ---------------------------------------------------------------------------


def test_deep_update_merges_nested_dicts():
    """Recursive merge: nested dict values are merged, not replaced."""
    base = {"a": {"b": 1, "c": 2}, "d": 3}
    updates = {"a": {"c": 20, "e": 30}}
    result = deep_update(base, updates)
    assert result is base  # in-place
    assert base == {"a": {"b": 1, "c": 20, "e": 30}, "d": 3}


def test_deep_update_preserves_uncovered_fields():
    """Fields not in updates are preserved."""
    base = {"x": 1, "y": {"z": 2, "w": 3}}
    updates = {"y": {"z": 99}}
    deep_update(base, updates)
    assert base == {"x": 1, "y": {"z": 99, "w": 3}}


def test_deep_update_replaces_non_dict_with_dict():
    """When base value is not a dict but update is, the update replaces."""
    base = {"a": 1}
    updates = {"a": {"b": 2}}
    deep_update(base, updates)
    assert base == {"a": {"b": 2}}


def test_deep_update_replaces_dict_with_non_dict():
    """When base value is a dict but update is not, the update replaces."""
    base = {"a": {"b": 1}}
    updates = {"a": 42}
    deep_update(base, updates)
    assert base == {"a": 42}


def test_deep_update_empty_updates():
    """Empty updates dict leaves base unchanged."""
    base = {"a": 1}
    deep_update(base, {})
    assert base == {"a": 1}


# ---------------------------------------------------------------------------
# load_runtime_settings
# ---------------------------------------------------------------------------


def test_load_runtime_settings_missing_file_returns_empty(work_dir):
    """When the settings file doesn't exist, returns {}."""
    path = work_dir / "nonexistent.json"
    assert load_runtime_settings(path) == {}


def test_load_runtime_settings_invalid_json_returns_empty(work_dir):
    """When the settings file has invalid JSON, returns {}."""
    path = work_dir / "bad.json"
    path.write_text("{invalid json!!!", encoding="utf-8")
    assert load_runtime_settings(path) == {}


def test_load_runtime_settings_valid_file(work_dir):
    """A valid JSON file is loaded correctly."""
    path = work_dir / "settings.json"
    data = {"wechat": {"message_delay": 1.5}, "proactive": {"enabled": True}}
    path.write_text(json.dumps(data), encoding="utf-8")
    result = load_runtime_settings(path)
    assert result == data


# ---------------------------------------------------------------------------
# save_runtime_settings
# ---------------------------------------------------------------------------


def test_save_runtime_settings_writes_utf8_json(work_dir):
    """save_runtime_settings writes UTF-8 JSON with ensure_ascii=False."""
    path = work_dir / "subdir" / "settings.json"
    data = {"name": "中文测试", "value": 42}
    save_runtime_settings(path, data)

    # File should exist and be readable as UTF-8
    raw = path.read_text(encoding="utf-8")
    assert "中文测试" in raw  # ensure_ascii=False → not escaped

    # Parsed content matches
    parsed = json.loads(raw)
    assert parsed == data


def test_save_runtime_settings_uses_indent_2(work_dir):
    """Output JSON uses indent=2 for readability."""
    path = work_dir / "settings.json"
    save_runtime_settings(path, {"a": 1})
    raw = path.read_text(encoding="utf-8")
    # indent=2 produces newlines + 2-space indentation
    assert '\n  "a"' in raw


def test_save_runtime_settings_creates_parent_dir(work_dir):
    """Parent directories are created if they don't exist."""
    path = work_dir / "deep" / "nested" / "dir" / "settings.json"
    save_runtime_settings(path, {"x": 1})
    assert path.exists()


# ---------------------------------------------------------------------------
# load_config (with env replacement)
# ---------------------------------------------------------------------------


def test_load_config_with_env_replacement(work_dir, monkeypatch):
    """load_config reads YAML and applies ${VAR:-default} substitution."""
    monkeypatch.setenv("TEST_CFG_VAL", "from_env")
    config_path = work_dir / "config.yaml"
    config_path.write_text(
        "model:\n  name: ${TEST_CFG_VAL:-default_name}\n  timeout: ${MISSING:-30}\n",
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg["model"]["name"] == "from_env"
    # YAML parses the substituted "30" as an integer
    assert cfg["model"]["timeout"] == 30


# ---------------------------------------------------------------------------
# RuntimeConfig
# ---------------------------------------------------------------------------


def test_runtime_config_loads_and_merges(work_dir, monkeypatch):
    """RuntimeConfig loads config.yaml and merges runtime_settings into cfg."""
    config_path = work_dir / "config.yaml"
    config_path.write_text(
        "wechat:\n  message_delay: 1.0\n  max_reply_delay: 3.0\n",
        encoding="utf-8",
    )
    settings_path = work_dir / "runtime_settings.json"
    settings_path.write_text(
        json.dumps({"wechat": {"message_delay": 2.0}}),
        encoding="utf-8",
    )

    rc = RuntimeConfig(config_path, settings_path)

    # runtime_settings overrides config.yaml
    assert rc.cfg["wechat"]["message_delay"] == 2.0
    # Uncovered field from config.yaml is preserved
    assert rc.cfg["wechat"]["max_reply_delay"] == 3.0
    # runtime_settings dict is accessible
    assert rc.runtime_settings == {"wechat": {"message_delay": 2.0}}


def test_runtime_config_no_settings_file(work_dir):
    """RuntimeConfig works when runtime_settings file doesn't exist."""
    config_path = work_dir / "config.yaml"
    config_path.write_text("key: value\n", encoding="utf-8")
    settings_path = work_dir / "nonexistent.json"

    rc = RuntimeConfig(config_path, settings_path)
    assert rc.cfg == {"key": "value"}
    assert rc.runtime_settings == {}


def test_runtime_config_apply_updates_merges_and_persists(work_dir):
    """apply_updates deep-merges into runtime_settings + cfg, and persists to file."""
    config_path = work_dir / "config.yaml"
    config_path.write_text(
        "wechat:\n  message_delay: 1.0\n  max_reply_delay: 3.0\n",
        encoding="utf-8",
    )
    settings_path = work_dir / "runtime_settings.json"
    settings_path.write_text(
        json.dumps({"wechat": {"message_delay": 2.0}}),
        encoding="utf-8",
    )

    rc = RuntimeConfig(config_path, settings_path)

    # Apply an update
    rc.apply_updates({"wechat": {"max_reply_delay": 5.0}, "proactive": {"enabled": True}})

    # cfg should reflect the merge
    assert rc.cfg["wechat"]["message_delay"] == 2.0  # from runtime_settings
    assert rc.cfg["wechat"]["max_reply_delay"] == 5.0  # from apply_updates
    assert rc.cfg["proactive"]["enabled"] is True

    # runtime_settings should be persisted
    saved = json.loads(settings_path.read_text(encoding="utf-8"))
    assert saved["wechat"]["message_delay"] == 2.0
    assert saved["wechat"]["max_reply_delay"] == 5.0
    assert saved["proactive"]["enabled"] is True


def test_runtime_config_apply_updates_returns_cfg(work_dir):
    """apply_updates returns the updated cfg dict."""
    config_path = work_dir / "config.yaml"
    config_path.write_text("a: 1\n", encoding="utf-8")
    settings_path = work_dir / "settings.json"

    rc = RuntimeConfig(config_path, settings_path)
    result = rc.apply_updates({"b": 2})
    assert result is rc.cfg
    assert result["a"] == 1
    assert result["b"] == 2


def test_runtime_config_cfg_and_runtime_settings_are_same_objects(work_dir):
    """Successive accesses to cfg/runtime_settings return the same dict objects."""
    config_path = work_dir / "config.yaml"
    config_path.write_text("a: 1\n", encoding="utf-8")
    settings_path = work_dir / "settings.json"

    rc = RuntimeConfig(config_path, settings_path)
    assert rc.cfg is rc.cfg  # same object across accesses
    assert rc.runtime_settings is rc.runtime_settings


# ---------------------------------------------------------------------------
# Integration: main.py wiring
# ---------------------------------------------------------------------------


def test_main_uses_runtime_config():
    """main.py creates a RuntimeConfig instance and exposes cfg/runtime_settings."""
    import main

    assert hasattr(main, "runtime_config")
    assert isinstance(main.runtime_config, RuntimeConfig)
    # cfg and runtime_settings are the same objects as runtime_config's
    assert main.cfg is main.runtime_config.cfg
    assert main.runtime_settings is main.runtime_config.runtime_settings


def test_main_configure_tools_called_at_module_level():
    """configure_tools(cfg) is called at module level (not moved to runtime/config.py)."""
    # Verify that runtime/config.py does NOT import configure_tools
    import runtime.config as rc_module

    assert not hasattr(rc_module, "configure_tools")

    # main.py should still import and call configure_tools
    import main

    assert hasattr(main, "configure_tools")
