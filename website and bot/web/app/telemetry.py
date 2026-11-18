"""Receive read-only telemetry from the AIPatronaj ESP32 sketch."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated

import httpx
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger("digital-bridge.telemetry")
FRAME_LIMIT = 2048
STALE_AFTER = 2.0
LAN_RANGES = tuple(
    ipaddress.ip_network(subnet) for subnet in
    ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8")
)


def is_local_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(ip in subnet for subnet in LAN_RANGES)


class FirmwareFrame(BaseModel):
    hr: int = Field(ge=0, le=300)
    spo2: int = Field(ge=0, le=100)
    temp: float = Field(ge=-128, le=125, allow_inf_nan=False)
    temp_ok: bool
    ecg_mv: int = Field(ge=0, le=5000)
    ecg_leads_ok: bool
    ecg_wave: list[Annotated[int, Field(ge=0, le=4095)]] = Field(min_length=1, max_length=128)
    co2: int = Field(ge=0, le=100000)
    mq_raw: int = Field(ge=0, le=4095)
    finger: bool
    button_start: bool


class Telemetry:
    def __init__(self) -> None:
        self.frame: FirmwareFrame | None = None
        self.received_at = 0.0
        self.timestamp: str | None = None
        self.source = "esp32"
        self.packets = 0
        self.button_presses = 0

    def ingest(self, payload: bytes, source: str) -> None:
        if len(payload) > FRAME_LIMIT:
            return
        try:
            frame = FirmwareFrame.model_validate_json(payload, strict=True)
        except ValidationError:
            return
        now = time.monotonic()
        previous = self.frame
        age = now - self.received_at
        # One firmware frame may arrive several times via broadcast.
        if source == "esp32-udp" and previous == frame and age < 0.15:
            return
        if frame.button_start and (previous is None or age >= STALE_AFTER or not previous.button_start):
            self.button_presses += 1
        self.frame = frame
        self.received_at = now
        self.timestamp = datetime.now(timezone.utc).isoformat()
        self.source = source
        self.packets += 1

    def snapshot(self) -> dict:
        f = self.frame
        if f is None or time.monotonic() - self.received_at >= STALE_AFTER:
            return {"connected": False, "source": "esp32", "sensors": {}, "timestamp": self.timestamp}
        pulse = f.hr if f.finger and f.hr else None
        spo2 = f.spo2 if f.finger and f.spo2 else None
        temperature = f.temp if f.temp_ok and f.temp not in (-127, 0) else None
        return {
            "connected": True,
            "source": self.source,
            "timestamp": self.timestamp,
            "packetCount": self.packets,
            "buttonPressCount": self.button_presses,
            "pulse": pulse,
            "spo2": spo2,
            "temperature": temperature,
            "respiration": None,
            "tremor": None,
            "sensors": {
                "heart_rate_bpm": pulse,
                "spo2_percent": spo2,
                "temperature_c": temperature,
                "temp_ok": temperature is not None,
                "ecg_mv": f.ecg_mv if f.ecg_leads_ok else None,
                "ecg_leads_ok": f.ecg_leads_ok,
                "ecg_wave": f.ecg_wave if f.ecg_leads_ok else [],
                "air_co2_ppm": f.co2,
                "mq_raw": f.mq_raw,
                "finger_on_sensor": f.finger,
                "button_start": f.button_start,
            },
        }


class FirmwareUDP(asyncio.DatagramProtocol):
    def __init__(self, telemetry: Telemetry, host: str) -> None:
        self.telemetry = telemetry
        self.host = host

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        ip = addr[0]
        if is_local_ip(ip) and (not self.host or ip == self.host):
            self.telemetry.ingest(data, "esp32-udp")


async def poll_http(telemetry: Telemetry, host: str) -> None:
    async with httpx.AsyncClient(timeout=0.8, trust_env=False) as client:
        while True:
            if telemetry.source != "esp32-udp" or time.monotonic() - telemetry.received_at > 0.8:
                try:
                    async with client.stream("GET", f"http://{host}/api/hardware") as response:
                        response.raise_for_status()
                        payload = bytearray()
                        async for chunk in response.aiter_bytes():
                            payload.extend(chunk)
                            if len(payload) > FRAME_LIMIT:
                                break
                        else:
                            telemetry.ingest(bytes(payload), "esp32-http")
                except httpx.HTTPError:
                    pass
            await asyncio.sleep(0.32)


async def read_serial(telemetry: Telemetry, port: str) -> None:
    import serial

    while True:
        try:
            with serial.Serial(port, 115200, timeout=0.35) as device:
                while True:
                    line = await asyncio.to_thread(device.read_until, b"\n", FRAME_LIMIT + 1)
                    if line.endswith(b"\n"):
                        telemetry.ingest(line, "esp32-serial")
        except (OSError, serial.SerialException):
            await asyncio.sleep(1)


@asynccontextmanager
async def telemetry_inputs(telemetry: Telemetry):
    port = os.getenv("ESP32_SERIAL_PORT", "").strip()
    host = os.getenv("ESP32_HOST", "").strip()
    transport = None
    task = None
    if port:
        task = asyncio.create_task(read_serial(telemetry, port))
    else:
        if host and not is_local_ip(host):
            logger.warning("ESP32_HOST must be a local IPv4 address. Ignoring this setting.")
            host = ""
        try:
            transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: FirmwareUDP(telemetry, host), local_addr=("0.0.0.0", 41730)
            )
        except OSError:
            logger.warning("UDP 41730 is unavailable. Close the other listener or set ESP32_HOST.")
        if host:
            task = asyncio.create_task(poll_http(telemetry, host))
    try:
        yield
    finally:
        if transport is not None:
            transport.close()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
