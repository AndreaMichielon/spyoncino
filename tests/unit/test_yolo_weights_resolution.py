import sys
from pathlib import Path

import pytest

from spyoncino.modules.process.yolo_detector import UltralyticsPredictor


class FakeYOLO:
    def __init__(self, model_spec: str) -> None:
        # Simulate Ultralytics behavior: creating a local file when asked for yolov8n.pt
        if str(model_spec).endswith("yolov8n.pt"):
            Path("yolov8n.pt").write_bytes(b"fake-weights")

    def predict(self, *args, **kwargs):
        return []


@pytest.fixture(autouse=True)
def isolate_cwd(tmp_path, monkeypatch):
    # Run each test in an isolated working directory
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_configured_model_path_copied_from_cache(monkeypatch, isolate_cwd):
    # Arrange: point ultralytics WEIGHTS_DIR to a temp cache containing yolov8n.pt
    fake_cache = isolate_cwd / "ultralytics_cache"
    fake_cache.mkdir(parents=True, exist_ok=True)
    cached_file = fake_cache / "yolov8n.pt"
    cached_file.write_bytes(b"cached-weights")

    # Patch ultralytics symbols used by our predictor
    monkeypatch.setitem(sys.modules, "ultralytics", type("UL", (), {"YOLO": FakeYOLO}))
    monkeypatch.setitem(
        sys.modules,
        "ultralytics.utils",
        type("Uutils", (), {"WEIGHTS_DIR": str(fake_cache)}),
    )

    # Configured path that does not exist yet
    configured_path = Path("config") / "yolov8n.pt"
    assert not configured_path.exists()

    # Act
    UltralyticsPredictor(model_path=str(configured_path))

    # Assert: weights copied into configured path
    assert configured_path.exists()
    assert configured_path.read_bytes() == b"cached-weights"


def test_default_model_path_materializes_to_config_when_no_configured_path(
    monkeypatch, isolate_cwd
):
    # Arrange: empty cache; FakeYOLO will create CWD yolov8n.pt on init
    fake_cache = isolate_cwd / "ultralytics_cache_empty"
    fake_cache.mkdir(parents=True, exist_ok=True)

    monkeypatch.setitem(sys.modules, "ultralytics", type("UL", (), {"YOLO": FakeYOLO}))
    monkeypatch.setitem(
        sys.modules,
        "ultralytics.utils",
        type("Uutils", (), {"WEIGHTS_DIR": str(fake_cache)}),
    )

    target = Path("config") / "yolov8n.pt"
    assert not target.exists()
    assert not (isolate_cwd / "yolov8n.pt").exists()

    # Act
    UltralyticsPredictor(model_path=None)

    # Assert: FakeYOLO created CWD file, predictor copied to config/yolov8n.pt
    assert (isolate_cwd / "yolov8n.pt").exists()
    assert target.exists()
    assert target.read_bytes() == b"fake-weights"
