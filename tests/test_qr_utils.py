"""Tests for bridge.qr_utils."""

import pytest
from bridge.qr_utils import normalize_qr_payload, make_qr_svg, make_qr_png


def test_make_qr_svg_empty():
    assert make_qr_svg("") == ""


def test_make_qr_svg_valid():
    svg = make_qr_svg("https://example.com/qr/test")
    # Should produce valid SVG with namespace
    assert svg != ""
    assert "<svg" in svg.lower() or svg == ""


def test_make_qr_png_empty():
    assert make_qr_png("") == b""


def test_make_qr_png_valid():
    png = make_qr_png("https://example.com/qr/test")
    assert len(png) > 0
    # PNG magic bytes
    assert png[:4] == b"\x89PNG"


def test_normalize_qr_payload_basic():
    raw = {
        "qrcode": "qr123",
        "status": "wait",
    }
    result = normalize_qr_payload(raw)
    assert result["qrcode"] == "qr123"
    assert result["qrcode_url"] == "qr123"
    assert result["status"] == "wait"
    assert "raw_keys" in result


def test_normalize_qr_payload_with_image():
    raw = {
        "qrcode_img_content": "https://img.example.com/qr.png",
        "status": "confirmed",
    }
    result = normalize_qr_payload(raw)
    assert result["qrcode_img"] == "https://img.example.com/qr.png"
    assert result["qrcode_url"] == "https://img.example.com/qr.png"


def test_normalize_qr_payload_can_render_qr():
    raw = {
        "qrcode": "https://example.com/qr",
        "status": "wait",
    }
    result = normalize_qr_payload(raw)
    # With a valid URL, SVG should be generated
    assert result["can_render_qr"] is True
    assert result["qrcode_svg"] != ""


def test_normalize_qr_payload_cannot_render_empty():
    raw = {"status": "wait"}
    result = normalize_qr_payload(raw)
    assert result["can_render_qr"] is False
    assert result["qrcode_svg"] == ""


def test_normalize_qr_payload_raw_keys():
    raw = {"a": 1, "b": 2, "status": "wait"}
    result = normalize_qr_payload(raw)
    assert result["raw_keys"] == sorted(raw.keys())
