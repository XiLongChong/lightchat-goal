"""Test JSON parser — Safeguard #1: parse fallback on invalid JSON."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.json_parser import safe_parse_json, parse_agent_output, extract_json


def test_extract_json_from_code_block():
    """Extract JSON from markdown code block."""
    raw = '```json\n{"actions": [], "done": true}\n```'
    result = extract_json(raw)
    assert result == '{"actions": [], "done": true}'


def test_extract_json_bare():
    """Extract bare JSON."""
    raw = '{"actions":[],"done":true}'
    result = extract_json(raw)
    assert result == raw


def test_extract_json_with_text_around():
    """Extract JSON surrounded by text."""
    raw = '好的，以下是回复：\n{"actions":[{"type":"message","text":"你好"}],"done":true}'
    result = extract_json(raw)
    assert result == '{"actions":[{"type":"message","text":"你好"}],"done":true}'


def test_safe_parse_valid_json():
    """Valid JSON parses correctly."""
    raw = '{"actions": [{"type": "message", "text": "hello"}], "done": true}'
    result = safe_parse_json(raw)
    assert result is not None
    assert result["done"] is True
    assert len(result["actions"]) == 1


def test_safe_parse_invalid_json_returns_none():
    """Invalid JSON returns None (caller handles fallback)."""
    raw = "这不是合法的JSON {{{"
    result = safe_parse_json(raw)
    assert result is None


def test_safe_parse_empty_string_returns_none():
    """Empty string returns None."""
    result = safe_parse_json("")
    assert result is None


def test_safe_parse_trailing_comma_fix():
    """Trailing comma before } is fixed."""
    raw = '{"actions": [{"type": "message", "text": "hi"},], "done": true,}'
    result = safe_parse_json(raw)
    assert result is not None
    assert result["done"] is True


def test_parse_agent_output_fallback():
    """parse_agent_output always returns valid structure, even on garbage."""
    result = parse_agent_output("不是JSON的输出文本")
    assert result["done"] is True
    assert len(result["actions"]) == 1
    assert result["actions"][0]["type"] == "message"
    assert "不是JSON的输出文本" in result["actions"][0]["text"]


def test_parse_agent_output_no_actions_field():
    """Missing actions field → fallback."""
    result = parse_agent_output('{"done": true}')
    assert result["done"] is True
    assert len(result["actions"]) == 1
    assert result["actions"][0]["type"] == "message"


def test_parse_agent_output_preserves_valid_json():
    """Valid JSON with long text is preserved as-is (truncation is Chat Agent's job)."""
    long_text = "啊" * 600
    raw = f'{{"actions":[{{"type":"message","text":"{long_text}"}}],"done":true}}'
    result = parse_agent_output(raw)
    # JSON parser preserves the content; truncation is Safeguard #2 in chat_agent.py
    assert result["done"] is True
    assert len(result["actions"]) == 1
    assert result["actions"][0]["text"] == long_text


if __name__ == "__main__":
    test_extract_json_from_code_block()
    test_extract_json_bare()
    test_extract_json_with_text_around()
    test_safe_parse_valid_json()
    test_safe_parse_invalid_json_returns_none()
    test_safe_parse_empty_string_returns_none()
    test_safe_parse_trailing_comma_fix()
    test_parse_agent_output_fallback()
    test_parse_agent_output_no_actions_field()
    test_parse_agent_output_preserves_valid_json()
    print("[OK] All JSON parser tests passed")
