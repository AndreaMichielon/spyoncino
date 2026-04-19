"""Detector weights path resolution (data/weights, legacy copy)."""

from pathlib import Path

from spyoncino.inference import object_detection as od
from spyoncino.inference.object_detection import ensure_detector_weights_file

_VALID = b"x" * od._MIN_PT_BYTES


def test_ensure_weights_uses_existing_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = tmp_path / "data" / "weights" / "yolov8n.pt"
    p.parent.mkdir(parents=True)
    p.write_bytes(_VALID)
    out = ensure_detector_weights_file(str(p))
    assert out == str(p.resolve())


def test_ensure_weights_copies_from_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "yolov8n.pt").write_bytes(_VALID)
    target = tmp_path / "data" / "weights" / "yolov8n.pt"
    out = ensure_detector_weights_file(str(target))
    assert Path(out).read_bytes() == _VALID
    assert Path(out) == target.resolve()
