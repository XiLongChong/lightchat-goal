from __future__ import annotations

import os

import pytest


def test_require_chat_auth_disabled_does_nothing():
    from main import _require_chat_auth

    class Request:
        headers = {}

    _require_chat_auth(Request(), {
        "chat_auth_enabled": False,
        "chat_api_key_env": "CHAT_API_KEY",
    })


def test_require_chat_auth_no_env_key_raises_503(monkeypatch):
    from fastapi import HTTPException
    from main import _require_chat_auth

    monkeypatch.delenv("CHAT_API_KEY", raising=False)

    class Request:
        headers = {}

    with pytest.raises(HTTPException) as exc:
        _require_chat_auth(Request(), {
            "chat_auth_enabled": True,
            "chat_api_key_env": "CHAT_API_KEY",
        })
    assert exc.value.status_code == 503


def test_require_chat_auth_correct_key_passes(monkeypatch):
    from main import _require_chat_auth

    monkeypatch.setenv("CHAT_API_KEY", "secret")

    class Request:
        headers = {"authorization": "Bearer secret"}

    _require_chat_auth(Request(), {
        "chat_auth_enabled": True,
        "chat_api_key_env": "CHAT_API_KEY",
    })


