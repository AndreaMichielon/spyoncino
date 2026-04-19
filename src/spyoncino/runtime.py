"""
Central control/status API for the live pipeline (SpyoncinoRuntime).

Intended to be injected into the FastAPI app only; other callers use HTTP (see roadmap).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import cv2

from .analytics import render_events_trend_jpeg
from .interface.memory_manager import (
    EventType,
    MemoryManager,
    ServiceStatus,
    SystemMetrics,
)
from .recipe_classes import normalize_notify_modes, resolve_recipe_class
from .recipe_paths import gallery_path_from_recipe

if TYPE_CHECKING:
    from .orchestrator import Orchestrator

from .media_store import MediaStore

RESTART_REQUIRED_CONFIG_KEYS = {
    "media.retention_days",
    "media.max_total_mb",
    "media.max_files_per_camera",
    "media.retention_every_n_cycles",
    "event_log.retention_days",
    "event_log.retention_every_n_cycles",
}

# Keys surfaced by /api/config for UI/Telegram parity: include every tunable even if unset
# in SQLite and omitted from the current recipe (effective value may be null).
_DISPLAY_TUNABLE_CONFIG_KEYS: tuple[str, ...] = (
    "patrol_time",
    "notification_rate_limit",
    "notify_on_preproc",
    "notify_on_detection",
    "max_file_size_mb",
    "media.retention_days",
    "media.max_total_mb",
    "media.max_files_per_camera",
    "media.retention_every_n_cycles",
    "event_log.retention_days",
    "event_log.retention_every_n_cycles",
)

# Removed from API/UI; face alerts are handled by the face pipeline. SQLite row may still
# exist until reset — omit from listings and reject writes.
DEPRECATED_CONFIG_KEYS = frozenset({"notify_on_face"})


def _service_to_dict(s: ServiceStatus) -> Dict[str, Any]:
    return {
        "service_name": s.service_name,
        "is_running": s.is_running,
        "last_check": s.last_check.isoformat() if s.last_check else None,
        "last_error": s.last_error,
        "uptime_seconds": s.uptime_seconds,
    }


def _metrics_to_dict(m: SystemMetrics) -> Dict[str, Any]:
    services = {}
    if m.services_status:
        for name, st in m.services_status.items():
            services[name] = _service_to_dict(st)
    return {
        "uptime_seconds": m.uptime_seconds,
        "total_events": m.total_events,
        "motion_events": m.motion_events,
        "person_events": m.person_events,
        "face_events": m.face_events,
        "error_events": m.error_events,
        "last_event_time": m.last_event_time.isoformat() if m.last_event_time else None,
        "services": services,
    }


class SpyoncinoRuntime:
    """
    Narrow facade over the orchestrator + MemoryManager + MediaStore.
    All mutating pipeline commands should go through this type (under the orchestrator lock).
    """

    def __init__(
        self,
        orchestrator: Orchestrator,
        media_store: Optional[MediaStore] = None,
    ):
        self._orch = orchestrator
        self._media_store = media_store
        self.logger = logging.getLogger(self.__class__.__name__)

    @property
    def memory_manager(self) -> MemoryManager:
        return self._orch.memory_manager

    @property
    def media_store(self) -> Optional[MediaStore]:
        return self._media_store

    def face_gallery_path(self) -> Path:
        """Directory used as DeepFace ``db_path`` (from recipe ``postproc`` face_identification params)."""
        path = gallery_path_from_recipe(self._orch.recipe)
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    def list_identities(self) -> List[Dict[str, Any]]:
        with self._orch._control_lock:
            return self.memory_manager.list_identities()

    def create_identity(self, display_name: str) -> Dict[str, Any]:
        gallery = self.face_gallery_path()
        with self._orch._control_lock:
            return self.memory_manager.create_identity(display_name, gallery)

    def update_identity(self, identity_id: str, display_name: str) -> bool:
        with self._orch._control_lock:
            return self.memory_manager.update_identity(identity_id, display_name)

    def delete_identity(self, identity_id: str) -> bool:
        gallery = self.face_gallery_path()
        with self._orch._control_lock:
            return self.memory_manager.delete_identity(identity_id, gallery)

    def list_pending_faces(
        self,
        status: Optional[str] = "open",
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        with self._orch._control_lock:
            rows = self.memory_manager.list_pending_faces(status=status, limit=limit)
            # Backfill identity labels for legacy "assigned" rows created before
            # assigned_identity_id/assigned_display_name columns were introduced.
            gallery_root = self.face_gallery_path()
            identities = self.memory_manager.list_identities()
            for row in rows:
                if str(row.get("status") or "") != "assigned":
                    continue
                if row.get("assigned_display_name"):
                    continue
                pending_id = str(row.get("id") or "").strip()
                if not pending_id:
                    continue
                for ident in identities:
                    folder = str(ident.get("gallery_folder") or "").strip()
                    if not folder:
                        continue
                    candidate = gallery_root / folder / f"{pending_id}.jpg"
                    if candidate.is_file():
                        row["assigned_identity_id"] = ident.get("id")
                        row["assigned_display_name"] = ident.get("display_name")
                        break
            return rows

    def assign_pending_face(
        self,
        pending_id: str,
        *,
        identity_id: Optional[str] = None,
        new_display_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self._media_store:
            raise ValueError("SpyoncinoRuntime media store not wired")
        with self._orch._control_lock:
            return self.memory_manager.assign_pending_face(
                pending_id,
                media_root=self._media_store.root,
                gallery_root=self.face_gallery_path(),
                identity_id=identity_id,
                new_display_name=new_display_name,
            )

    def reassign_assigned_face(
        self,
        pending_id: str,
        *,
        identity_id: Optional[str] = None,
        new_display_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Move gallery file from one identity folder to another (misassignment fix)."""
        with self._orch._control_lock:
            return self.memory_manager.reassign_assigned_face(
                pending_id,
                gallery_root=self.face_gallery_path(),
                identity_id=identity_id,
                new_display_name=new_display_name,
            )

    def unassign_assigned_face(self, pending_id: str) -> Dict[str, Any]:
        """Remove gallery assignment; pending face becomes open again. Prunes empty identities."""
        with self._orch._control_lock:
            return self.memory_manager.unassign_assigned_face(
                pending_id, gallery_root=self.face_gallery_path()
            )

    def ignore_pending_face(self, pending_id: str) -> bool:
        with self._orch._control_lock:
            return self.memory_manager.ignore_pending_face(pending_id)

    def recent_identified_presence(self, hours: int = 1) -> Dict[str, Any]:
        with self._orch._control_lock:
            return self.memory_manager.recent_identified_presence(hours=hours)

    def get_metrics(self) -> Dict[str, Any]:
        """Current SQL-backed metrics (same shape as ``get_status()['metrics']``)."""
        with self._orch._control_lock:
            return _metrics_to_dict(self.memory_manager.get_current_metrics())

    def get_events(
        self,
        hours: int = 24,
        event_type: Optional[str] = None,
        camera_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Recent events for dashboard/API consumers."""
        h = max(1, min(168, int(hours)))
        event_types = None
        if event_type:
            event_types = [EventType(event_type)]
        with self._orch._control_lock:
            events = self.memory_manager.get_events(
                hours=h,
                event_types=event_types,
                camera_id=camera_id,
            )
        out: List[Dict[str, Any]] = []
        for idx, event in enumerate(events):
            out.append(
                {
                    "id": idx,
                    "timestamp": event.timestamp,
                    "event_type": event.event_type.value,
                    "message": event.message,
                    "severity": event.severity,
                    "camera_id": event.camera_id,
                    "metadata": event.metadata,
                }
            )
        return out

    def get_services(self) -> Dict[str, Dict[str, Any]]:
        """Service statuses by service name."""
        with self._orch._control_lock:
            services = self.memory_manager.get_all_services_status()
        return {name: _service_to_dict(status) for name, status in services.items()}

    def _recipe_tunable_config(self) -> Dict[str, Any]:
        """
        Flat key/value defaults derived from the current recipe.
        Keys are intentionally dot-notated to match the existing `/api/config/{key}` API.
        """
        recipe = self._orch.recipe or {}
        out: Dict[str, Any] = {}
        out["patrol_time"] = recipe.get("patrol_time")

        media = recipe.get("media")
        if isinstance(media, dict):
            for key in (
                "retention_days",
                "max_total_mb",
                "max_files_per_camera",
                "retention_every_n_cycles",
            ):
                if key in media:
                    out[f"media.{key}"] = media.get(key)

        event_log = recipe.get("event_log")
        if isinstance(event_log, dict):
            for key in ("retention_days", "retention_every_n_cycles"):
                if key in event_log:
                    out[f"event_log.{key}"] = event_log.get(key)

        interfaces = recipe.get("interfaces")
        if isinstance(interfaces, list):
            for iface in interfaces:
                if not isinstance(iface, dict):
                    continue
                cls = str(iface.get("class") or "")
                try:
                    resolved = resolve_recipe_class(cls)
                except ValueError:
                    resolved = cls
                # Recipe alias "telegram" resolves to TelegramBotInterface (not *TelegramBot).
                if not (
                    resolved.endswith("TelegramBotInterface")
                    or resolved.endswith(".TelegramBot")
                ):
                    continue
                params = iface.get("params")
                if not isinstance(params, dict):
                    continue
                cfg = params.get("config")
                if not isinstance(cfg, dict):
                    continue
                for key in (
                    "notification_rate_limit",
                    "outbound_strategy",
                    "notify_on_preproc",
                    "notify_on_detection",
                    "max_file_size_mb",
                ):
                    if key in cfg:
                        out[key] = cfg.get(key)
                break
        return out

    def get_all_config(self) -> Dict[str, Any]:
        """
        Single source of truth per key: always resolve with get_config() so recipe vs SQLite
        merge cannot disagree (e.g. stale null in a partial merge).
        """
        with self._orch._control_lock:
            recipe = self._recipe_tunable_config()
            db = self.memory_manager.get_all_config()
            keys = (
                set(recipe.keys()) | set(db.keys()) | set(_DISPLAY_TUNABLE_CONFIG_KEYS)
            ) - DEPRECATED_CONFIG_KEYS
            return {k: self.get_config(k) for k in sorted(keys)}

    def get_config_traits(self) -> Dict[str, Dict[str, Any]]:
        """
        Per-key config behavior metadata for UI/bot consumers.
        """
        with self._orch._control_lock:
            keys = (
                set(self._recipe_tunable_config().keys())
                | set(self.memory_manager.get_all_config().keys())
                | set(_DISPLAY_TUNABLE_CONFIG_KEYS)
            ) - DEPRECATED_CONFIG_KEYS
        out: Dict[str, Dict[str, Any]] = {}
        for key in sorted(keys):
            requires_restart = key in RESTART_REQUIRED_CONFIG_KEYS
            out[key] = {
                "hot_swappable": not requires_restart,
                "requires_restart": requires_restart,
            }
        return out

    def get_config(self, key: str) -> Any:
        with self._orch._control_lock:
            value = self.memory_manager.get_config(key)
            if value is not None:
                return value
            return self._recipe_tunable_config().get(key)

    @staticmethod
    def _strip_wrapping_quotes(raw: str) -> str:
        s = raw.strip()
        if len(s) >= 2 and ((s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
            return s[1:-1].strip()
        return s

    def _normalize_config_value(self, key: str, value: Any) -> Any:
        if key in ("notify_on_preproc", "notify_on_detection"):
            candidate = value
            if isinstance(candidate, str):
                candidate = self._strip_wrapping_quotes(candidate)
            modes = normalize_notify_modes(candidate)
            return sorted(modes)
        if key == "patrol_time":
            try:
                f = float(value)
            except (TypeError, ValueError):
                raise ValueError("patrol_time must be a number in range 0.2..3600")
            if f < 0.2 or f > 3600.0:
                raise ValueError("patrol_time must be in range 0.2..3600")
            return f
        if key == "notification_rate_limit":
            try:
                i = int(value)
            except (TypeError, ValueError):
                raise ValueError("notification_rate_limit must be an integer >= 1")
            if i < 1:
                raise ValueError("notification_rate_limit must be >= 1")
            return i
        if key in (
            "media.retention_days",
            "media.max_files_per_camera",
            "media.retention_every_n_cycles",
            "event_log.retention_days",
            "event_log.retention_every_n_cycles",
        ):
            try:
                i = int(value)
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be an integer >= 0")
            if i < 0:
                raise ValueError(f"{key} must be >= 0")
            return i
        if key == "media.max_total_mb":
            try:
                f = float(value)
            except (TypeError, ValueError):
                raise ValueError("media.max_total_mb must be a number >= 0")
            if f < 0:
                raise ValueError("media.max_total_mb must be >= 0")
            return f
        return value

    def set_config(self, key: str, value: Any) -> Dict[str, Any]:
        if key in DEPRECATED_CONFIG_KEYS:
            raise ValueError(
                f"{key!r} is deprecated and not configurable here (face alerts use the face "
                "pipeline). Remove any DB override via reset if needed."
            )
        restart = {
            "scheduled": False,
            "newly_scheduled": False,
            "reason": None,
            "scheduled_at": None,
            "seconds_until_restart": None,
        }
        normalized = self._normalize_config_value(key, value)
        with self._orch._control_lock:
            self.memory_manager.set_config(key, normalized)
            self.memory_manager.log_event(
                EventType.CONFIG_CHANGE,
                f"Configuration updated: {key}",
                metadata={"key": key, "value": str(normalized)},
                severity="info",
            )
        if key in RESTART_REQUIRED_CONFIG_KEYS:
            restart = self._orch.schedule_restart_if_needed(f"config:{key}")
            restart["requires_restart"] = True
        else:
            restart["requires_restart"] = False
        restart["normalized_value"] = normalized
        return restart

    def reset_config(
        self,
        *,
        key: Optional[str] = None,
        reset_all: bool = False,
    ) -> Dict[str, Any]:
        if bool(key) == bool(reset_all):
            raise ValueError("Provide exactly one of key or reset_all=true")
        restart = {
            "scheduled": False,
            "newly_scheduled": False,
            "reason": None,
            "scheduled_at": None,
            "seconds_until_restart": None,
            "requires_restart": False,
        }
        removed = 0
        with self._orch._control_lock:
            current = self.memory_manager.get_all_config()
            restart_needed = False
            if reset_all:
                restart_needed = any(k in RESTART_REQUIRED_CONFIG_KEYS for k in current)
                removed = self.memory_manager.clear_config()
                self.memory_manager.log_event(
                    EventType.CONFIG_CHANGE,
                    "Configuration reset: all override keys removed",
                    metadata={"scope": "all", "removed": removed},
                    severity="warning",
                )
            else:
                if key is None:
                    raise ValueError(
                        "reset_config requires key when reset_all is false"
                    )
                restart_needed = key in RESTART_REQUIRED_CONFIG_KEYS and key in current
                removed = 1 if self.memory_manager.delete_config(key) else 0
                self.memory_manager.log_event(
                    EventType.CONFIG_CHANGE,
                    f"Configuration reset: {key}",
                    metadata={"scope": "single", "key": key, "removed": removed},
                    severity="warning",
                )
        if restart_needed:
            reason = "config_reset:all" if reset_all else f"config_reset:{key}"
            restart = self._orch.schedule_restart_if_needed(reason)
            restart["requires_restart"] = True
        return {
            "ok": True,
            "scope": "all" if reset_all else "single",
            "key": key,
            "removed": removed,
            "restart_schedule": restart,
        }

    def get_analytics_summary(self, hours: int) -> Dict[str, Any]:
        """Aggregated lifetime metrics plus a time-window slice for analytics UI."""
        h = max(1, min(168, int(hours)))
        with self._orch._control_lock:
            m = self.memory_manager.get_current_metrics()
            window = self.memory_manager.get_analytics_window(h)
        return {
            "hours": h,
            "metrics": _metrics_to_dict(m),
            "window": window,
        }

    def get_analytics_chart_jpeg(self, hours: int) -> Optional[bytes]:
        """Render the events trend chart as JPEG bytes (may return None on failure)."""
        h = max(1, min(168, int(hours)))
        with self._orch._control_lock:
            series = self.memory_manager.get_hourly_event_bins(h)
        return render_events_trend_jpeg(h, series)

    def get_analytics_series(self, hours: int) -> Dict[str, Any]:
        """Hourly event bins as JSON for dashboard charts (index 0 = oldest hour)."""
        h = max(1, min(168, int(hours)))
        with self._orch._control_lock:
            series = self.memory_manager.get_hourly_event_bins(h)
        return {"hours": h, "series": series}

    def get_status(self) -> Dict[str, Any]:
        """Snapshot: metrics, services, loop state (safe for JSON)."""
        with self._orch._control_lock:
            metrics = self.memory_manager.get_current_metrics()
            camera_ids = [
                str(getattr(c, "cam_id", "") or "").strip()
                for c in getattr(self._orch, "inputs", []) or []
                if getattr(c, "cam_id", None)
            ]
            out = {
                "paused": self._orch._paused,
                "orchestrator_running": self._orch.running,
                "total_cycles": self._orch.total_cycles,
                "patrol_time": self._orch.patrol_time,
                "camera_ids": camera_ids,
                "metrics": _metrics_to_dict(metrics),
                "restart_schedule": self._orch.get_restart_schedule_status(),
            }
        for iface in getattr(self._orch, "interfaces", []) or []:
            snap = getattr(iface, "outbound_metrics", None)
            if callable(snap):
                try:
                    out["telegram_outbound"] = snap()
                except Exception:
                    self.logger.debug("outbound_metrics failed", exc_info=True)
                break
        return out

    def set_paused(self, paused: bool) -> None:
        with self._orch._control_lock:
            prev = self._orch._paused
            np = bool(paused)
            self._orch._paused = np
            if prev != np:
                self.memory_manager.log_event(
                    EventType.PATROL,
                    "Patrol paused" if np else "Patrol resumed",
                    metadata={"paused": np},
                    severity="info",
                )

    def is_paused(self) -> bool:
        with self._orch._control_lock:
            return self._orch._paused

    def list_media(
        self,
        camera_id: Optional[str] = None,
        stage: Optional[str] = None,
        kind: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        return self.memory_manager.list_media_artifacts(
            camera_id=camera_id,
            stage=stage,
            kind=kind,
            since=since,
            until=until,
            limit=limit,
            offset=offset,
        )

    def get_media_artifact_meta(self, artifact_id: int) -> Optional[Dict[str, Any]]:
        """DB row for one artifact (id, camera_id, stage, kind, path_rel, …) or None."""
        row = self.memory_manager.get_media_artifact(artifact_id)
        if not row:
            return None
        out = dict(row)
        ca = out.get("created_at")
        if isinstance(ca, datetime):
            out["created_at"] = ca.astimezone(timezone.utc).isoformat()
        return out

    def get_media_path(self, artifact_id: int) -> Optional[Path]:
        """Absolute path for an indexed artifact, or None if missing/invalid."""
        row = self.memory_manager.get_media_artifact(artifact_id)
        if not row or not self._media_store:
            return None
        try:
            return self._media_store.resolve_relative(row["path_rel"])
        except ValueError:
            return None

    def snap(self, camera_id: str) -> Optional[Dict[str, Any]]:
        """
        Grab the latest frame from the given camera, write JPEG under media root, index row.
        Returns id, path_rel, and absolute path strings, or None.
        """
        with self._orch._control_lock:
            cam = next(
                (
                    c
                    for c in self._orch.inputs
                    if getattr(c, "cam_id", None) == camera_id
                ),
                None,
            )
            if cam is None:
                return None
            snap = cam.snap()
            if not snap or snap.get("frame") is None:
                return None
            if not self._media_store:
                self.logger.warning("snap: no media_store configured")
                return None

            out = self._media_store.new_artifact_path(camera_id, "snap", "jpeg")
            frame = snap["frame"]
            ok = cv2.imwrite(str(out), frame)
            if not ok:
                self.logger.error("snap: cv2.imwrite failed for %s", out)
                return None
            rel = self._media_store.path_relative_to_root(out)
            if not rel:
                return None
            try:
                size_b = out.stat().st_size
            except OSError:
                size_b = None
            ts = snap.get("timestamp")
            created = ts if isinstance(ts, datetime) else datetime.now(timezone.utc)
            mid = self.memory_manager.insert_media_artifact(
                camera_id=camera_id,
                stage="snap",
                kind="jpeg",
                path_rel=rel,
                size_bytes=size_b,
                metadata={"source": "api_snap"},
                created_at=created,
            )
            return {
                "id": mid,
                "path_rel": rel,
                "path": str(out),
                "camera_id": camera_id,
            }
