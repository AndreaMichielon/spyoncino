import logging
import shutil
from pathlib import Path
from typing import List, Optional

import cv2
import numpy as np
import torch
from ultralytics import YOLO

_log = logging.getLogger(__name__)

# Official yolov8n.pt is ~6 MiB; reject empty/partial GitHub failures (504, etc.).
_MIN_PT_BYTES = 500_000

_attempt_download_patched = False


def _weights_file_ok(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size >= _MIN_PT_BYTES
    except OSError:
        return False


def _ultralytics_cached_weights(name: str) -> Optional[Path]:
    """Path in Ultralytics ``weights_dir`` if present and valid."""
    try:
        from ultralytics.utils import SETTINGS

        wdir = SETTINGS.get("weights_dir")
        if not wdir:
            return None
        c = Path(wdir) / name
        return c if _weights_file_ok(c) else None
    except Exception:
        return None


def _patch_attempt_download_for_existing_files() -> None:
    """
    ``YOLO(path)`` calls ``torch_safe_load`` → ``attempt_download_asset`` again.
    That can re-hit GitHub and return a path to a missing file. Short-circuit when
    the checkpoint already exists and passes size checks.
    """
    global _attempt_download_patched
    if _attempt_download_patched:
        return
    import ultralytics.utils.downloads as udd

    _orig = udd.attempt_download_asset

    def _wrapped(file, *args, **kwargs):
        try:
            p = Path(str(file)).resolve()
        except OSError:
            p = Path(str(file))
        if _weights_file_ok(p):
            return str(p)
        return _orig(file, *args, **kwargs)

    udd.attempt_download_asset = _wrapped
    _attempt_download_patched = True


def ensure_detector_weights_file(weights: str) -> str:
    """
    Resolve ``weights`` to a valid on-disk ``.pt`` file (size-checked).

    Order: existing valid file → Ultralytics cache dir → cwd copy → hub download.
    Truncated/empty files are removed before retrying download.
    """
    path = Path(weights)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    else:
        path = path.resolve()

    if _weights_file_ok(path):
        return str(path)

    if path.is_file():
        try:
            sz = path.stat().st_size
            path.unlink()
            _log.warning(
                "Removed invalid/truncated weights file (%s bytes, need >= %s): %s",
                sz,
                _MIN_PT_BYTES,
                path,
            )
        except OSError:
            pass

    path.parent.mkdir(parents=True, exist_ok=True)
    name = path.name

    cached = _ultralytics_cached_weights(name)
    if cached is not None:
        try:
            shutil.copy2(cached, path)
            _log.info("Copied weights from Ultralytics cache %s -> %s", cached, path)
        except OSError as e:
            _log.warning("Could not copy from Ultralytics cache: %s", e)
        if _weights_file_ok(path):
            return str(path)

    legacy = Path.cwd() / name
    if _weights_file_ok(legacy) and legacy.resolve() != path:
        try:
            shutil.copy2(legacy, path)
            _log.info("Copied weights %s -> %s", legacy, path)
        except OSError as e:
            _log.warning("Could not copy weights from %s: %s", legacy, e)
        if _weights_file_ok(path):
            return str(path)

    try:
        from ultralytics.utils.downloads import attempt_download_asset
    except ImportError as e:  # pragma: no cover
        raise ImportError(
            "ultralytics is required to download detector weights; "
            "install dependencies or place a valid .pt file at "
            f"{path}"
        ) from e

    out = attempt_download_asset(path)
    out_path = Path(out).resolve()
    if not _weights_file_ok(out_path):
        raise FileNotFoundError(
            f"Detector weights missing or too small after download (min {_MIN_PT_BYTES} bytes): "
            f"{out_path}. Check network, or copy yolov8n.pt manually to {path}"
        )
    _log.info("Detector weights ready at %s", out_path)
    return str(out_path)


def _draw_label_pill(
    overlay: np.ndarray,
    x1: int,
    y_top: int,
    text: str,
    accent_bgr: tuple,
    scale: float,
) -> None:
    """Dark pill behind text; text in white. ``y_top`` is top row of the pill."""
    font = cv2.FONT_HERSHEY_DUPLEX
    thickness = max(1, int(round(scale)))
    (tw, th), baseline = cv2.getTextSize(text, font, scale, thickness)
    pad_x, pad_y = max(6, int(8 * scale)), max(3, int(4 * scale))
    px1 = x1
    py1 = y_top
    px2 = px1 + tw + pad_x * 2
    py2 = py1 + th + pad_y * 2
    h, w = overlay.shape[:2]
    px2 = min(px2, w - 1)
    py2 = min(py2, h - 1)
    if px2 <= px1 or py2 <= py1:
        return
    bg = (24, 38, 32)
    cv2.rectangle(overlay, (px1, py1), (px2, py2), bg, -1, lineType=cv2.LINE_AA)
    cv2.rectangle(overlay, (px1, py1), (px2, py2), accent_bgr, 1, lineType=cv2.LINE_AA)
    tx = px1 + pad_x
    ty = py1 + th + pad_y - baseline // 2
    cv2.putText(
        overlay,
        text,
        (tx, ty),
        font,
        scale,
        (250, 252, 250),
        thickness,
        lineType=cv2.LINE_AA,
    )


class ObjectDetection:
    def __init__(
        self,
        weights: str = None,
        conf_threshold: float = 0.25,
        iou_threshold: float = 0.6,
        batch_size: int = 16,
        alarmed_classes: list = ["person"],
    ):
        self.weights = weights
        self.conf_threshold = conf_threshold
        self.iou_threshold = iou_threshold
        self.batch_size = batch_size
        self.alarmed_classes = alarmed_classes
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        if weights:
            resolved = ensure_detector_weights_file(weights)
            self.weights = resolved
            _patch_attempt_download_for_existing_files()
            self.model = YOLO(resolved)
        else:
            self.weights = weights
            self.model = YOLO(weights)

    def detect(self, frames: List[np.ndarray]):
        """
        Run YOLO on ``frames``. Returns one dict per frame (aligned with input order),
        each including ``frame_index`` into the original ``frames`` list for downstream
        champion-frame selection (face post-processing).
        """
        frames_with_labels: list = []
        alarmed = False
        for i in range(0, len(frames), self.batch_size):
            batch = frames[i : i + self.batch_size]
            results = self.model.predict(
                batch,
                conf=self.conf_threshold,
                iou=self.iou_threshold,
                verbose=False,
                batch=len(batch),
            )
            for j, result in enumerate(results):
                frame = batch[j]
                h, w = frame.shape[:2]
                empty_overlay = np.zeros((h, w, 3), dtype=np.uint8)
                frame_index = i + j
                name_map = self.model.names
                if result.boxes is None or len(result.boxes) == 0:
                    frames_with_labels.append(
                        {
                            "frame_index": frame_index,
                            "overlay": empty_overlay,
                            "boxes": np.empty((0, 4), dtype=np.float32),
                            "confidences": np.array([], dtype=np.float32),
                            "labels": [],
                            "is_alarmed": [],
                        }
                    )
                    continue
                boxes = result.boxes.xyxy.cpu().numpy()
                confidences = result.boxes.conf.cpu().numpy()
                classes = result.boxes.cls.cpu().numpy()
                labels = [name_map[int(cls_id)] for cls_id in classes]
                is_alarmed = [label in self.alarmed_classes for label in labels]
                alarmed = alarmed or any(is_alarmed)
                frames_with_labels.append(
                    {
                        "frame_index": frame_index,
                        "overlay": self._create_overlay(
                            frame, boxes, confidences, labels, is_alarmed
                        ),
                        "boxes": boxes,
                        "confidences": confidences,
                        "labels": labels,
                        "is_alarmed": is_alarmed,
                    }
                )
        return frames_with_labels, alarmed

    def _create_overlay(
        self,
        frame: np.ndarray,
        boxes: np.ndarray,
        confidences: np.ndarray,
        labels: list,
        is_alarmed: list,
    ) -> np.ndarray:
        h, w = frame.shape[:2]
        overlay = np.zeros((h, w, 3), dtype=np.uint8)
        scale = min(w / 1920.0, h / 1080.0)
        font_scale = max(0.45, 0.55 * scale)
        line_w = max(2, int(round(2.2 * scale)))

        color_normal = (100, 220, 100)
        color_alarmed = (60, 80, 255)
        for box, confidence, label, alarmed in zip(
            boxes, confidences, labels, is_alarmed
        ):
            x1, y1, x2, y2 = [int(round(v)) for v in box]
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(w - 1, x2), min(h - 1, y2)
            if x2 <= x1 or y2 <= y1:
                continue

            accent = color_alarmed if alarmed else color_normal
            outline_dim = (18, 32, 22) if not alarmed else (24, 36, 90)

            # Strokes only (full-area fill would replace pixels in GIF blend and hide subjects).
            cv2.rectangle(
                overlay,
                (x1, y1),
                (x2, y2),
                outline_dim,
                line_w + 1,
                lineType=cv2.LINE_AA,
            )
            cv2.rectangle(
                overlay, (x1, y1), (x2, y2), accent, line_w, lineType=cv2.LINE_AA
            )

            raw_lbl = str(label) if label is not None else "?"
            if len(raw_lbl) > 22:
                raw_lbl = raw_lbl[:20] + "…"
            text = f"{raw_lbl}  {float(confidence):.0%}"
            label_y1 = y1 - max(8, int(6 * scale))
            if label_y1 < 2:
                label_y1 = y2 + max(4, int(4 * scale))

            _draw_label_pill(overlay, x1, label_y1, text, accent, font_scale)

        return overlay
