import pytest

from spyoncino.orchestrator_entrypoint import build_module_sequence


def test_build_module_sequence_respects_skip_and_extras() -> None:
    modules = build_module_sequence(
        preset="sim",
        extra_modules=["clip", "modules.process.yolo_detector"],
        skip_modules=["gif", "modules.process.motion_detector"],
    )

    assert modules[0] == "modules.input.camera_simulator"
    assert "modules.event.gif_builder" not in modules
    assert "modules.process.motion_detector" not in modules
    assert "modules.event.clip_builder" in modules
    assert modules.count("modules.process.yolo_detector") == 1


def test_build_module_sequence_unknown_module_raises() -> None:
    with pytest.raises(ValueError):
        build_module_sequence("sim", extra_modules=["nonexistent"], skip_modules=None)
