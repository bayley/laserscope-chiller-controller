"""Lydall chiller RS-485 protocol: framing, checksums and decoding.

Reference: https://directed-energy.net/laser/ams_lydallchiller.html

Link: 38400 baud, 8N1, half-duplex RS-485, no addressing (chiller is a
pure slave and only talks when polled).

    poll  (master -> chiller):  40 42 30 <len> <ASCII command...> <cs>
    reply (chiller -> master):  06 <len> <big-endian data...> <cs>
    NAK                         15 00 EB
    bad checksum                (no reply at all)

Checksum: every byte of a frame, including the checksum, sums to 0 mod 256.

This module is pure (no I/O) so it can be unit-tested and reused by the
simulator.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, asdict
from typing import Optional

POLL_PREFIX = b"\x40\x42\x30"
ACK = 0x06
NAK = 0x15

BAUDRATE = 38400

# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

# Read-only commands. Safe to send at any time.
READ_COMMANDS: dict[str, str] = {
    "C0": "Opto inputs (debounced bitfield)",
    "B1": "Fault word (0 = healthy)",
    "B0": "Stored-data health (EEPROM CRCs)",
    "A0": "Firmware ID (expected 0x158)",
    "H0": "Setpoint (degC x1000)",
    "H1": "Process temperature PV (degC x1000)",
    "H2": "PID output (% x1000)",
    "H3": "PID proportional term (x1000)",
    "H4": "PID integral term (x1000)",
    "H5": "PID derivative term (x1000)",
    "H6": "Error PV-SP (degC x1000)",
    "H7": "Integral accumulator (x1000)",
    "H8": "Delta error since last PID update (x1000)",
    "Y": "H0..H8 in one 36-byte reply",
    "I": "Configuration block (8 x int16)",
    "Z": "Keepalive (empty ACK)",
}

# The one and only write command. Persists to EEPROM; firmware does NO range
# checking. Only exposed through the validated config/setpoint path.
WRITE_CONFIG_CMD = "J"

H_FIELDS = [
    "setpoint_c",        # H0
    "pv_c",              # H1
    "pid_output_pct",    # H2
    "p_term",            # H3
    "i_term",            # H4
    "d_term",            # H5
    "error_c",           # H6
    "integral_acc",      # H7
    "delta_error",       # H8
]

FIRMWARE_ID_EXPECTED = 0x158

# --------------------------------------------------------------------------
# Bit definitions
# --------------------------------------------------------------------------

# C0 opto inputs: (bit mask, key, label, "active is bad?")
OPTO_BITS = [
    (0x001, "no_flow", "FLOW (set = no flow)", True),
    (0x002, "level_fault", "LEVEL fault", True),
    (0x004, "level_alarm", "Level alarm", True),
    (0x008, "hi_pres", "HI PRES", True),
    (0x010, "lo_pres", "LO PRES", True),
    (0x020, "compressor", "LEVEL1 out / compressor", False),
    (0x040, "set", "SET", False),
    (0x080, "run", "RUN", False),
    (0x100, "remote", "REMOTE (run request)", False),
    (0x200, "spare", "SPARE", False),
]

# B1 fault word: (bit mask, key, label)
FAULT_BITS = [
    (0x001, "low_flow", "Low flow"),
    (0x002, "low_level", "Low level"),
    (0x008, "high_pressure", "High pressure"),
    (0x010, "low_pressure", "Low pressure"),
    (0x040, "loop_under_4c", "Loop below 4 °C"),
    (0x080, "loop_over_40c", "Loop above 40 °C"),
    (0x100, "rtd_failure", "RTD read failure"),
]

# B0 stored-data health
HEALTH_BITS = [
    (0x01, "rtd_cal_crc_bad", "RTD calibration CRC bad"),
    (0x02, "config_crc_bad", "PID/config CRC bad (chiller will not run)"),
]


def decode_bits(value: int, table) -> dict:
    return {entry[1]: bool(value & entry[0]) for entry in table}


def describe_bits(value: int, table) -> list[str]:
    return [entry[2] for entry in table if value & entry[0]]


# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------

class ProtocolError(Exception):
    pass


class NakError(ProtocolError):
    """Chiller replied 15 00 EB: unknown command."""


class ChecksumError(ProtocolError):
    pass


def checksum(data: bytes) -> int:
    """Byte that makes sum(data + cs) == 0 mod 256."""
    return (-sum(data)) & 0xFF


def frame_ok(frame: bytes) -> bool:
    return sum(frame) & 0xFF == 0


def build_poll(command: str, payload: bytes = b"") -> bytes:
    body = command.encode("ascii") + payload
    if len(body) > 255:
        raise ValueError("command too long")
    head = POLL_PREFIX + bytes([len(body)]) + body
    return head + bytes([checksum(head)])


def build_reply(data: bytes) -> bytes:
    head = bytes([ACK, len(data)]) + data
    return head + bytes([checksum(head)])


NAK_FRAME = bytes([NAK, 0x00, checksum(bytes([NAK, 0x00]))])  # 15 00 EB


@dataclass
class Reply:
    data: bytes
    raw: bytes


def extract_reply(buf: bytes, sent: bytes = b"") -> tuple[Optional[Reply], int, bool]:
    """Try to pull one reply frame out of ``buf``.

    Returns (reply_or_None, bytes_consumed, need_more).

    * Strips a local echo of ``sent`` (many USB-RS485 adapters echo TX).
    * Skips line noise that is not a valid header byte.
    * Raises NakError on a NAK frame, ChecksumError on a corrupt frame.
    """
    i = 0
    # Local echo: replies only ever start with 06/15, polls with 40.
    if sent:
        if len(buf) < len(sent) and sent.startswith(buf):
            return None, 0, True
        if buf.startswith(sent):
            i = len(sent)
    # Skip junk until a plausible header byte.
    while i < len(buf) and buf[i] not in (ACK, NAK):
        i += 1
    if i + 2 > len(buf):
        return None, i, True
    length = buf[i + 1]
    end = i + 2 + length + 1
    if end > len(buf):
        return None, i, True
    frame = bytes(buf[i:end])
    if not frame_ok(frame):
        raise ChecksumError(f"bad checksum in reply {frame.hex(' ')}")
    if frame[0] == NAK:
        raise NakError("chiller NAK (unknown command)")
    return Reply(data=frame[2:-1], raw=frame), end, False


def extract_poll(buf: bytes) -> tuple[Optional[bytes], int]:
    """Simulator side: find one complete poll frame. Returns (frame, consumed)."""
    i = buf.find(POLL_PREFIX)
    if i < 0:
        # keep a possible partial prefix at the tail
        keep = 0
        for k in (2, 1):
            if len(buf) >= k and POLL_PREFIX.startswith(buf[-k:]):
                keep = k
                break
        return None, len(buf) - keep
    if i + 4 > len(buf):
        return None, i
    length = buf[i + 3]
    end = i + 4 + length + 1
    if end > len(buf):
        return None, i
    return bytes(buf[i:end]), end


# --------------------------------------------------------------------------
# Value decoding
# --------------------------------------------------------------------------

def be_unsigned(data: bytes) -> int:
    return int.from_bytes(data, "big", signed=False) if data else 0


def be_signed(data: bytes) -> int:
    return int.from_bytes(data, "big", signed=True) if data else 0


def decode_scaled(data: bytes) -> float:
    """H registers: signed 32-bit big-endian, scaled x1000."""
    return be_signed(data) / 1000.0


def decode_y(data: bytes) -> dict:
    if len(data) != 36:
        raise ProtocolError(f"Y reply should be 36 bytes, got {len(data)}")
    values = struct.unpack(">9i", data)
    return {name: v / 1000.0 for name, v in zip(H_FIELDS, values)}


# --------------------------------------------------------------------------
# Configuration block (I / J)
# --------------------------------------------------------------------------

CONFIG_FIELDS = [
    "proportional_band",
    "integral_time",
    "derivative_time",
    "unused",
    "pv_filter",
    "pv_gain_trim",
    "pv_offset_trim",
    "setpoint_x10",
]


@dataclass
class Config:
    proportional_band: int = 0
    integral_time: int = 0
    derivative_time: int = 0
    unused: int = 0
    pv_filter: int = 0
    pv_gain_trim: int = 0
    pv_offset_trim: int = 0
    setpoint_x10: int = 0

    @property
    def setpoint_c(self) -> float:
        return self.setpoint_x10 / 10.0

    @classmethod
    def from_bytes(cls, data: bytes) -> "Config":
        if len(data) != 16:
            raise ProtocolError(f"config block should be 16 bytes, got {len(data)}")
        return cls(*struct.unpack(">8h", data))

    def to_bytes(self) -> bytes:
        vals = [getattr(self, f) for f in CONFIG_FIELDS]
        for name, v in zip(CONFIG_FIELDS, vals):
            if not -32768 <= v <= 32767:
                raise ValueError(f"{name}={v} does not fit in int16")
        return struct.pack(">8h", *vals)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["setpoint_c"] = self.setpoint_c
        return d


def build_write_config(cfg: Config) -> bytes:
    return build_poll(WRITE_CONFIG_CMD, cfg.to_bytes())
