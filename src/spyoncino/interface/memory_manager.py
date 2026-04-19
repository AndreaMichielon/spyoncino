"""
Memory Manager - Permanent storage for metrics, events, and configuration.

Provides persistent storage for:
- System metrics (uptime, service status)
- Event logging
- Configuration parameters
"""

import sqlite3
import json
import logging
import re
import shutil
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass
from enum import Enum


class EventType(str, Enum):
    """Event types for logging."""

    MOTION = "motion"
    PERSON = "person"
    FACE = "face"
    DISCONNECT = "disconnect"
    RECONNECT = "reconnect"
    ERROR = "error"
    STARTUP = "startup"
    SHUTDOWN = "shutdown"
    PATROL = "patrol"
    STORAGE_WARNING = "storage_warning"
    CONFIG_CHANGE = "config_change"


@dataclass
class Event:
    """Represents a system event."""

    timestamp: datetime
    event_type: EventType
    message: str
    metadata: Optional[Dict[str, Any]] = None
    severity: str = "info"  # info, warning, error
    camera_id: Optional[str] = None


@dataclass
class ServiceStatus:
    """Service status information."""

    service_name: str
    is_running: bool
    last_check: datetime
    last_error: Optional[str] = None
    uptime_seconds: Optional[float] = None


@dataclass
class SystemMetrics:
    """System-wide metrics."""

    uptime_seconds: float
    total_events: int
    motion_events: int
    person_events: int
    face_events: int
    error_events: int
    last_event_time: Optional[datetime] = None
    services_status: Dict[str, ServiceStatus] = None


