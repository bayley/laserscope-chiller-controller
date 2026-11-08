"""A crude simulated Lydall chiller that speaks the wire protocol.

Two ways to use it:

* In-process: ``server.py --simulate`` (uses SimTransport, no serial at all).
* Over a pseudo-terminal, to exercise the real pyserial path end to end:

      python -m chiller.simulator --link /tmp/ttyCHILLER
      python server.py --port /tmp/ttyCHILLER

  Type commands on the simulator's stdin: ``remote on|off``,
  ``fault <name>``, ``clear``, ``status``. Fault names are the B1 keys in
  protocol.FAULT_BITS (low_flow, low_level, high_pressure, ...).

The thermal model and the PID are invented and only meant to look plausible.
Unknown reply widths (C0/B1/B0/A0) are sent as 16-bit values.
"""
from __future__ import annotations

import argparse
import os
import random
import select
import struct
import sys
import threading
import time
import tty

from . import protocol as p

AMBIENT_C = 24.0


class SimulatedChiller:
    def __init__(self, remote: bool = True, setpoint_c: float = 26.1):
        self.lock = threading.RLock()
        self.config = p.Config(80, 4, 2, 0, 0, -4, 0, int(round(setpoint_c * 10)))
        self.remote = remote
        self.pv = AMBIENT_C + 3.0
        self.compressor = False
        self.integral = 0.0
        self.last_err = 0.0
        self.terms = (0.0, 0.0, 0.0)
        self.output = 0.0
        self.delta_err = 0.0
        self.run_time = 0.0
        self.injected: set[str] = set()   # physical conditions present
        self.faults = 0                   # latched B1 word
        self.health = 0
        self.eeprom_writes = 0
        self.frames_handled = 0
        self._t = time.monotonic()

    # ----------------------------------------------------------- physics
    def _running(self) -> bool:
        return (self.remote and self.faults & ~0x100 == 0
                and not self.health & 0x02)

    def step(self) -> None:
        with self.lock:
            now = time.monotonic()
            dt = min(now - self._t, 2.0)
            self._t = now
            if dt <= 0:
                return
            sp = self.config.setpoint_c
            running = self._running()
            if running:
                self.run_time += dt
            else:
                self.run_time = 0.0
                self.integral = 0.0

            err = self.pv - sp
            if running:
                # Compressor: coarse bang-bang with hysteresis.
                if err > 0.4:
                    self.compressor = True
                elif err < -0.4:
                    self.compressor = False
                # Heater PID: fine trim (heats when PV is below SP).
                band = max(self.config.proportional_band, 1) / 100.0  # degC
                pt = -err / band * 100.0
                self.integral = max(-50.0, min(50.0, self.integral - err * dt * 2.0))
                it = self.integral
                dterm = -(err - self.last_err) / dt * self.config.derivative_time
                self.terms = (pt, it, dterm)
                self.output = max(0.0, min(100.0, 35.0 + pt + it + dterm))
            else:
                self.compressor = False
                self.output = 0.0
                self.terms = (0.0, 0.0, 0.0)
            self.delta_err = err - self.last_err
            self.last_err = err

            # Thermal model (degC/s).
            load = 0.012 if running else 0.0
            dpv = (load
                   + 0.025 * (self.output / 100.0)
                   - (0.035 if self.compressor else 0.0)
                   + 0.002 * (AMBIENT_C - self.pv))
            self.pv += dpv * dt

            # Fault detection (latching, except RTD).
            if running or self.remote:
                if "low_flow" in self.injected and self.run_time > 10:
                    self.faults |= 0x001
                for key, mask in (("low_level", 0x002), ("high_pressure", 0x008),
                                  ("low_pressure", 0x010)):
                    if key in self.injected:
                        self.faults |= mask
                if self.pv < 4.0:
                    self.faults |= 0x040
                if self.pv > 40.0:
                    self.faults |= 0x080
            if "rtd_failure" in self.injected:
                self.faults |= 0x100
            else:
                self.faults &= ~0x100
            if not self.remote:
                self.faults &= 0x100  # removing REMOTE clears latched faults

    def opto_word(self) -> int:
        w = 0x200  # SPARE reads high on real units (typical 0x320)
        flowing = self._running() and "low_flow" not in self.injected
        if not flowing:
            w |= 0x001
        if "low_level" in self.injected:
            w |= 0x002 | 0x004
        if "high_pressure" in self.injected:
            w |= 0x008
        if "low_pressure" in self.injected:
            w |= 0x010
        if self.compressor:
            w |= 0x020
        if self.remote:
            w |= 0x100
        return w

    def h_values(self) -> list[float]:
        sp = self.config.setpoint_c
        pv = self.pv + random.gauss(0, 0.005)
        pt, it, dterm = self.terms
        return [sp, pv, self.output, pt, it, dterm, pv - sp, self.integral, self.delta_err]

    # ----------------------------------------------------------- protocol
    def handle_frame(self, frame: bytes) -> bytes | None:
        """Return the reply bytes for one poll frame, or None for silence."""
        self.step()
        with self.lock:
            self.frames_handled += 1
            if not p.frame_ok(frame) or not frame.startswith(p.POLL_PREFIX) or len(frame) < 5:
                return None  # real unit is silent on bad checksum
            body = frame[4:-1]
            if len(body) != frame[3]:
                return None
            cmd, rest = chr(body[0]), body[1:]

            def u16(v: int) -> bytes:
                return p.build_reply(struct.pack(">H", v & 0xFFFF))

            if cmd == "Y" and not rest:
                vals = [int(round(v * 1000)) for v in self.h_values()]
                return p.build_reply(struct.pack(">9i", *vals))
            if cmd == "I" and not rest:
                return p.build_reply(self.config.to_bytes())
            if cmd == "Z" and not rest:
                return p.build_reply(b"")
            if cmd == "J" and len(rest) == 16:
                self.config = p.Config.from_bytes(rest)
                self.eeprom_writes += 1
                return p.build_reply(b"")
            if len(rest) == 1:
                sub = chr(rest[0])
                if cmd == "C" and sub == "0":
                    return u16(self.opto_word())
                if cmd == "B" and sub == "1":
                    return u16(self.faults)
                if cmd == "B" and sub == "0":
                    return u16(self.health)
                if cmd == "A" and sub == "0":
                    return u16(p.FIRMWARE_ID_EXPECTED)
                if cmd == "H" and sub in "012345678":
                    v = self.h_values()[int(sub)]
                    return p.build_reply(struct.pack(">i", int(round(v * 1000))))
            return p.NAK_FRAME

    # ----------------------------------------------------------- controls
    def set_remote(self, on: bool) -> None:
        with self.lock:
            self.remote = on
            self.step()

    def inject(self, key: str, present: bool = True) -> None:
        valid = {k for _, k, _ in p.FAULT_BITS}
        if key not in valid:
            raise ValueError(f"unknown fault {key!r}; choose from {sorted(valid)}")
        with self.lock:
            (self.injected.add if present else self.injected.discard)(key)

    def clear_injected(self) -> None:
        with self.lock:
            self.injected.clear()

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "remote": self.remote,
                "injected": sorted(self.injected),
                "eeprom_writes": self.eeprom_writes,
                "pv_true": round(self.pv, 3),
            }


