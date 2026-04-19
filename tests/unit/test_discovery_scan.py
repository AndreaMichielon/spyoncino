"""Unit tests for camera discovery helpers."""

from fastapi.testclient import TestClient

from spyoncino.discovery_app import app
from spyoncino.discovery_scan import (
    RTSP_PATH_TEMPLATES_ALL,
    build_rtsp_url,
    mask_rtsp_url_for_display,
    parse_host_list,
    plan_network_scan,
    rtsp_failure_hint,
)


def test_rtsp_templates_all_merged() -> None:
    assert len(RTSP_PATH_TEMPLATES_ALL) >= 30


def test_plan_network_scan() -> None:
    eff, w = plan_network_scan(True, True, [])
    assert eff is False and w is not None and "skipped" in w.lower()
    assert plan_network_scan(True, True, ["192.168.1.1"]) == (True, None)
    assert plan_network_scan(True, False, []) == (False, None)
    assert plan_network_scan(False, True, []) == (False, None)
    assert plan_network_scan(False, True, [], True) == (True, None)


def test_parse_host_list() -> None:
    assert parse_host_list("192.168.1.1\n192.168.1.2") == ["192.168.1.1", "192.168.1.2"]
    assert parse_host_list("10.0.0.1, 10.0.0.2") == ["10.0.0.1", "10.0.0.2"]
    assert parse_host_list("") == []


def test_build_rtsp_url_no_auth() -> None:
    u = build_rtsp_url("192.168.1.10", 554, "/stream1")
    assert u == "rtsp://192.168.1.10:554/stream1"


def test_build_rtsp_url_auth() -> None:
    u = build_rtsp_url("192.168.1.10", 554, "/live", "admin", "secret:pass")
    assert u.startswith("rtsp://")
    assert "192.168.1.10:554/live" in u
    assert "secret" in u


def test_rtsp_failure_hint_credentials() -> None:
    h = rtsp_failure_hint("not_opened", had_credentials=True)
    assert (
        "password" in h.lower() or "credentials" in h.lower() or "opencv" in h.lower()
    )


def test_rtsp_failure_hint_no_cred() -> None:
    h = rtsp_failure_hint("no_frame", had_credentials=False)
    assert "frame" in h.lower()


def test_mask_rtsp_url() -> None:
    m = mask_rtsp_url_for_display("rtsp://admin:hunter2@192.168.1.1:554/stream1")
    assert "hunter2" not in m
    assert "***" in m


def test_discover_root_serves_page() -> None:
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    assert "Camera discovery" in r.text
    assert "/api/discover/run" in r.text


def test_discover_run_requires_option() -> None:
    client = TestClient(app)
    r = client.post("/api/discover/run", json={"usb": False, "network": False})
    assert r.status_code == 400


def test_discover_network_only_without_hosts_400() -> None:
    client = TestClient(app)
    r = client.post(
        "/api/discover/run",
        json={"usb": False, "network": True, "hosts_text": ""},
    )
    assert r.status_code == 400