class MemoryManager:
    """
    Permanent storage manager for metrics, events, and configuration.

    Uses SQLite for persistent storage with separate tables for:
    - Events: Event logging
    - Metrics: System metrics snapshots
    - Services: Service status tracking
    - Config: Configuration parameters
    """

    def __init__(self, db_path: str = "spyoncino.db"):
        """
        Initialize the memory manager.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = Path(db_path)
        self.logger = logging.getLogger(self.__class__.__name__)
        self._init_database()
        self._start_time = datetime.now()

    def _init_database(self) -> None:
        """Initialize SQLite database with required tables."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()

                # Events table
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp DATETIME NOT NULL,
                        event_type TEXT NOT NULL,
                        message TEXT NOT NULL,
                        metadata TEXT,
                        severity TEXT DEFAULT 'info',
                        camera_id TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                # Metrics snapshots table
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS metrics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp DATETIME NOT NULL,
                        uptime_seconds REAL NOT NULL,
                        total_events INTEGER DEFAULT 0,
                        motion_events INTEGER DEFAULT 0,
                        person_events INTEGER DEFAULT 0,
                        error_events INTEGER DEFAULT 0,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                # Services status table
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS services (
                        service_name TEXT PRIMARY KEY,
                        is_running BOOLEAN NOT NULL,
                        last_check DATETIME NOT NULL,
                        last_error TEXT,
                        uptime_seconds REAL,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                # Configuration table
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS config (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)

                # Create indexes for faster queries
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_events_timestamp
                    ON events(timestamp)
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_events_type
                    ON events(event_type)
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_events_ts_type
                    ON events(timestamp, event_type)
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_metrics_timestamp
                    ON metrics(timestamp)
                """)

                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS media_artifacts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        camera_id TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        path_rel TEXT NOT NULL,
                        size_bytes INTEGER,
                        created_at TEXT NOT NULL,
                        metadata TEXT,
                        created_at_db DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_media_camera_created
                    ON media_artifacts(camera_id, created_at)
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_media_stage_created
                    ON media_artifacts(stage, created_at)
                """)

                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS identities (
                        id TEXT PRIMARY KEY,
                        display_name TEXT NOT NULL,
                        gallery_folder TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL
                    )
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS pending_faces (
                        id TEXT PRIMARY KEY,
                        camera_id TEXT NOT NULL,
                        path_rel TEXT NOT NULL,
                        embedding_hash TEXT,
                        champion_frame_index INTEGER,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'open',
                        assigned_identity_id TEXT,
                        assigned_display_name TEXT
                    )
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_pending_faces_status_created
                    ON pending_faces(status, created_at)
                """)
                cols = {
                    str(r[1])
                    for r in cursor.execute("PRAGMA table_info(pending_faces)")
                }
                if "assigned_identity_id" not in cols:
                    cursor.execute(
                        "ALTER TABLE pending_faces ADD COLUMN assigned_identity_id TEXT"
                    )
                if "assigned_display_name" not in cols:
                    cursor.execute(
                        "ALTER TABLE pending_faces ADD COLUMN assigned_display_name TEXT"
                    )

                self._merge_duplicate_identity_rows(conn)
                conn.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_identities_display_name_ci
                    ON identities(lower(trim(display_name)))
                    """
                )

                conn.commit()
                self.logger.info(f"Database initialized: {self.db_path}")

        except sqlite3.Error as e:
            self.logger.error(f"Database initialization failed: {e}")
            raise

    def _merge_duplicate_identity_rows(self, conn: sqlite3.Connection) -> None:
        """
        Enforce one identity per display name (case-insensitive): merge legacy duplicates.

        Keeps the oldest row by ``created_at`` (then smallest ``id``). Repoints
        ``pending_faces.assigned_identity_id`` to the keeper; deletes other rows.
        Duplicate gallery folders may remain on disk until removed manually or by a
        future cleanup pass.
        """
        try:
            dup_groups = conn.execute(
                """
                SELECT lower(trim(display_name)) AS k, COUNT(*) AS c
                FROM identities
                GROUP BY k
                HAVING c > 1
                """
            ).fetchall()
        except sqlite3.Error as e:
            self.logger.error("_merge_duplicate_identity_rows: list failed: %s", e)
            return
        for key, _cnt in dup_groups:
            rows = conn.execute(
                """
                SELECT id, display_name, gallery_folder, created_at FROM identities
                WHERE lower(trim(display_name)) = ?
                ORDER BY COALESCE(created_at, '') ASC, id ASC
                """,
                (key,),
            ).fetchall()
            if len(rows) < 2:
                continue
            keeper_id = str(rows[0][0])
            drop_ids = [str(r[0]) for r in rows[1:]]
            for drop_id in drop_ids:
                conn.execute(
                    """
                    UPDATE pending_faces
                    SET assigned_identity_id = ?
                    WHERE assigned_identity_id = ?
                    """,
                    (keeper_id, drop_id),
                )
            for drop_id in drop_ids:
                conn.execute("DELETE FROM identities WHERE id = ?", (drop_id,))
            self.logger.warning(
                "Merged duplicate identities for name key %r: kept id %s; removed %s. "
                "Stale gallery folders may remain for removed ids.",
                key,
                keeper_id,
                drop_ids,
            )

    def log_event(
        self,
        event_type: EventType,
        message: str,
        metadata: Optional[Dict[str, Any]] = None,
        severity: str = "info",
        camera_id: Optional[str] = None,
    ) -> None:
        """
        Log an event to the database.

        Args:
            event_type: Type of event
            message: Event message
            metadata: Optional metadata dictionary
            severity: Event severity (info, warning, error)
            camera_id: Optional camera identifier
        """
        try:
            metadata_json = json.dumps(metadata) if metadata else None

            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO events (timestamp, event_type, message, metadata, severity, camera_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                """,
                    (
                        datetime.now(),
                        event_type.value,
                        message,
                        metadata_json,
                        severity,
                        camera_id,
                    ),
                )
                conn.commit()

        except sqlite3.Error as e:
            self.logger.error(f"Failed to log event: {e}")

    def get_events(
        self,
        hours: int = 24,
        event_types: Optional[List[EventType]] = None,
        camera_id: Optional[str] = None,
    ) -> List[Event]:
        """
        Retrieve events from the database.

        Args:
            hours: Number of hours to look back
            event_types: Optional list of event types to filter
            camera_id: Optional camera ID to filter

        Returns:
            List of Event objects
        """
        try:
            since = datetime.now() - timedelta(hours=hours)

            query = "SELECT timestamp, event_type, message, metadata, severity, camera_id FROM events WHERE timestamp >= ?"
            params = [since]

            if event_types:
                placeholders = ",".join(["?"] * len(event_types))
                query += f" AND event_type IN ({placeholders})"
                params.extend([et.value for et in event_types])

            if camera_id:
                query += " AND camera_id = ?"
                params.append(camera_id)

            query += " ORDER BY timestamp DESC"

            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(query, params)
                rows = cursor.fetchall()

                events = []
                for row in rows:
                    metadata = json.loads(row["metadata"]) if row["metadata"] else None
                    events.append(
                        Event(
                            timestamp=datetime.fromisoformat(row["timestamp"]),
                            event_type=EventType(row["event_type"]),
                            message=row["message"],
                            metadata=metadata,
                            severity=row["severity"],
                            camera_id=row["camera_id"],
                        )
                    )

                return events

        except sqlite3.Error as e:
            self.logger.error(f"Failed to retrieve events: {e}")
            return []

    @staticmethod
    def _parse_event_ts(raw: Any) -> datetime:
        if isinstance(raw, datetime):
            return raw
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw)
            except ValueError:
                pass
        return datetime.now()

    def recent_identified_presence(
        self,
        hours: int = 1,
        *,
        scan_limit: int = 400,
    ) -> Dict[str, Any]:
        """
        Summarize recent FACE events: last sighting per known display name, plus unknown glimpses.

        Expects ``known_display_names`` / ``unknown_face_count`` in event metadata (newer runs).
        """
        h = max(1, min(168, int(hours)))
        lim = max(50, min(2000, int(scan_limit)))
        since = datetime.now() - timedelta(hours=h)
        by_name: Dict[str, Dict[str, Any]] = {}
        unknown_glimpses: List[Dict[str, Any]] = []
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    """
                    SELECT timestamp, metadata, camera_id FROM events
                    WHERE event_type = ? AND timestamp >= ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                    """,
                    (EventType.FACE.value, since, lim),
                )
                for row in cur:
                    ts = self._parse_event_ts(row["timestamp"])
                    cam = row["camera_id"]
                    md: Dict[str, Any] = {}
                    if row["metadata"]:
                        try:
                            md = json.loads(row["metadata"])
                        except (TypeError, json.JSONDecodeError):
                            md = {}
                    if not isinstance(md, dict):
                        md = {}
                    ts_iso = ts.isoformat(sep=" ", timespec="seconds")
                    names = md.get("known_display_names")
                    if isinstance(names, list):
                        for nm in names:
                            if not isinstance(nm, str):
                                continue
                            key = nm.strip()
                            if not key:
                                continue
                            if key not in by_name:
                                by_name[key] = {
                                    "display_name": key,
                                    "last_seen": ts_iso,
                                    "camera_id": cam or md.get("camera_id"),
                                }
                    unk = md.get("unknown_face_count", 0)
                    try:
                        unk_i = int(unk)
                    except (TypeError, ValueError):
                        unk_i = 0
                    if unk_i > 0 and len(unknown_glimpses) < 20:
                        unknown_glimpses.append(
                            {
                                "last_seen": ts_iso,
                                "camera_id": str(cam or md.get("camera_id") or ""),
                                "count": unk_i,
                            }
                        )
        except sqlite3.Error as e:
            self.logger.error("recent_identified_presence failed: %s", e)
        identified = sorted(
            by_name.values(),
            key=lambda r: r.get("last_seen") or "",
            reverse=True,
        )
        return {
            "hours": h,
            "identified": identified,
            "unknown_glimpses": unknown_glimpses,
        }

    def update_service_status(
        self,
        service_name: str,
        is_running: bool,
        last_error: Optional[str] = None,
        uptime_seconds: Optional[float] = None,
    ) -> None:
        """
        Update service status.

        Args:
            service_name: Name of the service
            is_running: Whether the service is running
            last_error: Optional error message
            uptime_seconds: Optional uptime in seconds
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO services
                    (service_name, is_running, last_check, last_error, uptime_seconds, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """,
                    (
                        service_name,
                        is_running,
                        datetime.now(),
                        last_error,
                        uptime_seconds,
                        datetime.now(),
                    ),
                )
                conn.commit()

        except sqlite3.Error as e:
            self.logger.error(f"Failed to update service status: {e}")

    def get_service_status(self, service_name: str) -> Optional[ServiceStatus]:
        """
        Get service status.

        Args:
            service_name: Name of the service

        Returns:
            ServiceStatus object or None if not found
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT * FROM services WHERE service_name = ?", (service_name,)
                )
                row = cursor.fetchone()

                if row:
                    return ServiceStatus(
                        service_name=row["service_name"],
                        is_running=bool(row["is_running"]),
                        last_check=datetime.fromisoformat(row["last_check"]),
                        last_error=row["last_error"],
                        uptime_seconds=row["uptime_seconds"],
                    )
                return None

        except sqlite3.Error as e:
            self.logger.error(f"Failed to get service status: {e}")
            return None

    def get_all_services_status(self) -> Dict[str, ServiceStatus]:
        """Get status of all services."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute("SELECT * FROM services")
                rows = cursor.fetchall()

                services = {}
                for row in rows:
                    services[row["service_name"]] = ServiceStatus(
                        service_name=row["service_name"],
                        is_running=bool(row["is_running"]),
                        last_check=datetime.fromisoformat(row["last_check"]),
                        last_error=row["last_error"],
                        uptime_seconds=row["uptime_seconds"],
                    )

                return services

        except sqlite3.Error as e:
            self.logger.error(f"Failed to get services status: {e}")
            return {}

    def save_metrics_snapshot(self, metrics: SystemMetrics) -> None:
        """
        Save a metrics snapshot.

        Args:
            metrics: SystemMetrics object to save
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO metrics
                    (timestamp, uptime_seconds, total_events, motion_events, person_events, error_events)
                    VALUES (?, ?, ?, ?, ?, ?)
                """,
                    (
                        datetime.now(),
                        metrics.uptime_seconds,
                        metrics.total_events,
                        metrics.motion_events,
                        metrics.person_events,
                        metrics.error_events,
                    ),
                )
                conn.commit()

        except sqlite3.Error as e:
            self.logger.error(f"Failed to save metrics snapshot: {e}")

    def get_current_metrics(self) -> SystemMetrics:
        """
        Get current system metrics (SQL aggregates — no full-table row load).
        """
        uptime = (datetime.now() - self._start_time).total_seconds()
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT
                      COUNT(*) AS total,
                      COALESCE(SUM(CASE WHEN event_type = ? THEN 1 ELSE 0 END), 0) AS motion,
                      COALESCE(SUM(CASE WHEN event_type = ? THEN 1 ELSE 0 END), 0) AS person,
                      COALESCE(SUM(CASE WHEN event_type = ? THEN 1 ELSE 0 END), 0) AS face,
                      COALESCE(SUM(CASE WHEN severity = 'error' THEN 1 ELSE 0 END), 0) AS err
                    FROM events
                    """,
                    (
                        EventType.MOTION.value,
                        EventType.PERSON.value,
                        EventType.FACE.value,
                    ),
                ).fetchone()
                last_raw = conn.execute("SELECT MAX(timestamp) FROM events").fetchone()
            last_event: Optional[datetime] = None
            if last_raw and last_raw[0] is not None:
                try:
                    last_event = datetime.fromisoformat(str(last_raw[0]))
                except ValueError:
                    last_event = None
            services_status = self.get_all_services_status()
            return SystemMetrics(
                uptime_seconds=uptime,
                total_events=int(row[0] or 0),
                motion_events=int(row[1] or 0),
                person_events=int(row[2] or 0),
                face_events=int(row[3] or 0),
                error_events=int(row[4] or 0),
                last_event_time=last_event,
                services_status=services_status,
            )
        except Exception as e:
            self.logger.error(f"Failed to get current metrics: {e}")
            return SystemMetrics(
                uptime_seconds=uptime,
                total_events=0,
                motion_events=0,
                person_events=0,
                face_events=0,
                error_events=0,
                services_status={},
            )

    def get_analytics_window(self, hours: int) -> Dict[str, Any]:
        """
        Aggregated counts for [now - hours, now] without loading all event rows.
        """
        hours = max(1, min(168, int(hours)))
        since = datetime.now() - timedelta(hours=hours)
        out: Dict[str, Any] = {
            "events_total": 0,
            "by_type": {"motion": 0, "person": 0, "face": 0},
            "warnings": 0,
            "errors": 0,
        }
        try:
            with sqlite3.connect(self.db_path) as conn:
                total = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE timestamp >= ?",
                    (since,),
                ).fetchone()
                out["events_total"] = int(total[0] if total else 0)
                for et, c in conn.execute(
                    """
                    SELECT event_type, COUNT(*) FROM events
                    WHERE timestamp >= ? AND event_type IN ('motion', 'person', 'face')
                    GROUP BY event_type
                    """,
                    (since,),
                ).fetchall():
                    if et in out["by_type"]:
                        out["by_type"][et] = int(c)
                for sev, c in conn.execute(
                    """
                    SELECT severity, COUNT(*) FROM events
                    WHERE timestamp >= ? AND severity IN ('warning', 'error')
                    GROUP BY severity
                    """,
                    (since,),
                ).fetchall():
                    if sev == "warning":
                        out["warnings"] = int(c)
                    elif sev == "error":
                        out["errors"] = int(c)
        except sqlite3.Error as e:
            self.logger.error(f"get_analytics_window failed: {e}")
        return out

    @staticmethod
    def _apply_orch_event_state(
        state: Tuple[bool, bool], row: Any
    ) -> Tuple[bool, bool]:
        """Update (process_alive, paused) from orchestrator lifecycle or patrol toggle."""
        process_alive, paused = state
        et = str(row["event_type"] or "")
        if et == "startup":
            return (True, False)
        if et == "shutdown":
            return (False, False)
        if et == "patrol":
            raw = row["metadata"]
            if raw:
                try:
                    d = json.loads(raw)
                    if isinstance(d, dict) and "paused" in d:
                        return (process_alive, bool(d["paused"]))
                except (TypeError, json.JSONDecodeError):
                    pass
        return state

    def _hourly_patrol_uptime_percent(self, hours: int) -> List[int]:
        """
        Per clock-hour fraction of time the patrol loop was active (orchestrator up and not paused).
        Index 0 = oldest hour; values 0-100. Uses ``patrol`` events plus Orchestrator startup/shutdown.
        """
        hours = max(1, min(168, int(hours)))
        now = datetime.now()
        since = now - timedelta(hours=hours)
        lookback = since - timedelta(days=120)
        out = [100] * hours
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                pre = conn.execute(
                    """
                    SELECT * FROM (
                        SELECT timestamp, event_type, message, metadata FROM events
                        WHERE timestamp < ?
                          AND (
                            event_type = 'patrol'
                            OR (event_type = 'startup' AND message LIKE '%Orchestrator%')
                            OR (event_type = 'shutdown' AND message LIKE '%Orchestrator%')
                          )
                        ORDER BY timestamp DESC
                        LIMIT 200
                    ) ORDER BY timestamp ASC
                    """,
                    (lookback,),
                ).fetchall()
                state = (False, False)
                for row in pre:
                    state = self._apply_orch_event_state(state, row)
                post = conn.execute(
                    """
                    SELECT timestamp, event_type, message, metadata FROM events
                    WHERE timestamp >= ? AND timestamp <= ?
                      AND (
                        event_type = 'patrol'
                        OR (event_type = 'startup' AND message LIKE '%Orchestrator%')
                        OR (event_type = 'shutdown' AND message LIKE '%Orchestrator%')
                      )
                    ORDER BY timestamp ASC
                    """,
                    (lookback, now),
                ).fetchall()
                segments: List[Tuple[datetime, datetime, bool]] = []
                t = lookback
                for row in post:
                    ts = self._parse_event_ts(row["timestamp"])
                    if ts > now:
                        ts = now
                    if ts < t:
                        continue
                    pa, pz = state
                    active = pa and not pz
                    if ts > t:
                        segments.append((t, ts, active))
                    state = self._apply_orch_event_state(state, row)
                    t = ts
                pa, pz = state
                active = pa and not pz
                if t < now:
                    segments.append((t, now, active))
                for idx in range(hours):
                    age_h = hours - 1 - idx
                    b_end = now - timedelta(hours=age_h)
                    b_start = now - timedelta(hours=age_h + 1)
                    sec = 0.0
                    for s0, s1, act in segments:
                        if not act:
                            continue
                        lo = max(b_start, s0)
                        hi = min(b_end, s1)
                        if hi > lo:
                            sec += (hi - lo).total_seconds()
                    total = (b_end - b_start).total_seconds()
                    if total <= 0:
                        pct = 100
                    else:
                        pct = int(round(min(100.0, max(0.0, sec / total * 100.0))))
                    out[idx] = pct
        except Exception as e:
            self.logger.error("_hourly_patrol_uptime_percent failed: %s", e)
        return out

    def get_hourly_event_bins(self, hours: int) -> Dict[str, List[int]]:
        """
        Hourly buckets aligned with the legacy Telegram plot: index 0 = oldest hour
        in the window, index hours-1 = current hour.

        Keys: motion, person, face, error (counts), system (0-100 patrol uptime %).
        """
        hours = max(1, min(168, int(hours)))
        since = datetime.now() - timedelta(hours=hours)
        bins = {k: [0] * hours for k in ("motion", "person", "face", "error")}
        try:
            with sqlite3.connect(self.db_path) as conn:
                # Static SQL only (no string interpolation) — age bucket expression is fixed.
                q_type = """
                    SELECT CAST((strftime('%s', 'now') - strftime('%s', timestamp)) / 3600 AS INTEGER) AS age_h, event_type, COUNT(*) AS c
                    FROM events
                    WHERE timestamp >= ?
                      AND CAST((strftime('%s', 'now') - strftime('%s', timestamp)) / 3600 AS INTEGER) >= 0
                      AND CAST((strftime('%s', 'now') - strftime('%s', timestamp)) / 3600 AS INTEGER) < ?
                      AND event_type IN ('motion', 'person', 'face')
                    GROUP BY 1, 2
                """
                for age_h, et, c in conn.execute(q_type, (since, hours)).fetchall():
                    ah = int(age_h)
                    if ah < 0 or ah >= hours:
                        continue
                    idx = hours - 1 - ah
                    if et in bins:
                        bins[et][idx] += int(c)
                q_err = """
                    SELECT CAST((strftime('%s', 'now') - strftime('%s', timestamp)) / 3600 AS INTEGER) AS age_h, COUNT(*) AS c
                    FROM events
                    WHERE timestamp >= ?
                      AND CAST((strftime('%s', 'now') - strftime('%s', timestamp)) / 3600 AS INTEGER) >= 0
                      AND CAST((strftime('%s', 'now') - strftime('%s', timestamp)) / 3600 AS INTEGER) < ?
                      AND severity = 'error'
                    GROUP BY 1
                """
                for age_h, c in conn.execute(q_err, (since, hours)).fetchall():
                    ah = int(age_h)
                    if ah < 0 or ah >= hours:
                        continue
                    idx = hours - 1 - ah
                    bins["error"][idx] += int(c)
        except sqlite3.Error as e:
            self.logger.error(f"get_hourly_event_bins failed: {e}")
        bins["system"] = self._hourly_patrol_uptime_percent(hours)
        return bins

    def set_config(self, key: str, value: Any) -> None:
        """
        Set a configuration parameter.

        Args:
            key: Configuration key
            value: Configuration value (will be JSON-encoded)
        """
        try:
            value_json = json.dumps(value)

            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO config (key, value, updated_at)
                    VALUES (?, ?, ?)
                """,
                    (key, value_json, datetime.now()),
                )
                conn.commit()

        except sqlite3.Error as e:
            self.logger.error(f"Failed to set config: {e}")

    def get_config(self, key: str, default: Any = None) -> Any:
        """
        Get a configuration parameter.

        Args:
            key: Configuration key
            default: Default value if not found

        Returns:
            Configuration value (JSON-decoded) or default
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("SELECT value FROM config WHERE key = ?", (key,))
                row = cursor.fetchone()

                if row:
                    return json.loads(row[0])
                return default

        except sqlite3.Error as e:
            self.logger.error(f"Failed to get config: {e}")
            return default

    def get_all_config(self) -> Dict[str, Any]:
        """Get all configuration parameters."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("SELECT key, value FROM config")
                rows = cursor.fetchall()

                config = {}
                for row in rows:
                    config[row[0]] = json.loads(row[1])

                return config

        except sqlite3.Error as e:
            self.logger.error(f"Failed to get all config: {e}")
            return {}

    def delete_config(self, key: str) -> bool:
        """Delete one configuration override key. Returns True if a row was removed."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute("DELETE FROM config WHERE key = ?", (key,))
                conn.commit()
                return cur.rowcount > 0
        except sqlite3.Error as e:
            self.logger.error(f"Failed to delete config key {key!r}: {e}")
            return False

    def clear_config(self) -> int:
        """Delete all configuration override rows. Returns removed row count."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute("DELETE FROM config")
                conn.commit()
                return int(cur.rowcount or 0)
        except sqlite3.Error as e:
            self.logger.error(f"Failed to clear config: {e}")
            return 0

    def insert_media_artifact(
        self,
        camera_id: str,
        stage: str,
        kind: str,
        path_rel: str,
        size_bytes: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
        created_at: Optional[datetime] = None,
    ) -> Optional[int]:
        """Register a file already written under the media root. Returns row id."""
        try:
            ts = created_at or datetime.now(timezone.utc)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            created_iso = ts.isoformat()
            meta_json = json.dumps(metadata) if metadata else None
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute(
                    """
                    INSERT INTO media_artifacts
                    (camera_id, stage, kind, path_rel, size_bytes, created_at, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        camera_id,
                        stage,
                        kind,
                        path_rel,
                        size_bytes,
                        created_iso,
                        meta_json,
                    ),
                )
                conn.commit()
                return int(cur.lastrowid)
        except sqlite3.Error as e:
            self.logger.error(f"Failed to insert media artifact: {e}")
            return None

    def list_media_artifacts(
        self,
        camera_id: Optional[str] = None,
        stage: Optional[str] = None,
        kind: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """List indexed media (paths are relative to media root)."""
        try:
            q = (
                "SELECT id, camera_id, stage, kind, path_rel, size_bytes, created_at, metadata "
                "FROM media_artifacts WHERE 1=1"
            )
            params: List[Any] = []
            if camera_id:
                q += " AND camera_id = ?"
                params.append(camera_id)
            if stage:
                q += " AND stage = ?"
                params.append(stage)
            if kind:
                q += " AND kind = ?"
                params.append(kind)
            if since:
                q += " AND created_at >= ?"
                params.append(since.astimezone(timezone.utc).isoformat())
            if until:
                q += " AND created_at <= ?"
                params.append(until.astimezone(timezone.utc).isoformat())
            q += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            out: List[Dict[str, Any]] = []
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                for row in conn.execute(q, params):
                    meta = json.loads(row["metadata"]) if row["metadata"] else None
                    out.append(
                        {
                            "id": row["id"],
                            "camera_id": row["camera_id"],
                            "stage": row["stage"],
                            "kind": row["kind"],
                            "path_rel": row["path_rel"],
                            "size_bytes": row["size_bytes"],
                            "created_at": row["created_at"],
                            "metadata": meta,
                        }
                    )
            return out
        except sqlite3.Error as e:
            self.logger.error(f"Failed to list media artifacts: {e}")
            return []

    def get_media_artifact(self, artifact_id: int) -> Optional[Dict[str, Any]]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT id, camera_id, stage, kind, path_rel, size_bytes, created_at, metadata "
                    "FROM media_artifacts WHERE id = ?",
                    (artifact_id,),
                ).fetchone()
                if not row:
                    return None
                meta = json.loads(row["metadata"]) if row["metadata"] else None
                return {
                    "id": row["id"],
                    "camera_id": row["camera_id"],
                    "stage": row["stage"],
                    "kind": row["kind"],
                    "path_rel": row["path_rel"],
                    "size_bytes": row["size_bytes"],
                    "created_at": row["created_at"],
                    "metadata": meta,
                }
        except sqlite3.Error as e:
            self.logger.error(f"Failed to get media artifact: {e}")
            return None

    def delete_media_artifact_row(self, artifact_id: int) -> bool:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM media_artifacts WHERE id = ?", (artifact_id,))
                conn.commit()
            return True
        except sqlite3.Error as e:
            self.logger.error(f"Failed to delete media artifact row: {e}")
            return False

    def apply_media_retention(
        self,
        media_root: Path,
        retention_days: Optional[int] = None,
        max_total_mb: Optional[float] = None,
        max_files_per_camera: Optional[int] = None,
    ) -> Dict[str, int]:
        """
        Delete files under media_root and matching DB rows per policy.
        Order: age cutoff, then per-camera file cap, then global size cap.
        """
        stats = {"age_deleted": 0, "cap_deleted": 0, "size_deleted": 0}
        root = Path(media_root).resolve()
        if not root.is_dir():
            return stats

        def unlink_safe(rel: str) -> None:
            try:
                p = (root / Path(rel)).resolve()
                p.relative_to(root)
            except ValueError:
                return
            try:
                if p.is_file():
                    p.unlink(missing_ok=True)
            except OSError as e:
                self.logger.warning("Retention unlink failed for %s: %s", rel, e)

        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                # Age-based
                if retention_days is not None and retention_days > 0:
                    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
                    cutoff_iso = cutoff.isoformat()
                    rows = conn.execute(
                        "SELECT id, path_rel FROM media_artifacts WHERE created_at < ?",
                        (cutoff_iso,),
                    ).fetchall()
                    for row in rows:
                        unlink_safe(row["path_rel"])
                        conn.execute(
                            "DELETE FROM media_artifacts WHERE id = ?", (row["id"],)
                        )
                        stats["age_deleted"] += 1
                    conn.commit()

                # Per-camera count cap (oldest first)
                if max_files_per_camera is not None and max_files_per_camera > 0:
                    cams = conn.execute(
                        "SELECT DISTINCT camera_id FROM media_artifacts"
                    ).fetchall()
                    for (cam,) in cams:
                        rows = conn.execute(
                            """
                            SELECT id, path_rel FROM media_artifacts
                            WHERE camera_id = ?
                            ORDER BY created_at ASC
                            """,
                            (cam,),
                        ).fetchall()
                        overflow = len(rows) - max_files_per_camera
                        for row in rows[: max(0, overflow)]:
                            unlink_safe(row["path_rel"])
                            conn.execute(
                                "DELETE FROM media_artifacts WHERE id = ?", (row["id"],)
                            )
                            stats["cap_deleted"] += 1
                    conn.commit()

                # Global size cap (oldest first)
                if max_total_mb is not None and max_total_mb > 0:
                    max_bytes = int(max_total_mb * 1024 * 1024)
                    while True:
                        rows = conn.execute(
                            """
                            SELECT id, path_rel, size_bytes FROM media_artifacts
                            ORDER BY created_at ASC
                            """
                        ).fetchall()
                        if not rows:
                            break
                        total = 0
                        for row in rows:
                            sz = row["size_bytes"]
                            if sz is None:
                                try:
                                    p = (root / Path(row["path_rel"])).resolve()
                                    p.relative_to(root)
                                    sz = p.stat().st_size if p.is_file() else 0
                                except (OSError, ValueError):
                                    sz = 0
                            total += int(sz)
                        if total <= max_bytes:
                            break
                        row = rows[0]
                        unlink_safe(row["path_rel"])
                        conn.execute(
                            "DELETE FROM media_artifacts WHERE id = ?", (row["id"],)
                        )
                        stats["size_deleted"] += 1
                        conn.commit()

        except sqlite3.Error as e:
            self.logger.error(f"Media retention failed: {e}")
        return stats

    @staticmethod
    def _gallery_folder_slug(display_name: str) -> str:
        raw = (display_name or "").strip()
        s = re.sub(r"[^a-zA-Z0-9._-]+", "_", raw)[:64].strip("._-") or "person"
        return s

    def list_identities(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                for row in conn.execute(
                    "SELECT id, display_name, gallery_folder, created_at FROM identities ORDER BY display_name"
                ):
                    out.append(
                        {
                            "id": row["id"],
                            "display_name": row["display_name"],
                            "gallery_folder": row["gallery_folder"],
                            "created_at": row["created_at"],
                        }
                    )
        except sqlite3.Error as e:
            self.logger.error("list_identities failed: %s", e)
        return out

    def get_identity(self, identity_id: str) -> Optional[Dict[str, Any]]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT id, display_name, gallery_folder, created_at FROM identities WHERE id = ?",
                    (identity_id,),
                ).fetchone()
                if not row:
                    return None
                return {
                    "id": row["id"],
                    "display_name": row["display_name"],
                    "gallery_folder": row["gallery_folder"],
                    "created_at": row["created_at"],
                }
        except sqlite3.Error as e:
            self.logger.error("get_identity failed: %s", e)
            return None

    @staticmethod
    def _normalize_identity_name(display_name: str) -> str:
        return (display_name or "").strip().lower()

    def find_identity_by_display_name(
        self, display_name: str
    ) -> Optional[Dict[str, Any]]:
        """Return identity row if another identity already uses this name (case-insensitive)."""
        key = self._normalize_identity_name(display_name)
        if not key:
            return None
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    """
                    SELECT id, display_name, gallery_folder, created_at FROM identities
                    WHERE LOWER(TRIM(display_name)) = ?
                    LIMIT 1
                    """,
                    (key,),
                ).fetchone()
                if not row:
                    return None
                return {
                    "id": row["id"],
                    "display_name": row["display_name"],
                    "gallery_folder": row["gallery_folder"],
                    "created_at": row["created_at"],
                }
        except sqlite3.Error as e:
            self.logger.error("find_identity_by_display_name failed: %s", e)
            return None

    def get_identity_by_gallery_folder(self, folder: str) -> Optional[Dict[str, Any]]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT id, display_name, gallery_folder, created_at FROM identities WHERE gallery_folder = ?",
                    (folder,),
                ).fetchone()
                if not row:
                    return None
                return {
                    "id": row["id"],
                    "display_name": row["display_name"],
                    "gallery_folder": row["gallery_folder"],
                    "created_at": row["created_at"],
                }
        except sqlite3.Error as e:
            self.logger.error("get_identity_by_gallery_folder failed: %s", e)
            return None

    def create_identity(self, display_name: str, gallery_root: Path) -> Dict[str, Any]:
        """Create a new identity row and empty gallery subfolder (unique ``gallery_folder`` slug)."""
        if self.find_identity_by_display_name(display_name):
            raise ValueError(
                "An identity with this display name already exists (names are unique)."
            )
        gallery_root = Path(gallery_root).resolve()
        gallery_root.mkdir(parents=True, exist_ok=True)
        iid = str(uuid.uuid4())
        base = self._gallery_folder_slug(display_name)
        # One folder per identity: slug + first UUID segment (no Andrea_1, Andrea_2 suffixes).
        hex8 = iid.replace("-", "")[:8]
        folder = f"{base}_{hex8}"[:64]
        target = gallery_root / folder
        if target.exists():
            raise ValueError(
                "Gallery folder collision; retry or remove stale folder: " + folder
            )
        target.mkdir(parents=True, exist_ok=True)
        created = datetime.now(timezone.utc).isoformat()
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO identities (id, display_name, gallery_folder, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (iid, display_name.strip(), folder, created),
                )
                conn.commit()
        except sqlite3.IntegrityError as e:
            try:
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
            except OSError:
                pass
            err = str(e).lower()
            if "idx_identities_display_name_ci" in err or (
                "unique" in err and "identities" in err
            ):
                raise ValueError(
                    "An identity with this display name already exists (names are unique)."
                ) from e
            raise
        return {
            "id": iid,
            "display_name": display_name.strip(),
            "gallery_folder": folder,
            "created_at": created,
        }

    def update_identity(self, identity_id: str, display_name: str) -> bool:
        other = self.find_identity_by_display_name(display_name)
        if other and str(other.get("id")) != str(identity_id):
            raise ValueError(
                "An identity with this display name already exists (names are unique)."
            )
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute(
                    "UPDATE identities SET display_name = ? WHERE id = ?",
                    (display_name.strip(), identity_id),
                )
                conn.commit()
                return cur.rowcount > 0
        except sqlite3.IntegrityError as e:
            err = str(e).lower()
            if "idx_identities_display_name_ci" in err or (
                "unique" in err and "identities" in err
            ):
                raise ValueError(
                    "An identity with this display name already exists (names are unique)."
                ) from e
            self.logger.error("update_identity integrity error: %s", e)
            return False
        except sqlite3.Error as e:
            self.logger.error("update_identity failed: %s", e)
            return False

    def delete_identity(self, identity_id: str, gallery_root: Path) -> bool:
        row = self.get_identity(identity_id)
        if not row:
            return False
        folder = gallery_root.resolve() / row["gallery_folder"]
        try:
            if folder.is_dir():
                shutil.rmtree(folder, ignore_errors=True)
        except OSError as e:
            self.logger.warning("delete_identity rmtree %s: %s", folder, e)
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM identities WHERE id = ?", (identity_id,))
                conn.commit()
            return True
        except sqlite3.Error as e:
            self.logger.error("delete_identity failed: %s", e)
            return False

    def _count_files_in_identity_folder(
        self, gallery_root: Path, gallery_folder: str
    ) -> int:
        gallery_root = Path(gallery_root).resolve()
        folder = str(gallery_folder or "").strip()
        if not folder:
            return 0
        d = gallery_root / folder
        if not d.is_dir():
            return 0
        return sum(1 for p in d.iterdir() if p.is_file())

    def count_pending_assigned_to_identity(self, identity_id: str) -> int:
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    """
                    SELECT COUNT(*) FROM pending_faces
                    WHERE status = 'assigned' AND assigned_identity_id = ?
                    """,
                    (str(identity_id),),
                ).fetchone()
                return int(row[0]) if row else 0
        except sqlite3.Error as e:
            self.logger.error("count_pending_assigned_to_identity failed: %s", e)
            return 999999

    def delete_identity_if_unused(
        self, gallery_root: Path, identity_id: Optional[str]
    ) -> bool:
        """
        Delete identity row and gallery folder if the folder has no files left and no
        pending_faces rows still assigned to this identity.
        """
        if not identity_id:
            return False
        iid = str(identity_id).strip()
        row = self.get_identity(iid)
        if not row:
            return False
        if (
            self._count_files_in_identity_folder(gallery_root, row["gallery_folder"])
            > 0
        ):
            return False
        if self.count_pending_assigned_to_identity(iid) > 0:
            return False
        return self.delete_identity(iid, gallery_root)

    def insert_pending_face(
        self,
        pending_id: str,
        camera_id: str,
        path_rel: str,
        embedding_hash: Optional[str],
        champion_frame_index: int,
        ttl_days: int,
    ) -> None:
        now = datetime.now(timezone.utc)
        exp = now + timedelta(days=max(1, int(ttl_days)))
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO pending_faces
                (id, camera_id, path_rel, embedding_hash, champion_frame_index, created_at, expires_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'open')
                """,
                (
                    pending_id,
                    camera_id,
                    path_rel,
                    embedding_hash,
                    int(champion_frame_index),
                    now.isoformat(),
                    exp.isoformat(),
                ),
            )
            conn.commit()

    def list_pending_faces(
        self, status: Optional[str] = "open", limit: int = 100
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                for row in conn.execute(
                    """
                    SELECT id, camera_id, path_rel, embedding_hash, champion_frame_index,
                           created_at, expires_at, status, assigned_identity_id, assigned_display_name
                    FROM pending_faces
                    WHERE (? IS NULL OR status = ?)
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (status, status, max(1, min(500, int(limit)))),
                ):
                    out.append(
                        {
                            "id": row["id"],
                            "camera_id": row["camera_id"],
                            "path_rel": row["path_rel"],
                            "embedding_hash": row["embedding_hash"],
                            "champion_frame_index": row["champion_frame_index"],
                            "created_at": row["created_at"],
                            "expires_at": row["expires_at"],
                            "status": row["status"],
                            "assigned_identity_id": row["assigned_identity_id"],
                            "assigned_display_name": row["assigned_display_name"],
                        }
                    )
        except sqlite3.Error as e:
            self.logger.error("list_pending_faces failed: %s", e)
        return out

    def get_pending_face(self, pending_id: str) -> Optional[Dict[str, Any]]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    """
                    SELECT id, camera_id, path_rel, embedding_hash, champion_frame_index,
                           created_at, expires_at, status, assigned_identity_id, assigned_display_name
                    FROM pending_faces WHERE id = ?
                    """,
                    (pending_id,),
                ).fetchone()
                if not row:
                    return None
                return {
                    "id": row["id"],
                    "camera_id": row["camera_id"],
                    "path_rel": row["path_rel"],
                    "embedding_hash": row["embedding_hash"],
                    "champion_frame_index": row["champion_frame_index"],
                    "created_at": row["created_at"],
                    "expires_at": row["expires_at"],
                    "status": row["status"],
                    "assigned_identity_id": row["assigned_identity_id"],
                    "assigned_display_name": row["assigned_display_name"],
                }
        except sqlite3.Error as e:
            self.logger.error("get_pending_face failed: %s", e)
            return None

    def ignore_pending_face(self, pending_id: str) -> bool:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute(
                    "UPDATE pending_faces SET status = 'ignored' WHERE id = ? AND status = 'open'",
                    (pending_id,),
                )
                conn.commit()
                return cur.rowcount > 0
        except sqlite3.Error as e:
            self.logger.error("ignore_pending_face failed: %s", e)
            return False

    def assign_pending_face(
        self,
        pending_id: str,
        *,
        media_root: Path,
        gallery_root: Path,
        identity_id: Optional[str] = None,
        new_display_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Copy pending crop into an identity gallery folder and close the pending row.
        Provide either ``identity_id`` (existing) or ``new_display_name`` (creates identity).
        """
        if bool(identity_id) == bool(new_display_name):
            raise ValueError("Provide exactly one of identity_id or new_display_name")

        pending = self.get_pending_face(pending_id)
        if not pending or pending.get("status") != "open":
            raise ValueError("Pending face not found or not open")

        media_root = Path(media_root).resolve()
        gallery_root = Path(gallery_root).resolve()
        src = (media_root / Path(pending["path_rel"])).resolve()
        src.relative_to(media_root)

        if new_display_name:
            ident = self.create_identity(new_display_name, gallery_root)
            folder = ident["gallery_folder"]
            iid = ident["id"]
            assigned_name = ident["display_name"]
        else:
            row = self.get_identity(str(identity_id))
            if not row:
                raise ValueError("Unknown identity_id")
            folder = row["gallery_folder"]
            iid = row["id"]
            assigned_name = row["display_name"]
            (gallery_root / folder).mkdir(parents=True, exist_ok=True)

        dest_dir = (gallery_root / folder).resolve()
        dest_dir.relative_to(gallery_root)
        dest = dest_dir / f"{pending_id}.jpg"
        shutil.copy2(src, dest)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE pending_faces
                SET status = 'assigned',
                    assigned_identity_id = ?,
                    assigned_display_name = ?
                WHERE id = ? AND status = 'open'
                """,
                (iid, assigned_name, pending_id),
            )
            conn.commit()

        return {
            "ok": True,
            "identity_id": iid,
            "gallery_folder": folder,
            "saved_path": str(dest),
        }

    def locate_assigned_gallery_face(
        self,
        gallery_root: Path,
        pending_id: str,
        assigned_identity_id: Optional[str],
    ) -> Tuple[Path, Dict[str, Any]]:
        """Find ``{pending_id}.jpg`` under a identity gallery folder."""
        gallery_root = Path(gallery_root).resolve()
        pid = str(pending_id).strip()
        if assigned_identity_id:
            row = self.get_identity(str(assigned_identity_id))
            if row:
                p = gallery_root / row["gallery_folder"] / f"{pid}.jpg"
                if p.is_file():
                    return p, row
        for ident in self.list_identities():
            folder = str(ident.get("gallery_folder") or "").strip()
            if not folder:
                continue
            p = gallery_root / folder / f"{pid}.jpg"
            if p.is_file():
                return p, dict(ident)
        raise ValueError("Assigned face image not found under face gallery")

    def reassign_assigned_face(
        self,
        pending_id: str,
        *,
        gallery_root: Path,
        identity_id: Optional[str] = None,
        new_display_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Move an already-assigned face file from one identity gallery folder to another.
        Use when a face was wrongly assigned (e.g. move from Andrea to Paolo).
        """
        if bool(identity_id) == bool(new_display_name):
            raise ValueError("Provide exactly one of identity_id or new_display_name")

        pending = self.get_pending_face(pending_id)
        if not pending:
            raise ValueError("Pending face not found")
        if str(pending.get("status") or "") != "assigned":
            raise ValueError(
                "Only assigned faces can be reassigned (use assign for open pending)"
            )

        gallery_root = Path(gallery_root).resolve()
        src, src_ident = self.locate_assigned_gallery_face(
            gallery_root,
            pending_id,
            pending.get("assigned_identity_id"),
        )

        if new_display_name:
            ident = self.create_identity(new_display_name, gallery_root)
            folder = ident["gallery_folder"]
            iid = ident["id"]
            assigned_name = ident["display_name"]
        else:
            row = self.get_identity(str(identity_id))
            if not row:
                raise ValueError("Unknown identity_id")
            folder = row["gallery_folder"]
            iid = row["id"]
            assigned_name = row["display_name"]
            (gallery_root / folder).mkdir(parents=True, exist_ok=True)

        if str(iid) == str(src_ident.get("id")):
            raise ValueError("Face is already under this identity")

        dest_dir = (gallery_root / folder).resolve()
        dest_dir.relative_to(gallery_root)
        dest = dest_dir / f"{pending_id}.jpg"
        if dest.is_file() and dest.resolve() != src.resolve():
            raise ValueError("Target gallery already contains this face file")

        shutil.move(str(src), str(dest))

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE pending_faces
                SET assigned_identity_id = ?,
                    assigned_display_name = ?
                WHERE id = ?
                """,
                (iid, assigned_name, pending_id),
            )
            conn.commit()

        removed_previous = self.delete_identity_if_unused(
            gallery_root, str(src_ident.get("id"))
        )

        return {
            "ok": True,
            "identity_id": iid,
            "gallery_folder": folder,
            "saved_path": str(dest),
            "previous_identity_id": src_ident.get("id"),
            "previous_identity_removed": removed_previous,
        }

    def unassign_assigned_face(
        self, pending_id: str, *, gallery_root: Path
    ) -> Dict[str, Any]:
        """
        Remove gallery copy, set pending face back to ``open`` (unknown), and drop assignment.
        Deletes the identity row if it no longer has any gallery images or assigned pending rows.
        """
        pending = self.get_pending_face(pending_id)
        if not pending:
            raise ValueError("Pending face not found")
        if str(pending.get("status") or "") != "assigned":
            raise ValueError("Only assigned faces can be unassigned")

        gallery_root = Path(gallery_root).resolve()
        src, src_ident = self.locate_assigned_gallery_face(
            gallery_root,
            pending_id,
            pending.get("assigned_identity_id"),
        )
        prev_id = str(src_ident.get("id") or "")
        try:
            if src.is_file():
                src.unlink()
        except OSError as e:
            self.logger.warning("unassign_assigned_face unlink %s: %s", src, e)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE pending_faces
                SET status = 'open',
                    assigned_identity_id = NULL,
                    assigned_display_name = NULL
                WHERE id = ?
                """,
                (pending_id,),
            )
            conn.commit()

        removed_identity = self.delete_identity_if_unused(gallery_root, prev_id or None)

        return {
            "ok": True,
            "previous_identity_id": prev_id,
            "identity_removed": removed_identity,
        }

    def cleanup_expired_pending_faces(self, media_root: Path) -> int:
        """Mark expired open pending as ignored and delete crop files under ``media_root``."""
        media_root = Path(media_root).resolve()
        now_iso = datetime.now(timezone.utc).isoformat()
        removed = 0
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT id, path_rel FROM pending_faces
                    WHERE status = 'open' AND expires_at < ?
                    """,
                    (now_iso,),
                ).fetchall()
                for row in rows:
                    rel = row["path_rel"]
                    try:
                        p = (media_root / Path(rel)).resolve()
                        p.relative_to(media_root)
                        if p.is_file():
                            p.unlink(missing_ok=True)
                    except (OSError, ValueError):
                        pass
                    conn.execute(
                        "UPDATE pending_faces SET status = 'ignored' WHERE id = ?",
                        (row["id"],),
                    )
                    removed += 1
                conn.commit()
        except sqlite3.Error as e:
            self.logger.error("cleanup_expired_pending_faces failed: %s", e)
        return removed

    def cleanup_old_data(self, days: int = 30) -> int:
        """
        Clean up old data from the database.

        Args:
            days: Number of days to keep

        Returns:
            Number of records deleted
        """
        try:
            cutoff = datetime.now() - timedelta(days=days)
            deleted = 0

            with sqlite3.connect(self.db_path) as conn:
                # Clean old events
                cursor = conn.execute(
                    "DELETE FROM events WHERE timestamp < ?", (cutoff,)
                )
                deleted += cursor.rowcount

                # Clean old metrics (keep only recent snapshots)
                cursor = conn.execute(
                    "DELETE FROM metrics WHERE timestamp < ?", (cutoff,)
                )
                deleted += cursor.rowcount

                conn.commit()

            self.logger.info(f"Cleaned up {deleted} old records")
            return deleted

        except sqlite3.Error as e:
            self.logger.error(f"Failed to cleanup old data: {e}")
            return 0
