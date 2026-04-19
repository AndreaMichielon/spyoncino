"""
On-disk layout for persisted notification and API media.

Paths mirror the index: media/<camera_id>/<YYYY-MM-DD>/<stage>_<ts>.<ext>
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


_SAFE_CAM = re.compile(r"[^a-zA-Z0-9._-]+")


def _safe_camera_segment(camera_id: str) -> str:
    s = _SAFE_CAM.sub("_", (camera_id or "unknown").strip()) or "unknown"
    return s[:128]


class MediaStore:
    """Resolves paths under a single media root; does not perform DB I/O."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def new_artifact_path(self, camera_id: str, stage: str, kind: str) -> Path:
        """
        Allocate a new path for an artifact. Creates parent directories.

        kind: logical type — gif, mp4, jpeg (jpg), etc.
        """
        self.ensure_root()
        ext_map = {
            "gif": ".gif",
            "mp4": ".mp4",
            "jpeg": ".jpg",
            "jpg": ".jpg",
            "avi": ".avi",
        }
        ext = ext_map.get(kind.lower().strip(), f".{kind.lower().strip().lstrip('.')}")
        stage_safe = _SAFE_CAM.sub("_", (stage or "unknown").strip())[:64] or "unknown"
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        cam = _safe_camera_segment(camera_id)
        subdir = self.root / cam / day
        subdir.mkdir(parents=True, exist_ok=True)
        return subdir / f"{stage_safe}_{ts}{ext}"

    def path_relative_to_root(self, absolute: Path) -> Optional[str]:
        try:
            return str(Path(absolute).resolve().relative_to(self.root))
        except ValueError:
            return None

    def resolve_relative(self, path_rel: str) -> Path:
        rel = Path(path_rel)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError("Invalid media path")
        return (self.root / rel).resolve()
