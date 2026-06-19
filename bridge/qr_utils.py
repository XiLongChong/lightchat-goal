"""QR code utilities for iLink deployment — pure functions, no FastAPI dependency."""

import io


def normalize_qr_payload(raw: dict) -> dict:
    """Normalize the iLink QR response for the admin frontend."""
    qrcode = (
        raw.get("qrcode")
        or raw.get("qr_code")
        or raw.get("code")
        or ""
    )
    image = (
        raw.get("qrcode_img_content")
        or raw.get("qrcode_img")
        or raw.get("qrcode_url")
        or raw.get("qr_code_url")
        or raw.get("url")
        or ""
    )
    qr_url = image or qrcode
    svg = make_qr_svg(qr_url)
    return {
        "qrcode": qrcode,
        "qrcode_content": qr_url,
        "qrcode_img": image,
        "qrcode_url": qr_url,
        "qrcode_svg": svg,
        "can_render_qr": bool(svg),
        "status": raw.get("status", "wait"),
        "raw_keys": sorted(raw.keys()),
    }


def make_qr_svg(content: str) -> str:
    """Render a QR code SVG for the deployment URL.

    iLink returns a WeChat liteapp URL, not an image. The browser needs an
    actual QR image to scan, matching emoRebot's Next implementation.
    """
    if not content:
        return ""
    try:
        import qrcode
        from qrcode.image.svg import SvgPathImage
    except Exception:
        return ""

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(content)
    qr.make(fit=True)
    img = qr.make_image(image_factory=SvgPathImage)
    return img.to_string(encoding="unicode")


def make_qr_png(content: str) -> bytes:
    """Render QR content to PNG bytes for browsers that cannot use raw SVG."""
    if not content:
        return b""
    import qrcode

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(content)
    qr.make(fit=True)
    img = qr.make_image()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
