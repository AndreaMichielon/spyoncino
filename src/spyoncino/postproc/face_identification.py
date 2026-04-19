"""
Face identification: champion frame, DeepFace detect + represent, gallery match, pending unknowns.

Requires optional dependency ``spyoncino[face]`` (``deepface``). When ``enabled`` is false, or
DeepFace is missing, ``identify`` returns a safe no-match without importing heavy backends.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

# Combined champion score uses: max_person_conf * sqrt(largest_person_area_px).
# Documented constant for integration spec (docs/face-recognition.md).
_COMBINED_AREA_SQRT_WEIGHT = 1.0

_MODEL_NAME_ALIASES = {
    "facenet": "Facenet",
    "vggface": "VGG-Face",
    "vgg-face": "VGG-Face",
    "openface": "OpenFace",
    "deepface": "DeepFace",
    "deepid": "DeepID",
    "arcface": "ArcFace",
    "sface": "SFace",
    "ghostfacenet": "GhostFaceNet",
}


def _normalize_model_name(name: str) -> str:
    key = (name or "").strip().lower()
    return _MODEL_NAME_ALIASES.get(key, (name or "").strip() or "Facenet")


def _person_alarm_indices(det_frame: Dict[str, Any]) -> List[int]:
    labels = det_frame.get("labels") or []
    is_alarmed = det_frame.get("is_alarmed") or []
    out: List[int] = []
    for idx, lab in enumerate(labels):
        alarm = is_alarmed[idx] if idx < len(is_alarmed) else False
        if alarm and str(lab).lower() == "person":
            out.append(idx)
    return out


def score_frame_for_champion(det_frame: Dict[str, Any], policy: str) -> Optional[float]:
    """
    Score one detection frame for champion selection among person-alarmed boxes only.
    Returns None if this frame has no alarmed person boxes.
    """
    idxs = _person_alarm_indices(det_frame)
    if not idxs:
        return None
    boxes = det_frame.get("boxes")
    confs = det_frame.get("confidences")
    if boxes is None or confs is None:
        return None
    max_conf = 0.0
    max_area = 0.0
    for k in idxs:
        if k >= len(boxes):
            continue
        x1, y1, x2, y2 = (float(t) for t in boxes[k])
        area = max(0.0, (x2 - x1) * (y2 - y1))
        c = float(confs[k]) if k < len(confs) else 0.0
        max_conf = max(max_conf, c)
        max_area = max(max_area, area)
    pol = (policy or "combined").strip().lower()
    if pol == "confidence":
        return max_conf
    if pol == "area":
        return max_area
    # combined (default)
    return float(max_conf * _COMBINED_AREA_SQRT_WEIGHT * (max_area**0.5))


def _detection_for_frame_index(
    detection_frames: List[Any],
    frame_index: int,
) -> Optional[Dict[str, Any]]:
    for det in detection_frames:
        if not isinstance(det, dict):
            continue
        try:
            if int(det.get("frame_index", -1)) == int(frame_index):
                return det
        except (TypeError, ValueError):
            continue
    return None


def _largest_person_crop_bgr(
    frame: np.ndarray,
    det_frame: Dict[str, Any],
    margin: float = 0.15,
) -> Optional[np.ndarray]:
    """
    Crop the largest (by area) person bbox from ``det_frame`` on ``frame``.
    Used when full-frame face detection finds nothing — typical for small/distant faces.
    """
    idxs = _person_alarm_indices(det_frame)
    if not idxs:
        labels = det_frame.get("labels") or []
        idxs = [i for i, lab in enumerate(labels) if str(lab).lower() == "person"]
    if not idxs:
        return None
    boxes = det_frame.get("boxes")
    if boxes is None or len(boxes) == 0:
        return None
    best_i = None
    best_area = -1.0
    for k in idxs:
        if k >= len(boxes):
            continue
        x1, y1, x2, y2 = (float(t) for t in boxes[k])
        area = max(0.0, (x2 - x1) * (y2 - y1))
        if area > best_area:
            best_area = area
            best_i = k
    if best_i is None:
        return None
    x1, y1, x2, y2 = (float(t) for t in boxes[best_i])
    h, w = frame.shape[:2]
    bw = max(1.0, x2 - x1)
    bh = max(1.0, y2 - y1)
    mx = int(bw * margin)
    my = int(bh * margin)
    xi1 = max(0, int(x1) - mx)
    yi1 = max(0, int(y1) - my)
    xi2 = min(w, int(x2) + mx)
    yi2 = min(h, int(y2) + my)
    if xi2 <= xi1 + 16 or yi2 <= yi1 + 16:
        return None
    crop = frame[yi1:yi2, xi1:xi2].copy()
    return crop if crop.size > 0 else None


def pick_champion_frame_index(
    detection_frames: List[Dict[str, Any]],
    policy: str,
) -> Optional[int]:
    """Index into ``record_frames`` / detection_frames ``frame_index`` of the champion, or None."""
    best_idx: Optional[int] = None
    best_score = float("-inf")
    for det in detection_frames:
        if not isinstance(det, dict):
            continue
        sc = score_frame_for_champion(det, policy)
        if sc is None:
            continue
        fi = det.get("frame_index")
        if fi is None:
            continue
        try:
            fi_int = int(fi)
        except (TypeError, ValueError):
            continue
        if sc > best_score:
            best_score = sc
            best_idx = fi_int
    return best_idx


def _vec_fingerprint(vec: Any) -> str:
    """Short stable hash for cooldown dedupe of unknown faces."""
    try:
        arr = np.asarray(vec, dtype=np.float64).ravel()[:32]
        raw = arr.tobytes()
    except Exception:
        raw = str(vec).encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()[:16]


class FaceIdentification:
    """
    Post-process buffered frames after a person alarm: pick a champion frame, run DeepFace,
    match against a folder gallery (DeepFace layout) and optional SQLite pending rows.
    """

    def __init__(
        self,
        enabled: bool = False,
        gallery_path: str = "data/face_gallery",
        detector_backend: str = "opencv",
        model_name: str = "Facenet",
        align: bool = True,
        distance_metric: str = "cosine",
        match_threshold: float = 0.35,
        champion_frame_policy: str = "combined",
        recognition_cooldown_seconds_per_identity: float = 600.0,
        unknown_prompt_cooldown_seconds: float = 120.0,
        pending_ttl_days: int = 14,
        max_exemplars_per_identity: int = 30,
        **kwargs: Any,
    ) -> None:
        _ = kwargs
        self.enabled = bool(enabled)
        self.gallery_path = str(gallery_path or "data/face_gallery").strip()
        self.detector_backend = str(detector_backend or "opencv").strip()
        self.model_name = _normalize_model_name(str(model_name or "Facenet"))
        self.align = bool(align)
        self.distance_metric = str(distance_metric or "cosine").strip()
        self.match_threshold = float(match_threshold)
        self.champion_frame_policy = str(champion_frame_policy or "combined").strip()
        self.recognition_cooldown_s = float(recognition_cooldown_seconds_per_identity)
        self.unknown_cooldown_s = float(unknown_prompt_cooldown_seconds)
        self.pending_ttl_days = max(1, int(pending_ttl_days))
        self.max_exemplars = max(1, int(max_exemplars_per_identity))

        self._log = logging.getLogger(self.__class__.__name__)
        self._last_known_mono: Dict[str, float] = {}
        self._last_unknown_mono: Dict[str, float] = {}
        self._deepface_warned = False

    def _try_deepface(self) -> Tuple[Any, Any]:
        try:
            from deepface import DeepFace  # type: ignore[import-untyped]
        except Exception as e:  # pragma: no cover - optional dependency
            if not self._deepface_warned:
                hint = "pip install spyoncino[face]"
                msg = str(e).lower()
                if "tf-keras" in msg or "tf_keras" in msg:
                    hint = 'pip install tf-keras   # required with TensorFlow 2.16+; or: pip install -e ".[face]"'
                self._log.warning(
                    "DeepFace import failed (%s). Fix: %s",
                    e,
                    hint,
                )
                self._deepface_warned = True
            raise
        return DeepFace, None

    def _cooldown_ok(
        self, store: Dict[str, float], key: str, seconds: float, now: float
    ) -> bool:
        if seconds <= 0:
            return True
        last = store.get(key)
        if last is None or (now - last) >= seconds:
            store[key] = now
            return True
        return False

    def _extract_faces(
        self,
        DeepFace: Any,
        frame: np.ndarray,
        person_crop: Optional[np.ndarray],
    ) -> List[Any]:
        """Detect faces on champion frame, then on person crop if the full frame is empty."""
        backend = self.detector_backend
        last_exc: Optional[Exception] = None
        for tag, img in (("full", frame), ("person_crop", person_crop)):
            if img is None or not hasattr(img, "size") or img.size == 0:
                continue
            try:
                out = DeepFace.extract_faces(
                    img_path=img,
                    detector_backend=backend,
                    enforce_detection=False,
                    align=self.align,
                )
                faces = list(out or [])
                if faces:
                    if tag != "full":
                        self._log.info(
                            "extract_faces: source=%s faces=%s (person crop)",
                            tag,
                            len(faces),
                        )
                    return faces
            except Exception as e:
                last_exc = e
                self._log.debug("extract_faces skip source=%s: %s", tag, e)

        if last_exc is not None:
            self._log.warning(
                "DeepFace.extract_faces failed (last: %s). Check OpenCV; "
                "or set detector_backend to another DeepFace backend in recipe.",
                last_exc,
            )
        return []

    def identify(
        self,
        record_frames: List[Any],
        detection_frames: List[Any],
        camera_id: Optional[str] = None,
        memory_manager: Any = None,
        media_store: Any = None,
    ) -> Tuple[bool, Any]:
        """
        Args:
            record_frames: BGR numpy arrays (same order as detection pipeline).
            detection_frames: Per-frame dicts from object detection (includes ``frame_index``).
            camera_id: Camera id for pending rows and logging.
            memory_manager: Optional ``MemoryManager`` for ``pending_faces`` / identities.
            media_store: Optional ``MediaStore`` to persist unknown face crops.

        Returns:
            ``(face_alarmed, face_result)`` — ``face_result`` matches the integration contract
            (champion_frame_index, champion_policy, faces[] with hints and paths).
        """
        cam = (camera_id or "unknown").strip() or "unknown"
        if not self.enabled:
            return False, None

        if not record_frames or not detection_frames:
            return False, None

        champion_idx = pick_champion_frame_index(
            [f for f in detection_frames if isinstance(f, dict)],
            self.champion_frame_policy,
        )
        if (
            champion_idx is None
            or champion_idx < 0
            or champion_idx >= len(record_frames)
        ):
            return False, {
                "champion_frame_index": -1,
                "champion_policy": self.champion_frame_policy,
                "faces": [],
                "camera_id": cam,
                "skipped": "no_person_alarm_frame",
            }

        frame = record_frames[champion_idx]
        if frame is None or not hasattr(frame, "shape"):
            return False, {
                "champion_frame_index": champion_idx,
                "champion_policy": self.champion_frame_policy,
                "faces": [],
                "camera_id": cam,
                "skipped": "invalid_frame",
            }

        try:
            DeepFace, _ = self._try_deepface()
        except Exception:
            return False, {
                "champion_frame_index": champion_idx,
                "champion_policy": self.champion_frame_policy,
                "faces": [],
                "camera_id": cam,
                "error": "deepface_unavailable",
            }

        gallery_root = Path(self.gallery_path)
        if not gallery_root.is_absolute():
            gallery_root = Path.cwd() / gallery_root
        gallery_root.mkdir(parents=True, exist_ok=True)

        now_mono = time.monotonic()

        det_champion = _detection_for_frame_index(detection_frames, champion_idx)
        person_crop: Optional[np.ndarray] = None
        if det_champion is not None:
            person_crop = _largest_person_crop_bgr(frame, det_champion)

        faces_objs = self._extract_faces(DeepFace, frame, person_crop)

        if not faces_objs:
            self._log.info(
                "Face pipeline: no faces on champion frame %s (detector=%s).",
                champion_idx,
                self.detector_backend,
            )

        face_rows: List[Dict[str, Any]] = []
        face_alarmed = False

        for slot, face_obj in enumerate(faces_objs or []):
            # DeepFace versions differ: dict with facial_area / numpy face / keys "face"
            region = None
            if isinstance(face_obj, dict):
                region = face_obj.get("facial_area") or face_obj.get("region")
                face_arr = face_obj.get("face")
            else:
                face_arr = face_obj
            bbox_face = None
            if isinstance(region, dict):
                try:
                    x, y, w, h = (
                        int(region["x"]),
                        int(region["y"]),
                        int(region["w"]),
                        int(region["h"]),
                    )
                    bbox_face = [x, y, x + w, y + h]
                except Exception:
                    bbox_face = None

            try:
                rep = DeepFace.represent(
                    img_path=face_arr if face_arr is not None else frame,
                    model_name=self.model_name,
                    detector_backend=self.detector_backend,
                    enforce_detection=False,
                    align=self.align,
                )
            except Exception as e:
                self._log.warning("DeepFace.represent failed for slot %s: %s", slot, e)
                continue

            if isinstance(rep, list) and rep:
                rep0 = rep[0]
            elif isinstance(rep, dict):
                rep0 = rep
            else:
                continue
            vec = rep0.get("embedding") if isinstance(rep0, dict) else None
            if vec is None:
                continue
            fp = _vec_fingerprint(vec)

            matched_identity_id: Optional[str] = None
            display_name: Optional[str] = None
            confidence: Optional[float] = None
            crop_path: Optional[str] = None
            pending_face_id: Optional[str] = None
            notification_hint = "suppressed_cooldown"

            # Match via DeepFace.find on a temp crop (folder gallery under gallery_root)
            match_path: Optional[Path] = None
            crop_bgr: Optional[np.ndarray] = None
            try:
                if face_arr is not None:
                    crop = np.asarray(face_arr)
                    if crop.dtype != np.uint8:
                        crop = (np.clip(crop, 0.0, 1.0) * 255.0).astype(np.uint8)
                    if crop.ndim == 3 and crop.shape[2] == 3:
                        crop_bgr = cv2.cvtColor(crop, cv2.COLOR_RGB2BGR)
                if crop_bgr is None and bbox_face is not None:
                    x1, y1, x2, y2 = (int(max(0, t)) for t in bbox_face)
                    crop_bgr = frame[y1:y2, x1:x2].copy()
                if crop_bgr is None or crop_bgr.size == 0:
                    crop_bgr = frame.copy()

                fd, tmp_name = tempfile.mkstemp(prefix="sp_face_", suffix=".jpg")
                os.close(fd)
                match_path = Path(tmp_name)
                cv2.imwrite(str(match_path), crop_bgr)
            except Exception as e:
                self._log.warning("Face crop for matching failed: %s", e)
                match_path = None

            best_identity: Optional[str] = None
            best_name: Optional[str] = None
            best_dist: Optional[float] = None

            has_gallery_faces = False
            try:
                for p in gallery_root.iterdir():
                    if p.is_dir() and not p.name.startswith("."):
                        has_gallery_faces = True
                        break
            except OSError:
                has_gallery_faces = False

            if match_path is not None and has_gallery_faces:
                try:
                    find_kw: Dict[str, Any] = {
                        "img_path": str(match_path),
                        "db_path": str(gallery_root),
                        "model_name": self.model_name,
                        "detector_backend": self.detector_backend,
                        "distance_metric": self.distance_metric,
                        "enforce_detection": False,
                        "align": self.align,
                        "silent": True,
                    }
                    try:
                        dfs = DeepFace.find(**find_kw, threshold=self.match_threshold)
                    except TypeError:
                        dfs = DeepFace.find(**find_kw)
                    if (
                        isinstance(dfs, list)
                        and dfs
                        and hasattr(dfs[0], "empty")
                        and not dfs[0].empty
                    ):
                        row0 = dfs[0].iloc[0]
                        # identity column name varies by version
                        for col in ("identity", "Identity"):
                            if col in dfs[0].columns:
                                ident = str(row0[col])
                                pth = Path(ident)
                                best_name = pth.parent.name
                                break
                        for col in ("distance", "Distance"):
                            if col in dfs[0].columns:
                                best_dist = float(row0[col])
                                break
                        if (
                            best_name
                            and best_dist is not None
                            and best_dist <= self.match_threshold
                        ):
                            # Resolve DB identity id from folder name if possible
                            best_identity = best_name
                            if memory_manager is not None and hasattr(
                                memory_manager, "get_identity_by_gallery_folder"
                            ):
                                row = memory_manager.get_identity_by_gallery_folder(
                                    best_name
                                )
                                if row:
                                    best_identity = str(row["id"])
                                    best_name = str(row["display_name"])
                except Exception as e:
                    self._log.info("DeepFace.find (no match or empty gallery): %s", e)

            try:
                if match_path is not None and match_path.exists():
                    match_path.unlink(missing_ok=True)
            except OSError:
                pass

            if best_identity and best_name and best_dist is not None:
                matched_identity_id = best_identity
                display_name = best_name
                confidence = float(best_dist)
                key = str(matched_identity_id)
                if self._cooldown_ok(
                    self._last_known_mono, key, self.recognition_cooldown_s, now_mono
                ):
                    notification_hint = "known_text"
                    face_alarmed = True
                else:
                    notification_hint = "suppressed_cooldown"
            else:
                # Unknown face — optional pending row + crop on disk
                pending_id = str(uuid.uuid4())
                rel: Optional[str] = None
                if media_store is not None and hasattr(
                    media_store, "new_artifact_path"
                ):
                    try:
                        out = media_store.new_artifact_path(cam, "face", "jpg")
                        ok = cv2.imwrite(
                            str(out), crop_bgr if crop_bgr is not None else frame
                        )
                        if ok:
                            rel = media_store.path_relative_to_root(out)
                            crop_path = str(out)
                    except Exception as e:
                        self._log.warning("Failed to write pending face crop: %s", e)

                if (
                    rel
                    and memory_manager is not None
                    and hasattr(memory_manager, "insert_pending_face")
                ):
                    try:
                        memory_manager.insert_pending_face(
                            pending_id=pending_id,
                            camera_id=cam,
                            path_rel=rel,
                            embedding_hash=fp,
                            champion_frame_index=champion_idx,
                            ttl_days=self.pending_ttl_days,
                        )
                        pending_face_id = pending_id
                    except Exception as e:
                        self._log.warning("insert_pending_face failed: %s", e)

                if self._cooldown_ok(
                    self._last_unknown_mono, fp, self.unknown_cooldown_s, now_mono
                ):
                    notification_hint = "unknown_prompt"
                    face_alarmed = True
                else:
                    notification_hint = "suppressed_cooldown"

            face_rows.append(
                {
                    "slot_index": slot,
                    "matched_identity_id": matched_identity_id,
                    "display_name": display_name,
                    "confidence": confidence,
                    "bbox_face": bbox_face,
                    "crop_path": crop_path,
                    "pending_face_id": pending_face_id,
                    "notification_hint": notification_hint,
                }
            )

        result: Dict[str, Any] = {
            "champion_frame_index": champion_idx,
            "champion_policy": self.champion_frame_policy,
            "faces": face_rows,
            "camera_id": cam,
            "model_name": self.model_name,
            "detector_backend": self.detector_backend,
            "distance_metric": self.distance_metric,
        }
        return bool(face_alarmed), result
