"""Transports: a real USB-serial/RS-485 port, or an in-process simulator."""
from __future__ import annotations

import threading
import time
from typing import Optional, Protocol

from . import protocol as p


class Transport(Protocol):
    description: str

    def transact(self, frame: bytes) -> p.Reply: ...
    def close(self) -> None: ...


class TimeoutError_(p.ProtocolError):
    """No (complete) reply within the timeout."""


class SerialTransport:
    """Half-duplex request/response over a pyserial port.

    Most USB-RS485 adapters (FTDI/CH340 + auto-direction) need nothing
    special. Adapters that use RTS for driver-enable can set ``rs485_rts``.
    Local echo of our own TX is detected and discarded automatically.
    """

    def __init__(self, port: str, baudrate: int = p.BAUDRATE, timeout: float = 0.3,
                 retries: int = 2, rs485_rts: bool = False, inter_frame_gap: float = 0.005):
        import serial  # local import so the simulator works without pyserial

        self.port = port
        self.timeout = timeout
        self.retries = retries
        self.inter_frame_gap = inter_frame_gap
        self._lock = threading.Lock()
        self._ser = serial.Serial(
            port=port, baudrate=baudrate, bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
            timeout=0.02, write_timeout=1.0,
        )
        if rs485_rts:
            from serial.rs485 import RS485Settings
            self._ser.rs485_mode = RS485Settings(rts_level_for_tx=True, rts_level_for_rx=False)
        self.description = f"{port} @ {baudrate} 8N1"

    def transact(self, frame: bytes) -> p.Reply:
        last_exc: Exception = TimeoutError_("no reply")
        with self._lock:
            for _ in range(self.retries + 1):
                try:
                    return self._once(frame)
                except p.NakError:
                    raise  # deterministic; retrying won't help
                except (TimeoutError_, p.ChecksumError) as exc:
                    last_exc = exc
                    time.sleep(0.02)
        raise last_exc

    def _once(self, frame: bytes) -> p.Reply:
        ser = self._ser
        ser.reset_input_buffer()
        ser.write(frame)
        ser.flush()
        buf = bytearray()
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            chunk = ser.read(max(1, ser.in_waiting))
            if chunk:
                buf += chunk
                reply, _, need_more = p.extract_reply(bytes(buf), sent=frame)
                if reply is not None:
                    time.sleep(self.inter_frame_gap)
                    return reply
        raise TimeoutError_(
            f"no reply to {frame.hex(' ')}" + (f" (got {bytes(buf).hex(' ')})" if buf else "")
        )

    def close(self) -> None:
        try:
            self._ser.close()
        except Exception:
            pass


class SimTransport:
    """Talks bytes to an in-process ``SimulatedChiller``, exercising the same
    framing/parsing path as the real port."""

    def __init__(self, sim):
        self.sim = sim
        self._lock = threading.Lock()
        self.description = "built-in simulator"

    def transact(self, frame: bytes) -> p.Reply:
        with self._lock:
            out = self.sim.handle_frame(frame)
            if out is None:
                raise TimeoutError_(f"no reply to {frame.hex(' ')}")
            reply, _, _ = p.extract_reply(out)
            if reply is None:
                raise TimeoutError_("incomplete reply from simulator")
            return reply

    def close(self) -> None:
        pass


def list_serial_ports() -> list[dict]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    ports = []
    for info in sorted(list_ports.comports(), key=lambda i: i.device):
        ports.append({
            "device": info.device,
            "description": info.description or "",
            "hwid": info.hwid or "",
        })
    return ports
