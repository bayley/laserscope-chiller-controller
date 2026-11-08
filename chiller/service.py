"""Background poller + state + validated writes for one chiller."""
from __future__ import annotations

import csv
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from . import protocol as p
from .transport import SerialTransport, SimTransport, Transport

log = logging.getLogger("chiller")

# Config fields a user may edit through the advanced editor. "unused" is
# always preserved from what the chiller reports.
EDITABLE_CONFIG = {
    # name: (min, max)   -- protective bounds; firmware itself checks nothing
    "proportional_band": (1, 32767),   # 0 could mean divide-by-zero in firmware
    "integral_time": (0, 32767),
    "derivative_time": (0, 32767),
    "pv_filter": (0, 32767),
    "pv_gain_trim": (-100, 100),       # percent
    "pv_offset_trim": (-32768, 32767),
}

SLOW_POLL_EVERY = 15  # cycles between re-reading B0/A0/I


class WriteError(Exception):
    pass


class ChillerService:
    def __init__(self, *, setpoint_min: float = 15.0, setpoint_max: float = 35.0,
                 poll_interval: float = 1.0, history_seconds: int = 6 * 3600,
                 csv_path: Optional[str] = None, read_only: bool = False,
                 serial_kwargs: Optional[dict] = None):
        self.setpoint_min = setpoint_min
        self.setpoint_max = setpoint_max
        self.poll_interval = poll_interval
        self.read_only = read_only
        self.serial_kwargs = serial_kwargs or {}
        self.csv_path = csv_path

        self._lock = threading.RLock()
        self._transport: Optional[Transport] = None
        self.sim = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_write = 0.0

        maxlen = max(10, int(history_seconds / max(poll_interval, 0.1)))
        self.history: deque = deque(maxlen=maxlen)
        self.events: deque = deque(maxlen=500)
        self._reset_state()

    # ------------------------------------------------------------ state
    def _reset_state(self) -> None:
        self.values: Optional[dict] = None
        self.opto: Optional[int] = None
        self.faults: Optional[int] = None
        self.health: Optional[int] = None
        self.firmware: Optional[int] = None
        self.config: Optional[p.Config] = None
        self.last_ok: Optional[float] = None
        self.last_error: Optional[str] = None
        self.comm_errors = 0
        self.consecutive_failures = 0
        self.polls = 0

    def _event(self, level: str, msg: str) -> None:
        self.events.append({"t": time.time(), "level": level, "msg": msg})
        getattr(log, "warning" if level in ("warn", "error") else "info")(msg)

    # ------------------------------------------------------------ connection
    def connect(self, port: Optional[str] = None, simulate: bool = False) -> None:
        self.disconnect()
        with self._lock:
            if simulate:
                from .simulator import SimulatedChiller
                self.sim = SimulatedChiller()
                self._transport = SimTransport(self.sim)
            else:
                if not port:
                    raise ValueError("no serial port given")
                self._transport = SerialTransport(port, **self.serial_kwargs)
            self._reset_state()
            self._event("info", f"Connected to {self._transport.description}")
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="chiller-poll", daemon=True)
            self._thread.start()

    def disconnect(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=3)
        with self._lock:
            if self._transport:
                self._transport.close()
                self._event("info", f"Disconnected from {self._transport.description}")
            self._transport = None
            self.sim = None
            self._thread = None

    @property
    def connected(self) -> bool:
        return self._transport is not None

    def _tx(self, frame: bytes) -> p.Reply:
        t = self._transport
        if t is None:
            raise p.ProtocolError("not connected")
        return t.transact(frame)

    def read(self, command: str) -> p.Reply:
        return self._tx(p.build_poll(command))

    # ------------------------------------------------------------ polling
    def _run(self) -> None:
        while not self._stop.is_set():
            start = time.monotonic()
            self.poll_once()
            self._stop.wait(max(0.0, self.poll_interval - (time.monotonic() - start)))

    def poll_once(self) -> None:
        try:
            values = p.decode_y(self.read("Y").data)
            opto = p.be_unsigned(self.read("C0").data)
            faults = p.be_unsigned(self.read("B1").data)
            slow = None
            if self.polls % SLOW_POLL_EVERY == 0 or self.config is None:
                slow = (
                    p.be_unsigned(self.read("B0").data),
                    p.be_unsigned(self.read("A0").data),
                    p.Config.from_bytes(self.read("I").data),
                )
        except Exception as exc:  # noqa: BLE001 - keep poller alive
            with self._lock:
                self.comm_errors += 1
                self.consecutive_failures += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                if self.consecutive_failures == 3:
                    self._event("error", f"Chiller not responding ({self.last_error})")
            return

        with self._lock:
            if self.consecutive_failures >= 3:
                self._event("info", "Communication restored")
            self.consecutive_failures = 0
            self.polls += 1
            self.last_ok = time.time()
            self._track_changes(opto, faults, slow)
            self.values, self.opto, self.faults = values, opto, faults
            if slow:
                self.health, self.firmware, self.config = slow
            point = {
                "t": self.last_ok,
                "pv": values["pv_c"],
                "sp": values["setpoint_c"],
                "out": values["pid_output_pct"],
            }
            self.history.append(point)
        self._log_csv(values, opto, faults)

    def _track_changes(self, opto: int, faults: int, slow) -> None:
        if self.faults is not None and faults != self.faults:
            new = faults & ~self.faults
            gone = self.faults & ~faults
            if new:
                self._event("error", "FAULT: " + ", ".join(p.describe_bits(new, p.FAULT_BITS)))
            if gone:
                self._event("info", "Cleared: " + ", ".join(p.describe_bits(gone, p.FAULT_BITS)))
        elif self.faults is None and faults:
            self._event("error", "Active faults: " + ", ".join(p.describe_bits(faults, p.FAULT_BITS)))
        if self.opto is not None:
            changed = opto ^ self.opto
            if changed & 0x100:
                self._event("info", "REMOTE " + ("asserted (run request)" if opto & 0x100 else "removed"))
            if changed & 0x020:
                self._event("info", "Compressor " + ("on" if opto & 0x020 else "off"))
        if slow:
            health, fw, _ = slow
            if health and health != self.health:
                self._event("error", "EEPROM: " + ", ".join(p.describe_bits(health, p.HEALTH_BITS)))
            if fw != p.FIRMWARE_ID_EXPECTED and fw != self.firmware:
                self._event("warn", f"Unexpected firmware ID 0x{fw:X} (expected 0x{p.FIRMWARE_ID_EXPECTED:X})")

    def _log_csv(self, values: dict, opto: int, faults: int) -> None:
        if not self.csv_path:
            return
        try:
            new = False
            try:
                with open(self.csv_path):
                    pass
            except FileNotFoundError:
                new = True
            with open(self.csv_path, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["timestamp", *p.H_FIELDS, "opto", "faults"])
                w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            *[values[k] for k in p.H_FIELDS], f"0x{opto:03X}", f"0x{faults:03X}"])
        except OSError as exc:
            log.warning("CSV logging failed: %s", exc)

    # ------------------------------------------------------------ writes
    def _check_writable(self) -> None:
        if self.read_only:
            raise WriteError("server is running in read-only mode")
        if not self.connected:
            raise WriteError("not connected")
        if time.monotonic() - self._last_write < 2.0:
            raise WriteError("writes are rate-limited (EEPROM); wait a moment")

    def _write_config(self, new: p.Config, what: str) -> p.Config:
        """J writes to EEPROM, then I is read back to verify."""
        reply = self._tx(p.build_write_config(new))
        self._last_write = time.monotonic()
        if reply.data:
            raise WriteError(f"unexpected reply to J: {reply.raw.hex(' ')}")
        readback = p.Config.from_bytes(self.read("I").data)
        if readback != new:
            self._event("error", f"Config write verify FAILED ({what}): wrote {new}, read {readback}")
            raise WriteError("read-back after write did not match")
        with self._lock:
            self.config = readback
        self._event("info", f"Config written: {what}")
        return readback

    def set_setpoint(self, celsius: float) -> dict:
        with self._lock:
            self._check_writable()
            if not (self.setpoint_min <= celsius <= self.setpoint_max):
                raise WriteError(f"setpoint must be within {self.setpoint_min}–{self.setpoint_max} °C")
            x10 = int(round(celsius * 10))
            current = p.Config.from_bytes(self.read("I").data)  # fresh read-modify-write
            if current.setpoint_x10 == x10:
                return {"changed": False, "config": current.to_dict()}
            new = p.Config(**{**current.__dict__, "setpoint_x10": x10})
            cfg = self._write_config(new, f"setpoint {current.setpoint_c:.1f} → {x10 / 10:.1f} °C")
            return {"changed": True, "config": cfg.to_dict()}

    def update_config(self, changes: dict) -> dict:
        with self._lock:
            self._check_writable()
            unknown = set(changes) - set(EDITABLE_CONFIG)
            if unknown:
                raise WriteError(f"fields not editable here: {sorted(unknown)}")
            for k, v in changes.items():
                lo, hi = EDITABLE_CONFIG[k]
                if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
                    raise WriteError(f"{k} must be an integer in [{lo}, {hi}]")
            current = p.Config.from_bytes(self.read("I").data)
            new = p.Config(**{**current.__dict__, **changes})
            if new == current:
                return {"changed": False, "config": current.to_dict()}
            diff = ", ".join(f"{k} {getattr(current, k)}→{getattr(new, k)}"
                             for k in changes if getattr(current, k) != getattr(new, k))
            cfg = self._write_config(new, diff)
            return {"changed": True, "config": cfg.to_dict()}

    # ------------------------------------------------------------ raw console
    def raw(self, command: str) -> dict:
        command = command.strip().upper()
        if command not in p.READ_COMMANDS:
            raise WriteError(f"only read commands allowed: {', '.join(p.READ_COMMANDS)}")
        frame = p.build_poll(command)
        out = {"command": command, "tx": frame.hex(" ")}
        try:
            reply = self._tx(frame)
        except p.NakError:
            out.update(rx="15 00 EB", decoded="NAK")
            return out
        out["rx"] = reply.raw.hex(" ")
        d = reply.data
        if command == "Y":
            out["decoded"] = p.decode_y(d)
        elif command == "I":
            out["decoded"] = p.Config.from_bytes(d).to_dict()
        elif command.startswith("H"):
            out["decoded"] = p.decode_scaled(d)
        elif command == "Z":
            out["decoded"] = "ACK"
        else:
            v = p.be_unsigned(d)
            table = {"C0": p.OPTO_BITS, "B1": p.FAULT_BITS, "B0": p.HEALTH_BITS}.get(command)
            out["decoded"] = {"value": v, "hex": f"0x{v:X}",
                              **({"bits": p.describe_bits(v, table)} if table else {})}
        return out

    # ------------------------------------------------------------ snapshot
    def status(self) -> dict:
        with self._lock:
            now = time.time()
            if not self.connected:
                link = "disconnected"
            elif self.consecutive_failures >= 3 or (self.last_ok is None and self.consecutive_failures):
                link = "no_response"
            elif self.last_ok is None:
                link = "connecting"
            else:
                link = "ok"
            opto_bits = p.decode_bits(self.opto, p.OPTO_BITS) if self.opto is not None else None
            stale = self.last_ok is None or now - self.last_ok > max(5, 5 * self.poll_interval)
            return {
                "time": now,
                "connection": {
                    "state": link,
                    "connected": self.connected,
                    "transport": self._transport.description if self._transport else None,
                    "simulated": self.sim is not None,
                    "port": getattr(self._transport, "port", None),
                    "last_ok": self.last_ok,
                    "stale": stale,
                    "comm_errors": self.comm_errors,
                    "consecutive_failures": self.consecutive_failures,
                    "last_error": self.last_error,
                    "polls": self.polls,
                },
                "values": self.values,
                "opto": None if self.opto is None else {
                    "raw": self.opto, "bits": opto_bits,
                },
                "faults": None if self.faults is None else {
                    "raw": self.faults,
                    "bits": p.decode_bits(self.faults, p.FAULT_BITS),
                    "active": p.describe_bits(self.faults, p.FAULT_BITS),
                },
                "health": None if self.health is None else {
                    "raw": self.health, "active": p.describe_bits(self.health, p.HEALTH_BITS),
                },
                "firmware": None if self.firmware is None else {
                    "raw": self.firmware, "ok": self.firmware == p.FIRMWARE_ID_EXPECTED,
                },
                "config": self.config.to_dict() if self.config else None,
                "derived": None if opto_bits is None else {
                    "run_requested": opto_bits["remote"],
                    "compressor": opto_bits["compressor"],
                    "flow_ok": not opto_bits["no_flow"],
                    "faulted": bool(self.faults),
                },
                "limits": {
                    "setpoint_min": self.setpoint_min,
                    "setpoint_max": self.setpoint_max,
                    "read_only": self.read_only,
                    "editable_config": EDITABLE_CONFIG,
                },
                "sim": self.sim.snapshot() if self.sim else None,
            }

    def history_since(self, seconds: float) -> list[dict]:
        cutoff = time.time() - seconds
        with self._lock:
            return [pt for pt in self.history if pt["t"] >= cutoff]
