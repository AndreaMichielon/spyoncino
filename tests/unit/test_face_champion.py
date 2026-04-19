"""Unit tests for champion-frame scoring (face post-processing)."""

import numpy as np

from spyoncino.postproc.face_identification import (
    pick_champion_frame_index,
    score_frame_for_champion,
)


def _frame(person_conf: float, area: float, alarmed: bool = True) -> dict:
    """Single alarmed person box with given confidence and axis-aligned area."""
    w = h = int(area**0.5)
    boxes = np.array([[0.0, 0.0, float(w), float(h)]], dtype=np.float32)
    return {
        "frame_index": 0,
        "overlay": np.zeros((10, 10, 3), dtype=np.uint8),
        "boxes": boxes,
        "confidences": np.array([person_conf], dtype=np.float32),
        "labels": ["person"],
        "is_alarmed": [alarmed],
    }


def test_score_frame_none_when_no_person_alarm():
    det = _frame(0.9, 10000.0, alarmed=False)
    assert score_frame_for_champion(det, "confidence") is None


def test_pick_champion_prefers_higher_combined():
    low = _frame(0.5, 10000.0)
    low["frame_index"] = 0
    high = _frame(0.9, 10000.0)
    high["frame_index"] = 1
    idx = pick_champion_frame_index([low, high], "combined")
    assert idx == 1


def test_pick_champion_confidence_policy():
    a = _frame(0.3, 50000.0)
    a["frame_index"] = 0
    b = _frame(0.95, 100.0)
    b["frame_index"] = 1
    assert pick_champion_frame_index([a, b], "confidence") == 1


def test_pick_champion_area_policy():
    a = _frame(0.99, 100.0)
    a["frame_index"] = 2
    b = _frame(0.1, 90000.0)
    b["frame_index"] = 3
    assert pick_champion_frame_index([a, b], "area") == 3
