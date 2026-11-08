"""Service + API tests against the simulator, including a real-serial pty run."""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chiller import protocol as p
from chiller.service import ChillerService, WriteError
from server import create_app

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def svc():
    s = ChillerService(poll_interval=0.05)
    s.connect(simulate=True)
    s._stop.set()          # drive polling manually for determinism
    s._thread.join()
    s.poll_once()
    yield s
    s.disconnect()


def test_poll_populates_state(svc):
    st = svc.status()
    assert st["connection"]["state"] == "ok"
    assert st["values"]["setpoint_c"] == pytest.approx(26.1)
    assert st["firmware"] == {"raw": 0x158, "ok": True}
    assert st["config"]["proportional_band"] == 80
    assert st["derived"]["run_requested"] is True


def test_setpoint_write_and_verify(svc):
    svc._last_write = 0
    r = svc.set_setpoint(22.04)
    assert r["changed"] and r["config"]["setpoint_x10"] == 220
    assert svc.sim.config.setpoint_x10 == 220
    assert svc.sim.config.proportional_band == 80     # other fields preserved
    assert svc.sim.eeprom_writes == 1
    # no-op write is skipped (saves EEPROM wear) even after rate limit window
    svc._last_write = 0
    assert svc.set_setpoint(22.0)["changed"] is False
    assert svc.sim.eeprom_writes == 1


def test_setpoint_limits_and_rate_limit(svc):
    with pytest.raises(WriteError):
        svc.set_setpoint(50)
    with pytest.raises(WriteError):
        svc.set_setpoint(2)
    svc.set_setpoint(20)
    with pytest.raises(WriteError, match="rate-limited"):
        svc.set_setpoint(21)


def test_read_only_mode():
    s = ChillerService(read_only=True)
    s.connect(simulate=True)
    try:
        with pytest.raises(WriteError, match="read-only"):
            s.set_setpoint(20)
    finally:
        s.disconnect()


def test_config_validation(svc):
    with pytest.raises(WriteError):
        svc.update_config({"proportional_band": 0})
    with pytest.raises(WriteError):
        svc.update_config({"unused": 5})
    with pytest.raises(WriteError):
        svc.update_config({"setpoint_x10": 900})   # must go via set_setpoint
    r = svc.update_config({"integral_time": 6})
    assert r["changed"] and svc.sim.config.integral_time == 6


def test_fault_latching_events(svc):
    svc.sim.inject("high_pressure")
    svc.poll_once()
    st = svc.status()
    assert st["faults"]["bits"]["high_pressure"]
    assert st["derived"]["faulted"]
    svc.sim.clear_injected()
    svc.poll_once()
    assert svc.status()["faults"]["bits"]["high_pressure"]   # still latched
    svc.sim.set_remote(False)
    svc.poll_once()
    assert svc.status()["faults"]["raw"] == 0
    msgs = [e["msg"] for e in svc.events]
    assert any("FAULT: High pressure" in m for m in msgs)
    assert any("REMOTE removed" in m for m in msgs)


def test_no_response_detection(svc):
    svc.sim.handle_frame = lambda f: None
    for _ in range(3):
        svc.poll_once()
    assert svc.status()["connection"]["state"] == "no_response"


def test_raw_console_read_only(svc):
    assert svc.raw("a0")["decoded"]["value"] == 0x158
    assert svc.raw("C0")["tx"] == "40 42 30 02 43 30 d9"
    with pytest.raises(WriteError):
        svc.raw("J")


def test_api_roundtrip():
    s = ChillerService(poll_interval=0.05)
    with TestClient(create_app(s, {"simulate": True})) as c:
        time.sleep(0.3)
        st = c.get("/api/status").json()
        assert st["connection"]["state"] == "ok"
        assert len(c.get("/api/history?seconds=60").json()) > 0
        assert c.post("/api/setpoint", json={"celsius": 99}).status_code == 400
        r = c.post("/api/setpoint", json={"celsius": 24.5})
        assert r.status_code == 200 and r.json()["config"]["setpoint_x10"] == 245
        assert c.post("/api/config", json={"changes": {"pv_filter": 1}}).status_code == 400  # no confirm
        assert c.post("/api/raw", json={"command": "Z"}).json()["rx"] == "06 00 fa"
        assert c.post("/api/sim", json={"inject": "bogus"}).status_code == 400
        assert c.get("/").status_code == 200
        assert c.get("/static/app.js").status_code == 200


@pytest.mark.skipif(sys.platform == "win32", reason="pty only on POSIX")
def test_end_to_end_over_pty(tmp_path):
    """Real SerialTransport (pyserial) talking to the simulator over a pty."""
    link = tmp_path / "ttySIM"
    proc = subprocess.Popen([sys.executable, "-m", "chiller.simulator", "--link", str(link)],
                            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)
    try:
        for _ in range(50):
            if link.exists():
                break
            time.sleep(0.1)
        s = ChillerService(poll_interval=0.1)
        s.connect(port=str(link))
        try:
            time.sleep(0.8)
            st = s.status()
            assert st["connection"]["state"] == "ok", st["connection"]
            assert st["firmware"]["ok"]
            s._last_write = 0
            assert s.set_setpoint(23.3)["config"]["setpoint_x10"] == 233
            assert s.raw("H0")["decoded"] == pytest.approx(23.3)
        finally:
            s.disconnect()
    finally:
        proc.terminate()
        proc.wait(5)
