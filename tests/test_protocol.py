"""Protocol tests against the example frames in the reference document."""
import struct

import pytest

from chiller import protocol as p

# Poll frames exactly as listed in the documentation.
DOC_POLLS = {
    "C0": "40 42 30 02 43 30 D9",
    "B1": "40 42 30 02 42 31 D9",
    "B0": "40 42 30 02 42 30 DA",
    "A0": "40 42 30 02 41 30 DB",
    "H0": "40 42 30 02 48 30 D4",
    "H1": "40 42 30 02 48 31 D3",
    "H2": "40 42 30 02 48 32 D2",
    "H3": "40 42 30 02 48 33 D1",
    "H4": "40 42 30 02 48 34 D0",
    "H5": "40 42 30 02 48 35 CF",
    "H6": "40 42 30 02 48 36 CE",
    "H7": "40 42 30 02 48 37 CD",
    "H8": "40 42 30 02 48 38 CC",
    "Y": "40 42 30 01 59 F4",
    "I": "40 42 30 01 49 04",
    "Z": "40 42 30 01 5A F3",
}

DOC_CONFIG = bytes.fromhex("00 50 00 04 00 02 00 00 00 00 FF FC 00 00 01 05")
DOC_J = "40 42 30 11 4A 00 50 00 04 00 02 00 00 00 00 FF FC 00 00 01 05 9C"


@pytest.mark.parametrize("cmd,expected", DOC_POLLS.items())
def test_poll_frames_match_doc(cmd, expected):
    assert p.build_poll(cmd).hex(" ").upper() == expected


def test_doc_frames_checksum_to_zero():
    for frame in [*DOC_POLLS.values(), DOC_J, "15 00 EB", "06 00 FA"]:
        assert p.frame_ok(bytes.fromhex(frame)), frame


def test_nak_and_empty_ack():
    assert p.NAK_FRAME.hex(" ").upper() == "15 00 EB"
    assert p.build_reply(b"").hex(" ").upper() == "06 00 FA"


def test_config_decode_doc_example():
    cfg = p.Config.from_bytes(DOC_CONFIG)
    assert cfg.proportional_band == 80
    assert cfg.integral_time == 4
    assert cfg.derivative_time == 2
    assert cfg.pv_gain_trim == -4
    assert cfg.setpoint_x10 == 261
    assert cfg.setpoint_c == pytest.approx(26.1)
    assert cfg.to_bytes() == DOC_CONFIG


def test_write_config_matches_doc():
    cfg = p.Config.from_bytes(DOC_CONFIG)
    assert p.build_write_config(cfg).hex(" ").upper() == DOC_J


def test_config_rejects_out_of_range():
    with pytest.raises(ValueError):
        p.Config(setpoint_x10=40000).to_bytes()


def test_decode_y_and_scaled():
    vals = [26100, 25987, 42500, 1, -2, 3, -113, 7, -8]
    data = struct.pack(">9i", *vals)
    y = p.decode_y(data)
    assert y["setpoint_c"] == pytest.approx(26.1)
    assert y["pv_c"] == pytest.approx(25.987)
    assert y["error_c"] == pytest.approx(-0.113)
    assert p.decode_scaled(struct.pack(">i", -1500)) == -1.5
    with pytest.raises(p.ProtocolError):
        p.decode_y(data[:-1])


def test_extract_reply_basic_and_partial():
    frame = p.build_reply(b"\x01\x20")
    for cut in range(len(frame)):
        r, _, need = p.extract_reply(frame[:cut])
        assert r is None and need
    r, used, _ = p.extract_reply(frame)
    assert r.data == b"\x01\x20" and used == len(frame)


def test_extract_reply_strips_echo_and_noise():
    sent = p.build_poll("C0")
    reply = p.build_reply(b"\x03\x20")
    # partial echo -> wait
    assert p.extract_reply(sent[:3], sent=sent)[2] is True
    r, _, _ = p.extract_reply(sent + reply, sent=sent)
    assert r.data == b"\x03\x20"
    r, _, _ = p.extract_reply(b"\x00\xff" + reply, sent=sent)
    assert r.data == b"\x03\x20"


def test_extract_reply_errors():
    with pytest.raises(p.NakError):
        p.extract_reply(p.NAK_FRAME)
    bad = bytearray(p.build_reply(b"\x00\x01"))
    bad[-1] ^= 0xFF
    with pytest.raises(p.ChecksumError):
        p.extract_reply(bytes(bad))


def test_extract_poll():
    a, b = p.build_poll("Y"), p.build_poll("C0")
    stream = b"\x00" + a + b[:3]
    f, used = p.extract_poll(stream)
    assert f == a
    rest = stream[used:]
    assert p.extract_poll(rest) == (None, 0)
    f, _ = p.extract_poll(rest + b[3:])
    assert f == b


def test_bit_decoding():
    bits = p.decode_bits(0x320, p.OPTO_BITS)
    assert bits["remote"] and bits["compressor"] and bits["spare"]
    assert not bits["no_flow"]
    assert p.describe_bits(0x009, p.FAULT_BITS) == ["Low flow", "High pressure"]
