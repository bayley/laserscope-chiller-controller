# Lydall chiller control

Web UI and REST backend for monitoring and adjusting a Lydall chiller over RS-485
through a USB-serial adapter. The protocol follows
<https://directed-energy.net/laser/ams_lydallchiller.html>.

## Quick start

```bash
cd chiller_control
uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt
# or: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python server.py --port /dev/ttyUSB0      # real chiller
.venv/bin/python server.py --simulate               # no hardware
.venv/bin/python server.py                          # choose the port in the UI
```

Then open <http://127.0.0.1:8080>.

Use a stable device path such as `/dev/serial/by-id/usb-FTDI_...`, since `ttyUSB0` can
renumber. On Linux your user needs to be in the `dialout` group:
`sudo usermod -aG dialout $USER`, then log out and back in.

### Useful options

| Flag | Default | Purpose |
|---|---|---|
| `--port` / `CHILLER_PORT` | none | Serial device to auto-connect |
| `--host` | `127.0.0.1` | Use `0.0.0.0` for LAN access. **There is no authentication.** |
| `--http-port` | `8080` | Web port |
| `--poll-interval` | `1.0` | Seconds between poll cycles |
| `--timeout` | `0.3` | Reply timeout per frame (2 retries) |
| `--setpoint-min/--setpoint-max` | `15` / `35` °C | The UI refuses setpoints outside this range |
| `--read-only` | off | Monitoring only; all writes disabled |
| `--csv FILE` | off | Append every poll (all H values, C0, B1) to a CSV |
| `--rs485-rts` | off | For adapters that need RTS to drive TX-enable (most auto-switch) |

## What it does

**Monitor.** About once a second it polls `Y` (all of H0–H8 in one frame), `C0`
(inputs) and `B1` (faults). Every 15 cycles it also reads `B0` (EEPROM health),
`A0` (firmware ID) and `I` (config block). The UI shows:
- PV, setpoint, error and heater output
- input lamps and fault lamps
- a PV/SP/output chart covering up to 6 hours
- PID internals
- an event log of fault, REMOTE, compressor and communication transitions

**Setpoint.** To change the setpoint, the server reads `I`, replaces only the
setpoint word, writes it back with `J`, then reads `I` again and checks that it
matches.

**PID config.** The advanced editor covers proportional band, integral, derivative,
PV filter and the trims. The `unused` word is always written back unchanged.

**Raw console.** Lets you send any *read* command and see the TX/RX hex and the
decoded value. You cannot send `J` from the console.

## Important protocol limitations

- **You cannot start or stop the chiller over serial.** Run/stop is the hardware
  REMOTE input (~7 V DC = run). The UI shows its state.
- **Faults latch until REMOTE is removed.** The RTD fault is the exception and
  clears by itself. There is no serial command to reset faults.
- **`J` writes EEPROM, and the firmware does no range checking.** Because of that,
  the backend adds its own safety measures:
  - setpoint range limits
  - bounds on the config fields (for example, proportional band ≥ 1)
  - a fresh read before every change
  - read-back verification after every write
  - a 2 s rate limit between writes
  - no write at all when nothing changed

  The real laser never sends `J`. Record your unit's factory `I` block (use the raw
  console) before you change anything.

## Assumptions to check on real hardware

The reference document leaves these points open:

1. **Reply widths for C0/B1/B0/A0.** These aren't documented. The parser reads
   the length byte and decodes the value as a big-endian unsigned integer of any
   width, so this should work either way.
2. **Config words are treated as signed int16.** `FF FC` (gain trim) then decodes
   as −4, which looks right.
3. **Units of the PID config values** (P band, integral and derivative times, filter)
   aren't documented, so the UI shows the raw integers.
4. **Echo.** Some adapters echo what they transmit. The receiver detects and drops
   that echo automatically.
5. **Bus bias.** The source needed about 1 kΩ bias resistors (A up to about 3.6 V,
   B to ground). If you see timeouts or garbage, check biasing and A/B polarity
   before anything else.

## Testing without hardware

```bash
.venv/bin/pip install -r requirements-dev.txt   # or: uv pip install ...
.venv/bin/python -m pytest
```

The tests check every example frame in the reference document byte for byte. They
also run the service against the simulator, including an end-to-end test where real
pyserial talks to the simulator over a pseudo-terminal.

To try the full serial path by hand:

```bash
.venv/bin/python -m chiller.simulator --link /tmp/ttyCHILLER   # type: fault high_pressure / remote off / clear
.venv/bin/python server.py --port /tmp/ttyCHILLER
```

The simulator's thermal model and PID are made up and only meant to look plausible.
It is useful for exercising the UI and protocol code, not for tuning.

## Layout

```
server.py              FastAPI app + CLI
chiller/protocol.py    framing, checksums, decoding (pure, no I/O)
chiller/transport.py   pyserial transport (echo handling, retries) + simulator transport
chiller/service.py     poller, state/history/events, validated writes
chiller/simulator.py   simulated chiller (in-process or on a pty)
static/                web UI (plain HTML/CSS/JS, no CDN, works offline)
tests/
```

## REST API

`GET /api/status`, `GET /api/history?seconds=N`, `GET /api/events`, `GET /api/ports`,
`POST /api/connect {port | simulate}`, `POST /api/disconnect`,
`POST /api/setpoint {celsius}`, `POST /api/config {changes:{...}, confirm:true}`,
`POST /api/raw {command}`. Interactive docs are at `/docs`.
