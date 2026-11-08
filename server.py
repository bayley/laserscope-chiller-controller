#!/usr/bin/env python3
"""Web UI + REST API for a Lydall chiller on a USB-RS485 adapter.

    python server.py --port /dev/ttyUSB0          # real chiller
    python server.py --simulate                    # no hardware
    python server.py                               # pick the port in the UI

Then open http://127.0.0.1:8080
"""
from __future__ import annotations

import argparse
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from chiller import protocol as p
from chiller.service import ChillerService, WriteError
from chiller.transport import list_serial_ports

STATIC = Path(__file__).parent / "static"


class ConnectReq(BaseModel):
    port: Optional[str] = None
    simulate: bool = False


class SetpointReq(BaseModel):
    celsius: float


class ConfigReq(BaseModel):
    changes: dict[str, int]
    confirm: bool = False


class RawReq(BaseModel):
    command: str


class SimReq(BaseModel):
    remote: Optional[bool] = None
    inject: Optional[str] = None
    clear: bool = False


def create_app(service: ChillerService, autoconnect: Optional[dict] = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if autoconnect:
            try:
                service.connect(**autoconnect)
            except Exception as exc:  # keep the UI up so the user can pick a port
                logging.getLogger("chiller").error("Auto-connect failed: %s", exc)
                service._event("error", f"Auto-connect failed: {exc}")
        yield
        service.disconnect()

    app = FastAPI(title="Lydall Chiller Control", lifespan=lifespan)

    def guard(fn, *a):
        try:
            return fn(*a)
        except (WriteError, ValueError) as exc:   # bad request / refused
            raise HTTPException(400, str(exc))
        except (p.ProtocolError, OSError) as exc:  # chiller or port trouble
            raise HTTPException(502, f"{type(exc).__name__}: {exc}")

    @app.get("/api/status")
    def status():
        return service.status()

    @app.get("/api/history")
    def history(seconds: float = 600):
        return service.history_since(min(seconds, 7 * 86400))

    @app.get("/api/events")
    def events():
        return list(service.events)[::-1]

    @app.get("/api/ports")
    def ports():
        return list_serial_ports()

    @app.get("/api/protocol")
    def protocol_info():
        return {
            "read_commands": p.READ_COMMANDS,
            "opto_bits": [{"mask": m, "key": k, "label": l, "bad": b} for m, k, l, b in p.OPTO_BITS],
            "fault_bits": [{"mask": m, "key": k, "label": l} for m, k, l in p.FAULT_BITS],
        }

    @app.post("/api/connect")
    def connect(req: ConnectReq):
        guard(service.connect, req.port, req.simulate)
        return service.status()

    @app.post("/api/disconnect")
    def disconnect():
        service.disconnect()
        return service.status()

    @app.post("/api/setpoint")
    def setpoint(req: SetpointReq):
        return guard(service.set_setpoint, req.celsius)

    @app.post("/api/config")
    def config(req: ConfigReq):
        if not req.confirm:
            raise HTTPException(400, "config writes require confirm=true")
        return guard(service.update_config, req.changes)

    @app.post("/api/raw")
    def raw(req: RawReq):
        return guard(service.raw, req.command)

    @app.post("/api/sim")
    def sim(req: SimReq):
        if service.sim is None:
            raise HTTPException(400, "not connected to the simulator")
        if req.remote is not None:
            service.sim.set_remote(req.remote)
        if req.clear:
            service.sim.clear_injected()
        if req.inject:
            guard(service.sim.inject, req.inject)
        return service.sim.snapshot()

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default=os.environ.get("CHILLER_PORT"),
                    help="serial device, e.g. /dev/ttyUSB0 or /dev/serial/by-id/... (env CHILLER_PORT)")
    ap.add_argument("--simulate", action="store_true", help="use the built-in simulated chiller")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (use 0.0.0.0 for LAN access; no auth!)")
    ap.add_argument("--http-port", type=int, default=8080)
    ap.add_argument("--poll-interval", type=float, default=1.0, help="seconds between polls")
    ap.add_argument("--timeout", type=float, default=0.3, help="serial reply timeout, seconds")
    ap.add_argument("--rs485-rts", action="store_true", help="drive RTS for TX enable (adapters without auto-direction)")
    ap.add_argument("--setpoint-min", type=float, default=15.0, help="lowest setpoint the UI may write, °C")
    ap.add_argument("--setpoint-max", type=float, default=35.0, help="highest setpoint the UI may write, °C")
    ap.add_argument("--read-only", action="store_true", help="disable all writes (monitor only)")
    ap.add_argument("--csv", help="append every poll to this CSV file")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    service = ChillerService(
        setpoint_min=args.setpoint_min, setpoint_max=args.setpoint_max,
        poll_interval=args.poll_interval, csv_path=args.csv, read_only=args.read_only,
        serial_kwargs={"timeout": args.timeout, "rs485_rts": args.rs485_rts},
    )
    auto = None
    if args.simulate:
        auto = {"simulate": True}
    elif args.port:
        auto = {"port": args.port}

    import uvicorn
    uvicorn.run(create_app(service, auto), host=args.host, port=args.http_port, log_level="warning")


if __name__ == "__main__":
    main()
