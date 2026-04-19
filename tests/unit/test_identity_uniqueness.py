"""Display name uniqueness: merge legacy duplicates + unique index."""

import sqlite3
import tempfile
from pathlib import Path

from spyoncino.interface.memory_manager import MemoryManager


def test_merge_duplicate_identities_and_unique_index():
    td = tempfile.mkdtemp()
    db = Path(td) / "t.db"
    MemoryManager(str(db))
    conn = sqlite3.connect(db)
    conn.execute("DROP INDEX IF EXISTS idx_identities_display_name_ci")
    conn.execute(
        "INSERT INTO identities (id, display_name, gallery_folder, created_at) VALUES (?, ?, ?, ?)",
        ("id-a", "Andrea", "andrea_deadbeef", "2020-01-01T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO identities (id, display_name, gallery_folder, created_at) VALUES (?, ?, ?, ?)",
        ("id-b", "ANDREA", "andrea_cafebabe", "2021-01-01T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO pending_faces (id, camera_id, path_rel, created_at, expires_at, status, assigned_identity_id) "
        "VALUES (?,?,?,?,?,?,?)",
        ("pf1", "cam", "p.jpg", "2024-01-01", "2025-01-01", "assigned", "id-b"),
    )
    conn.commit()
    conn.close()

    MemoryManager(str(db))
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT id FROM identities").fetchall()
    pending = conn.execute(
        "SELECT assigned_identity_id FROM pending_faces WHERE id='pf1'"
    ).fetchone()
    idx = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='idx_identities_display_name_ci'"
    ).fetchone()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "id-a"
    assert pending[0] == "id-a"
    assert idx is not None
