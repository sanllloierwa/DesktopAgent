"""Best-effort diagnostic bundles for vision MCP calls."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from loguru import logger

from src.utils.config import AppConfig


_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        default=str,
    ).encode("utf-8")


def _artifact_root(config: AppConfig) -> Path:
    configured = config.vision.artifact_dir.strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(tempfile.gettempdir()) / "desktop-agent" / "vision-mcp"


def _image_suffix(media_type: str) -> str:
    return {
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/bmp": ".bmp",
    }.get(media_type.lower(), ".png")


def _prepare_request(arguments: dict[str, Any]) -> tuple[dict[str, Any], bytes | None, str | None]:
    """Remove the large base64 field from JSON and decode it as an image entry."""
    request = dict(arguments)
    encoded = request.pop("image_base64", None)
    if not isinstance(encoded, str):
        return request, None, None

    request["image_base64"] = {
        "stored_separately": True,
        "encoded_characters": len(encoded),
    }
    try:
        return request, base64.b64decode(encoded, validate=True), None
    except (binascii.Error, ValueError) as exc:
        return request, None, f"Could not decode image_base64: {exc}"


def _prune_old_artifacts(root: Path, retention: int, keep: Path) -> None:
    if retention <= 0:
        return
    archives = sorted(
        root.glob("*.zip"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in archives[retention:]:
        if path != keep:
            try:
                path.unlink()
            except OSError as exc:
                logger.warning(f"Could not prune old vision artifact {path}: {exc}")


def write_mcp_artifact(
    *,
    tool_name: str,
    arguments: dict[str, Any],
    config: AppConfig,
    response: dict[str, Any] | None = None,
    error: str | None = None,
    duration_ms: float = 0.0,
) -> Path | None:
    """Write one self-contained ZIP bundle without affecting the MCP call outcome."""
    if not config.vision.artifact_output_enabled:
        return None

    try:
        root = _artifact_root(config)
        root.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        safe_tool = _SAFE_NAME.sub("_", tool_name).strip("_") or "mcp_tool"
        filename = f"{now.strftime('%Y%m%dT%H%M%S.%fZ')}_{safe_tool}_{uuid4().hex[:8]}.zip"
        final_path = root / filename
        temporary_path = root / f".{filename}.tmp"

        request, image_bytes, image_error = _prepare_request(arguments)
        media_type = str(arguments.get("media_type", "image/png"))
        image_entry = f"screenshot{_image_suffix(media_type)}" if image_bytes else None
        manifest = {
            "format_version": 1,
            "created_at": now.isoformat(),
            "pid": os.getpid(),
            "tool_name": tool_name,
            "success": error is None,
            "duration_ms": round(float(duration_ms), 3),
            "transport": config.vision.transport,
            "provider": config.vision.provider,
            "model": config.vision.model,
            "request_entry": "request.json",
            "result_entry": "error.json" if error is not None else "response.json",
            "screenshot_entry": image_entry,
            "screenshot_error": image_error,
        }

        with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", _json_bytes(manifest))
            archive.writestr("request.json", _json_bytes(request))
            if error is not None:
                archive.writestr("error.json", _json_bytes({"error": error}))
            else:
                archive.writestr("response.json", _json_bytes(response or {}))
            if image_bytes is not None and image_entry is not None:
                archive.writestr(image_entry, image_bytes)

        temporary_path.replace(final_path)
        _prune_old_artifacts(root, config.vision.artifact_retention, final_path)
        return final_path
    except Exception as exc:
        logger.warning(f"Could not write vision MCP artifact: {exc}")
        return None