# ----------------------------------------------------------------- pty mode

def _serve_pty(sim: SimulatedChiller, link: str | None) -> None:
    master, slave = os.openpty()
    tty.setraw(slave)
    slave_name = os.ttyname(slave)
    if link:
        try:
            os.unlink(link)
        except FileNotFoundError:
            pass
        os.symlink(slave_name, link)
    print(f"Simulated chiller on {slave_name}" + (f" (symlink {link})" if link else ""), flush=True)
    print("stdin commands: remote on|off, fault <name>, clear, status, quit", flush=True)

    buf = b""
    watch = [master, sys.stdin]
    try:
        while True:
            r, _, _ = select.select(watch, [], [], 0.5)
            if master in r:
                buf += os.read(master, 1024)
                while True:
                    frame, consumed = p.extract_poll(buf)
                    buf = buf[consumed:]
                    if frame is None:
                        break
                    reply = sim.handle_frame(frame)
                    if reply:
                        time.sleep(0.002)  # small turnaround like a real MCU
                        os.write(master, reply)
            if sys.stdin in r:
                line = sys.stdin.readline()
                if not line:  # EOF (e.g. run in background): stop watching
                    watch = [master]
                    continue
                parts = line.split()
                try:
                    if not parts:
                        pass
                    elif parts[0] == "remote":
                        sim.set_remote(parts[1] == "on")
                    elif parts[0] == "fault":
                        sim.inject(parts[1])
                    elif parts[0] == "clear":
                        sim.clear_injected()
                    elif parts[0] == "quit":
                        break
                    print(sim.snapshot(), flush=True)
                except (IndexError, ValueError) as exc:
                    print(f"error: {exc}", flush=True)
            sim.step()
    finally:
        if link:
            try:
                os.unlink(link)
            except OSError:
                pass


def main() -> None:
    ap = argparse.ArgumentParser(description="Simulated Lydall chiller on a pty")
    ap.add_argument("--link", help="create a symlink to the pty at this path")
    ap.add_argument("--remote-off", action="store_true", help="start with REMOTE de-asserted")
    args = ap.parse_args()
    _serve_pty(SimulatedChiller(remote=not args.remote_off), args.link)


if __name__ == "__main__":
    main()
