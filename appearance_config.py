"""Shared validation for the cross-device dashboard appearance settings."""

from __future__ import annotations

import hashlib
import io
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError


DEFAULT_APPEARANCE: dict[str, Any] = {
    "version": 1,
    "theme": "apricot",
    "background": {"kind": "gradient", "intensity": 0.7},
    "glass": {"blur": 12, "opacity": 0.78},
    "font": {"display": "serif", "scale": 1.0},
    "effects": {"rain": {"mode": "off", "intensity": 0.35}},
}

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
ACCEPTED_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp"}


def _number(value: Any, default: float, low: float, high: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result or result in (float("inf"), float("-inf")):
        return default
    return max(low, min(high, result))


def normalize_appearance(raw: Any, available_asset_id: str = "") -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    background = source.get("background") if isinstance(source.get("background"), dict) else {}
    glass = source.get("glass") if isinstance(source.get("glass"), dict) else {}
    font = source.get("font") if isinstance(source.get("font"), dict) else {}
    effects = source.get("effects") if isinstance(source.get("effects"), dict) else {}
    rain = effects.get("rain") if isinstance(effects.get("rain"), dict) else {}

    kind = background.get("kind") if background.get("kind") in {"gradient", "upload", "none"} else "gradient"
    asset_id = str(background.get("assetId") or "")
    if kind == "upload" and (not available_asset_id or asset_id != available_asset_id):
        kind = "gradient"
    safe_background: dict[str, Any] = {"kind": kind}
    if available_asset_id and asset_id == available_asset_id:
        safe_background["assetId"] = asset_id
    # 背景浓度：渐变 / 照片上盖一层底色的反比，越低越淡
    safe_background["intensity"] = round(_number(background.get("intensity"), 0.7, 0.2, 1), 2)

    return {
        "version": 1,
        "theme": source.get("theme") if source.get("theme") in {"apricot", "sakura", "mist", "dusk"} else "apricot",
        "background": safe_background,
        "glass": {
            "blur": round(_number(glass.get("blur"), 12, 0, 30), 1),
            "opacity": round(_number(glass.get("opacity"), 0.78, 0.4, 1), 2),
        },
        "font": {
            "display": font.get("display") if font.get("display") in {"serif", "sans"} else "serif",
            "scale": round(_number(font.get("scale"), 1.0, 0.85, 1.3), 2),
        },
        "effects": {
            "rain": {
                "mode": rain.get("mode") if rain.get("mode") in {"off", "on", "weather"} else "off",
                "intensity": round(_number(rain.get("intensity"), 0.35, 0, 1), 2),
            },
        },
    }


def compress_background(data: bytes, declared_mime: str) -> tuple[str, str, bytes]:
    if declared_mime not in ACCEPTED_IMAGE_MIMES:
        raise ValueError("unsupported background image MIME")
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("background image must be 1 byte to 5 MB")
    try:
        with Image.open(io.BytesIO(data)) as opened:
            if opened.format not in {"JPEG", "PNG", "WEBP"}:
                raise ValueError("unsupported background image format")
            if opened.width * opened.height > 30_000_000:
                raise ValueError("background image dimensions are too large")
            image = ImageOps.exif_transpose(opened)
            image.thumbnail((1920, 1920), Image.Resampling.LANCZOS)
            if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                layer = Image.new("RGB", image.size, "#FCFAF8")
                layer.paste(image.convert("RGBA"), mask=image.convert("RGBA").getchannel("A"))
                image = layer
            else:
                image = image.convert("RGB")
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=84, optimize=True)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("invalid background image") from exc
    compressed = output.getvalue()
    return hashlib.sha256(compressed).hexdigest()[:24], "image/jpeg", compressed
