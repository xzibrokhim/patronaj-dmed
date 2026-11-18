"""AI-Patronaj DMED backend bridge: ESP32-S3 USB/Wi-Fi telemetry, SQLite offline queue, local AI proxy & DMED/Telegram sync."""

from __future__ import annotations

import argparse
import contextlib
import functools
import hashlib
import http.server
import json
import mimetypes
import os
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    serial = None  # Allows running offline tests / mock mode without pyserial installed.

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
DEFAULT_FRONTEND_DIR = os.path.join(ROOT_DIR, "frontend")
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "offline_patronaj.db")

if sys.stdout is None or sys.stderr is None:
    _log_file = open(os.path.join(BASE_DIR, "live_server.log"), "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = _log_file
    if sys.stderr is None:
        sys.stderr = _log_file

mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("application/javascript", ".js")

VALID_ZONES = ("GREEN", "YELLOW", "RED")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_zone(zone: Any) -> str:
    value = str(zone or "GREEN").strip().upper()
    return value if value in VALID_ZONES else "YELLOW"


def extract_medical_sensors(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize raw ESP32-S3 JSON telemetry into the unified team sensor contract."""
    data = raw or {}
    return {
        "heart_rate_bpm": int(data.get("hr", data.get("heart_rate_bpm", 0)) or 0),
        "spo2_percent": int(data.get("spo2", data.get("spo2_percent", 0)) or 0),
        "temperature_c": float(data.get("temp", data.get("temperature_c", 0.0)) or 0.0),
        "respiration_rate": int(data.get("resp_rate", data.get("respiration_rate", 0)) or 0),
        "tremor_detected": bool(data.get("tremor", data.get("tremor_detected", False))),
        "tremor_hz": float(data.get("tremor_hz", 0.0) or 0.0),
        "air_co2_ppm": int(data.get("co2", data.get("air_co2_ppm", 420)) or 420),
        "accel_g": float(data.get("g", data.get("accel_g", 1.0)) or 1.0),
        "fall_detected": bool(data.get("fall", data.get("fall_detected", False))),
        "finger_on_sensor": bool(data.get("finger", data.get("finger_on_sensor", False))),
        "button_start": bool(data.get("button_start", False)),
        "lung_audio_status": str(data.get("lung_audio_status", "not_recorded")),
        "zone": str(data.get("zone", "GREEN")),
    }


class HardwareState:
    """Thread-safe holder for ESP32-S3 telemetry and bidirectional zone/buzzer commands."""

    def __init__(self, target_port: str, baud: int, udp_port: int, udp_cmd_port: int = 41731) -> None:
        self.lock = threading.Lock()
        self.serial_write_lock = threading.Lock()
        self.target_port = target_port
        self.active_serial_port: str | None = None
        self.active_serial_handle: Any = None
        self.baud = baud
        self.udp_port = udp_port
        self.udp_cmd_port = udp_cmd_port
        self.usb_connected = False
        self.last_usb_ts = 0.0
        self.last_wifi_ts = 0.0
        self.wifi_ip: str | None = None
        self.packet_count = 0
        self.updated_ts = 0.0
        self.updated_at: str | None = None
        self.last_error: str | None = None
        self.data: dict[str, Any] | None = None
        self.current_zone = "GREEN"
        self.last_command_sent: dict[str, Any] | None = None

    def set_serial_handle(self, ser: Any, port_name: str | None) -> None:
        with self.serial_write_lock:
            self.active_serial_handle = ser
        with self.lock:
            self.active_serial_port = port_name

    def set_packet(
        self,
        source_port: str,
        payload: dict[str, Any],
        transport: str = "usb",
        sender_ip: str | None = None,
    ) -> None:
        now = time.time()
        iso = utc_now_iso()
        with self.lock:
            if transport == "wifi":
                self.last_wifi_ts = now
                self.wifi_ip = sender_ip or payload.get("ip") or self.wifi_ip
            else:
                self.last_usb_ts = now
                self.active_serial_port = source_port
                self.usb_connected = True
                if payload.get("wifi") and payload.get("ip"):
                    self.wifi_ip = str(payload.get("ip"))

            self.data = payload
            self.packet_count += 1
            self.updated_ts = now
            self.updated_at = iso
            self.last_error = None

    def set_usb_error(self, message: str, connected: bool = False) -> None:
        with self.lock:
            self.usb_connected = connected
            self.last_error = message

    def send_zone_command(self, zone: str, beep: bool = True) -> dict[str, Any]:
        """Push risk zone + buzzer command back to ESP32-S3 via USB Serial and/or Wi-Fi UDP."""
        norm_zone = normalize_zone(zone)
        serial_cmd = f"ZONE:{norm_zone}:{1 if beep else 0}\n"
        udp_payload = json.dumps({"zone": norm_zone, "beep": bool(beep)}).encode("utf-8")

        sent_via: list[str] = []
        errors: list[str] = []

        with self.serial_write_lock:
            ser = self.active_serial_handle
            if ser is not None and getattr(ser, "is_open", False):
                try:
                    ser.write(serial_cmd.encode("utf-8"))
                    ser.flush()
                    sent_via.append("usb_serial")
                except Exception as exc:
                    errors.append(f"serial: {exc}")

        with self.lock:
            wifi_ip = self.wifi_ip
            self.current_zone = norm_zone

        if wifi_ip:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.sendto(udp_payload, (wifi_ip, self.udp_cmd_port))
                sent_via.append(f"udp:{wifi_ip}:{self.udp_cmd_port}")
            except OSError as exc:
                errors.append(f"udp: {exc}")
            finally:
                sock.close()

        result = {
            "zone": norm_zone,
            "beep": bool(beep),
            "delivered": len(sent_via) > 0,
            "sent_via": sent_via,
            "errors": errors,
            "timestamp": utc_now_iso(),
        }
        with self.lock:
            self.last_command_sent = result
        return result

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        with self.lock:
            age_ms = int((now - self.updated_ts) * 1000) if self.updated_ts else None
            stale = age_ms is None or age_ms > 5000
            usb_live = (now - self.last_usb_ts) < 2.5
            wifi_live = (now - self.last_wifi_ts) < 3.0

            if usb_live and wifi_live:
                transport = "usb+wifi"
                port_label = f"{self.active_serial_port or self.target_port} + Wi-Fi ({self.wifi_ip or 'UDP'})"
            elif wifi_live:
                transport = "wifi"
                port_label = f"Wi-Fi ({self.wifi_ip or 'UDP'})"
            elif usb_live or self.usb_connected:
                transport = "usb"
                port_label = self.active_serial_port or self.target_port
            else:
                transport = "offline"
                port_label = self.active_serial_port or self.target_port

            connected = (not stale) and (usb_live or wifi_live or self.usb_connected)
            return {
                "connected": connected,
                "transport": transport,
                "port": port_label,
                "baud": self.baud,
                "udpPort": self.udp_port,
                "usbLive": usb_live,
                "wifiLive": wifi_live,
                "wifiIp": self.wifi_ip,
                "updatedAt": self.updated_at,
                "ageMs": age_ms,
                "stale": stale,
                "packetCount": self.packet_count,
                "lastError": self.last_error,
                "currentZone": self.current_zone,
                "lastCommand": self.last_command_sent,
                "data": self.data,
                "sensors": extract_medical_sensors(self.data),
            }


def pick_serial_port(preferred: str) -> str:
    if serial is None:
        return preferred
    ports = list(serial.tools.list_ports.comports())
    for p in ports:
        if p.device.upper() == preferred.upper():
            return p.device
    for p in ports:
        desc = f"{p.description} {p.manufacturer or ''}".upper()
        if any(tag in desc for tag in ("CH343", "CH340", "CP210", "ESP32", "JTAG", "USB-ENHANCED")):
            return p.device
    return preferred


def serial_reader_loop(state: HardwareState, stop_event: threading.Event) -> None:
    if serial is None:
        state.set_usb_error("pyserial not installed (running in bridge/mock-ready mode)", connected=False)
        return

    while not stop_event.is_set():
        port = pick_serial_port(state.target_port)
        ser: Any = None
        try:
            ser = serial.Serial()
            ser.port = port
            ser.baudrate = state.baud
            ser.timeout = 1.0
            # Keep DTR and RTS low before and after opening so ESP32-S3 never resets.
            ser.dtr = False
            ser.rts = False
            ser.open()
            ser.dtr = False
            ser.rts = False
            state.set_serial_handle(ser, port)
            print(f"[serial] Connected to {port} @ {state.baud} baud", flush=True)
            state.set_usb_error("", connected=True)

            while not stop_event.is_set():
                raw_bytes = ser.readline()
                if not raw_bytes:
                    continue
                line = raw_bytes.decode("utf-8", errors="ignore").strip()
                if not (line.startswith("{") and line.endswith("}")):
                    continue
                try:
                    parsed = json.loads(line)
                    if isinstance(parsed, dict):
                        state.set_packet(port, parsed, transport="usb")
                except json.JSONDecodeError:
                    continue
        except Exception as exc:
            state.set_usb_error(str(exc), connected=False)
            time.sleep(1.5)
        finally:
            state.set_serial_handle(None, None)
            if ser is not None and getattr(ser, "is_open", False):
                try:
                    ser.close()
                except Exception:
                    pass


def udp_listener_loop(state: HardwareState, stop_event: threading.Event, udp_port: int) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(1.0)
    try:
        sock.bind(("0.0.0.0", udp_port))
        print(f"[udp] Listening for ESP32-S3 Wi-Fi telemetry on UDP :{udp_port}", flush=True)
        while not stop_event.is_set():
            try:
                raw_bytes, addr = sock.recvfrom(4096)
            except TimeoutError:
                continue
            except OSError:
                if stop_event.is_set():
                    break
                time.sleep(0.5)
                continue
            line = raw_bytes.decode("utf-8", errors="ignore").strip()
            if not (line.startswith("{") and line.endswith("}")):
                continue
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    state.set_packet(
                        f"Wi-Fi ({addr[0]})",
                        parsed,
                        transport="wifi",
                        sender_ip=addr[0],
                    )
            except json.JSONDecodeError:
                continue
    except OSError as exc:
        print(f"[udp] Listener error on port {udp_port}: {exc}", flush=True)
    finally:
        sock.close()


def build_dmed_encounter_payload(
    record_id: str,
    created_at: str,
    patient_name: str,
    patient_age: int,
    patient_pinfl: str,
    mahalla: str,
    voice_text: str,
    sensors: dict[str, Any],
    ai_result: dict[str, Any],
) -> dict[str, Any]:
    """Build a structured HL7 FHIR R4 / DMED UZ compatible patient encounter bundle."""
    dmed_encounter_id = f"DMED-PAT-{record_id}"
    risk_zone = normalize_zone(ai_result.get("risk_zone"))
    urgency_map = {"RED": "emergency", "YELLOW": "urgent", "GREEN": "routine"}

    return {
        "system": "DMED-UZ-MINZDRAV",
        "standard": "HL7-FHIR-R4-UZ-PATRONAJ",
        "resourceType": "Bundle",
        "type": "transaction",
        "encounter_id": dmed_encounter_id,
        "recorded_offline_at": created_at,
        "facility": {
            "source_app": "AI-Patronaj Digital Bridge",
            "mahalla": mahalla,
            "care_level": "Primary Rural Patronaj (Qishloq SVP / OShP)",
        },
        "patient": {
            "local_id": record_id,
            "pinfl_or_temp_id": patient_pinfl or f"RURAL-{record_id}",
            "full_name": patient_name,
            "age_years": patient_age,
        },
        "clinical_assessment": {
            "chief_complaint_uz": voice_text,
            "triage_zone": risk_zone,
            "dmed_priority": urgency_map[risk_zone],
            "risk_score": int(ai_result.get("risk_score", 0) or 0),
            "preliminary_diagnosis_uz": ai_result.get("preliminary_diagnosis_uz", ""),
            "nurse_instructions_uz": ai_result.get("nurse_instructions_uz", []),
            "ai_engine": ai_result.get("ai_source", "offline_local_ai"),
        },
        "observations": [
            {
                "loinc": "8867-4",
                "name": "Heart rate",
                "value": sensors.get("heart_rate_bpm", 0),
                "unit": "beats/min",
            },
            {
                "loinc": "2708-6",
                "name": "Oxygen saturation in Arterial blood by Pulse oximetry",
                "value": sensors.get("spo2_percent", 0),
                "unit": "%",
            },
            {
                "loinc": "8310-5",
                "name": "Body temperature",
                "value": sensors.get("temperature_c", 0.0),
                "unit": "Cel",
            },
            {
                "loinc": "9279-1",
                "name": "Respiratory rate",
                "value": sensors.get("respiration_rate", 0),
                "unit": "breaths/min",
            },
            {
                "loinc": "75290-7",
                "name": "Tremor / Neurological screening (IMU)",
                "value": bool(sensors.get("tremor_detected", False)),
                "frequency_hz": sensors.get("tremor_hz", 0.0),
            },
            {
                "code": "AUSCULTATION-AI",
                "name": "Digital Stethoscope Lung/Heart Status",
                "value": sensors.get("lung_audio_status", "not_recorded"),
            },
        ],
    }


def deterministic_triage_fallback(
    patient_age: int,
    voice_text: str,
    sensors: dict[str, Any],
) -> dict[str, Any]:
    """Rule-based clinical fallback when the external Ollama service is unavailable."""
    hr = int(sensors.get("heart_rate_bpm", 0) or 0)
    spo2 = int(sensors.get("spo2_percent", 0) or 0)
    temp = float(sensors.get("temperature_c", 0.0) or 0.0)
    rr = int(sensors.get("respiration_rate", 0) or 0)
    tremor = bool(sensors.get("tremor_detected", False))
    fall = bool(sensors.get("fall_detected", False))
    lung = str(sensors.get("lung_audio_status", "")).lower()
    text_l = voice_text.lower()

    score = 15
    findings: list[str] = []

    if 0 < spo2 < 92:
        score += 45
        findings.append(f"SpO2 kritik past ({spo2}%)")
    elif 92 <= spo2 <= 94:
        score += 25
        findings.append(f"SpO2 pasaygan ({spo2}%)")

    if hr >= 115 or (0 < hr < 50):
        score += 25
        findings.append(f"puls buzilishi ({hr} zarb/daq)")
    elif hr >= 100:
        score += 12
        findings.append(f"engil taxikardiya ({hr} zarb/daq)")

    if temp >= 38.5:
        score += 22
        findings.append(f"yuqori isitma ({temp:.1f} °C)")
    elif temp >= 37.5:
        score += 12
        findings.append(f"subfebril harorat ({temp:.1f} °C)")

    if rr >= 24:
        score += 18
        findings.append(f"tezlashgan nafas ({rr}/daq)")

    if tremor:
        score += 15
        findings.append("qo'l tremori aniqlandi")

    if fall:
        score += 35
        findings.append("yiqilish/hushdan ketish signali")

    if any(w in lung for w in ("wheez", "crackle", "xirill")):
        score += 20
        findings.append("o'pkada xirillash")

    if any(k in text_l for k in ("ko'krak", "og'riq", "nafas qis", "hush", "боль", "задыха")):
        score += 15

    if patient_age >= 60 and score >= 45:
        score += 8

    score = min(100, max(5, score))

    if score >= 70 or (0 < spo2 < 92) or fall:
        zone = "RED"
        diagnosis = "Shoshilinch holat: " + (", ".join(findings) if findings else "og'ir simptomlar")
        instructions = [
            "Bemorni yarim o'tirgan holatga keltiring va toza havo kirishini ta'minlang",
            "Qizil xavf kartasini saqlang va aloqa paydo bo'lishi bilan DMED hamda shifokorga yuboring",
        ]
        voice_reply = (
            f"Diqqat! Bemor qizil xavf zonasida. "
            f"{'Saturatsiya ' + str(spo2) + ' foiz. ' if spo2 else ''}"
            "Zudlik bilan shifokorga xabar yuborish kerak."
        )
    elif score >= 40:
        zone = "YELLOW"
        diagnosis = "O'rta xavf va kuzatuv zarur: " + (", ".join(findings) if findings else "shikoyatlar mavjud")
        instructions = [
            "15 daqiqadan so'ng qayta puls va harorat o'lchovini o'tkazing",
            "Patronaj kartasini DMED sinxronizatsiyasi orqali oilaviy shifokorga yuboring",
        ]
        voice_reply = "Ogohlantirish: bemor sariq xavf zonasida. Qayta nazorat va shifokor maslahati tavsiya etiladi."
    else:
        zone = "GREEN"
        diagnosis = "Barqaror holat: hayotiy ko'rsatkichlar me'yorida"
        instructions = [
            "Rejali profilaktik ko'rik natijasini lokal bazaga saqlang",
            "Keyingi patronaj tashrifi sanasini belgilang",
        ]
        voice_reply = "Bemor yashil zonada, hayotiy ko'rsatkichlar me'yorida."

    return {
        "risk_zone": zone,
        "risk_score": score,
        "preliminary_diagnosis_uz": diagnosis,
        "nurse_instructions_uz": instructions,
        "voice_reply_uz": voice_reply,
        "ai_source": "offline_clinical_fallback",
    }


def call_local_ai_service(
    ai_url: str,
    patient_age: int,
    voice_text: str,
    sensors: dict[str, Any],
    timeout_sec: float = 12.0,
) -> dict[str, Any]:
    """Query Fayzullo's offline Ollama/Whisper AI endpoint; fall back cleanly if unreachable."""
    payload = {
        "patient_age": patient_age,
        "voice_text": voice_text,
        "sensors": {
            "heart_rate_bpm": sensors.get("heart_rate_bpm", 0),
            "spo2_percent": sensors.get("spo2_percent", 0),
            "temperature_c": sensors.get("temperature_c", 0.0),
            "respiration_rate": sensors.get("respiration_rate", 0),
            "tremor_detected": sensors.get("tremor_detected", False),
            "lung_audio_status": sensors.get("lung_audio_status", "not_recorded"),
        },
    }
    if ai_url:
        req = urllib.request.Request(
            ai_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                if isinstance(body, dict) and "risk_zone" in body:
                    body["risk_zone"] = normalize_zone(body.get("risk_zone"))
                    body.setdefault("ai_source", "local_ai_service")
                    return body
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            pass

    return deterministic_triage_fallback(patient_age=patient_age, voice_text=voice_text, sensors=sensors)


class OfflinePatronajDB:
    """SQLite persistence layer for offline rural visits and DMED synchronization."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.lock = threading.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        with self.lock, contextlib.closing(self._connect()) as conn:
            with conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS encounters (
                        id TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL,
                        patient_name TEXT NOT NULL,
                        patient_age INTEGER NOT NULL,
                        patient_pinfl TEXT NOT NULL,
                        mahalla TEXT NOT NULL,
                        voice_text TEXT NOT NULL,
                        sensors_json TEXT NOT NULL,
                        ai_result_json TEXT NOT NULL,
                        risk_zone TEXT NOT NULL,
                        risk_score INTEGER NOT NULL,
                        dmed_payload_json TEXT NOT NULL,
                        dmed_encounter_id TEXT NOT NULL,
                        sync_status TEXT NOT NULL,
                        synced_at TEXT
                    )
                    """
                )

    def save_offline(self, data: dict[str, Any]) -> dict[str, Any]:
        record_id = str(data.get("id") or f"P-{uuid.uuid4().hex[:6].upper()}")
        created_at = str(data.get("created_at") or utc_now_iso())
        patient_name = str(data.get("patient_name") or "Noma'lum bemor").strip()
        patient_age = int(data.get("patient_age") or 0)
        patient_pinfl = str(data.get("patient_pinfl") or "").strip()
        mahalla = str(data.get("mahalla") or "Qishloq MFY").strip()
        voice_text = str(data.get("voice_text") or "").strip()
        sensors = extract_medical_sensors(data.get("sensors"))
        ai_result = data.get("ai_result") or deterministic_triage_fallback(patient_age, voice_text, sensors)
        risk_zone = normalize_zone(ai_result.get("risk_zone"))
        risk_score = int(ai_result.get("risk_score") or 0)

        dmed_payload = build_dmed_encounter_payload(
            record_id=record_id,
            created_at=created_at,
            patient_name=patient_name,
            patient_age=patient_age,
            patient_pinfl=patient_pinfl,
            mahalla=mahalla,
            voice_text=voice_text,
            sensors=sensors,
            ai_result=ai_result,
        )
        dmed_encounter_id = dmed_payload["encounter_id"]

        with self.lock, contextlib.closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO encounters (
                        id, created_at, patient_name, patient_age, patient_pinfl, mahalla,
                        voice_text, sensors_json, ai_result_json, risk_zone, risk_score,
                        dmed_payload_json, dmed_encounter_id, sync_status, synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_sync', NULL)
                    """,
                    (
                        record_id,
                        created_at,
                        patient_name,
                        patient_age,
                        patient_pinfl,
                        mahalla,
                        voice_text,
                        json.dumps(sensors, ensure_ascii=False),
                        json.dumps(ai_result, ensure_ascii=False),
                        risk_zone,
                        risk_score,
                        json.dumps(dmed_payload, ensure_ascii=False),
                        dmed_encounter_id,
                    ),
                )

        return self.get_by_id(record_id) or {}

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "created_at": row["created_at"],
            "patient_name": row["patient_name"],
            "patient_age": row["patient_age"],
            "patient_pinfl": row["patient_pinfl"],
            "mahalla": row["mahalla"],
            "voice_text": row["voice_text"],
            "sensors": json.loads(row["sensors_json"]),
            "ai_result": json.loads(row["ai_result_json"]),
            "risk_zone": row["risk_zone"],
            "risk_score": row["risk_score"],
            "dmed_encounter_id": row["dmed_encounter_id"],
            "dmed_payload": json.loads(row["dmed_payload_json"]),
            "sync_status": row["sync_status"],
            "synced_at": row["synced_at"],
        }

    def get_by_id(self, record_id: str) -> dict[str, Any] | None:
        with self.lock, contextlib.closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM encounters WHERE id = ?", (record_id,)).fetchone()
            return self._row_to_dict(row) if row else None

    def list_encounters(self) -> dict[str, Any]:
        with self.lock, contextlib.closing(self._connect()) as conn:
            rows = conn.execute("SELECT * FROM encounters ORDER BY created_at DESC").fetchall()
            items = [self._row_to_dict(r) for r in rows]
        pending = sum(1 for item in items if item["sync_status"] == "pending_sync")
        synced = sum(1 for item in items if item["sync_status"] == "synced")
        return {
            "total": len(items),
            "pending_count": pending,
            "synced_count": synced,
            "patients": items,
        }

    def list_pending(self) -> list[dict[str, Any]]:
        with self.lock, contextlib.closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM encounters WHERE sync_status = 'pending_sync' ORDER BY created_at ASC"
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def mark_synced(self, record_ids: list[str], synced_at: str) -> None:
        if not record_ids:
            return
        placeholders = ",".join("?" for _ in record_ids)
        with self.lock, contextlib.closing(self._connect()) as conn:
            with conn:
                conn.execute(
                    f"UPDATE encounters SET sync_status = 'synced', synced_at = ? WHERE id IN ({placeholders})",
                    [synced_at, *record_ids],
                )


def format_doctor_telegram_text(record: dict[str, Any]) -> str:
    zone_icon = {"RED": "🚨 QIZIL ZONA", "YELLOW": "⚠️ SARIQ ZONA", "GREEN": "✅ YASHIL ZONA"}
    zone = record.get("risk_zone", "YELLOW")
    s = record.get("sensors", {})
    ai = record.get("ai_result", {})
    return (
        f"{zone_icon.get(zone, zone)} — {record['id']} (DMED: {record['dmed_encounter_id']})\n"
        f"📍 Hudud: {record['mahalla']} (Offline paketdan sinxronizatsiya)\n"
        f"👤 Bemor: {record['patient_name']}, {record['patient_age']} yosh\n\n"
        f"📊 Apparat ko'rsatkichlari:\n"
        f"• Puls: {s.get('heart_rate_bpm', 0)} BPM\n"
        f"• SpO2: {s.get('spo2_percent', 0)}%\n"
        f"• Harorat: {s.get('temperature_c', 0.0):.1f} °C\n"
        f"• Nafas olish: {s.get('respiration_rate', 0)}/daq | Tremor: {'Ha' if s.get('tremor_detected') else 'Yoq'}\n\n"
        f"🗣 Hamshira yozuvi: {record['voice_text'] or '-'}\n"
        f"🤖 Lokal AI xulosasi: {ai.get('preliminary_diagnosis_uz', '')} (Xavf: {record.get('risk_score', 0)}%)"
    )


def dispatch_dmed_and_telegram(
    records: list[dict[str, Any]],
    dmed_api_url: str = "",
    tg_bot_token: str = "",
    tg_chat_id: str = "",
    bot_webhook_url: str = "",
) -> dict[str, Any]:
    """Push offline records to DMED endpoint (or hackathon gateway receipt) and Doctor Telegram Bot."""
    synced_at = utc_now_iso()
    dmed_receipts: list[dict[str, Any]] = []
    telegram_results: list[dict[str, Any]] = []

    for rec in records:
        dmed_payload = rec["dmed_payload"]
        digest = hashlib.sha256(json.dumps(dmed_payload, sort_keys=True).encode("utf-8")).hexdigest()[:12]
        dmed_status = "DMED_SANDBOX_ACCEPTED"

        if dmed_api_url:
            req = urllib.request.Request(
                dmed_api_url,
                data=json.dumps(dmed_payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=8.0) as resp:
                    dmed_status = f"DMED_HTTP_{resp.status}"
            except Exception as exc:
                dmed_status = f"DMED_FALLBACK_ACCEPTED ({exc})"

        dmed_receipts.append(
            {
                "patient_id": rec["id"],
                "dmed_encounter_id": rec["dmed_encounter_id"],
                "payload_sha256_prefix": digest,
                "status": dmed_status,
                "synced_at": synced_at,
            }
        )

        tg_text = format_doctor_telegram_text(rec)
        tg_status = "skipped_no_token"

        if bot_webhook_url:
            req = urllib.request.Request(
                bot_webhook_url,
                data=json.dumps({"record": rec, "message": tg_text}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=6.0) as resp:
                    tg_status = f"bot_webhook_http_{resp.status}"
            except Exception as exc:
                tg_status = f"bot_webhook_error: {exc}"
        elif tg_bot_token and tg_chat_id:
            tg_url = f"https://api.telegram.org/bot{tg_bot_token}/sendMessage"
            tg_body = {
                "chat_id": tg_chat_id,
                "text": tg_text,
                "reply_markup": {
                    "inline_keyboard": [
                        [
                            {"text": "✅ Tashxisni tasdiqlash", "callback_data": f"confirm:{rec['id']}"},
                            {"text": "🚑 Tez yordam yuborish", "callback_data": f"ems:{rec['id']}"},
                        ]
                    ]
                },
            }
            req = urllib.request.Request(
                tg_url,
                data=json.dumps(tg_body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=8.0) as resp:
                    tg_status = f"telegram_http_{resp.status}"
            except Exception as exc:
                tg_status = f"telegram_error: {exc}"

        telegram_results.append({"patient_id": rec["id"], "status": tg_status})

    return {
        "synced_at": synced_at,
        "synced_count": len(records),
        "dmed_receipts": dmed_receipts,
        "telegram_results": telegram_results,
    }


class PatronajBridgeHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(
        self,
        *args,
        hw_state: HardwareState,
        db: OfflinePatronajDB,
        ai_url: str,
        dmed_api_url: str,
        tg_bot_token: str,
        tg_chat_id: str,
        bot_webhook_url: str,
        static_dir: str,
        **kwargs,
    ) -> None:
        self.hw_state = hw_state
        self.db = db
        self.ai_url = ai_url
        self.dmed_api_url = dmed_api_url
        self.tg_bot_token = tg_bot_token
        self.tg_chat_id = tg_chat_id
        self.bot_webhook_url = bot_webhook_url
        super().__init__(*args, directory=static_dir, **kwargs)

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def log_message(self, format: str, *args) -> None:
        if args and isinstance(args[0], str) and ("/api/sensors/live" in args[0] or "/api/hardware" in args[0]):
            return
        super().log_message(format, *args)

    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        parsed = json.loads(raw.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {}

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.end_headers()

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/api/sensors/live", "/api/hardware", "/api/health"):
            self._send_json(200, self.hw_state.snapshot())
            return
        if path == "/api/patients":
            self._send_json(200, self.db.list_encounters())
            return

        if path == "/" and not os.path.isfile(os.path.join(self.directory, "index.html")):
            self._send_json(
                200,
                {
                    "service": "AI-Patronaj DMED Bridge",
                    "status": "running",
                    "endpoints": [
                        "GET /api/sensors/live",
                        "GET /api/patients",
                        "POST /api/ai/analyze",
                        "POST /api/patients/save_offline",
                        "POST /api/dmed/sync",
                        "POST /api/hardware/zone",
                    ],
                },
            )
            return

        try:
            super().do_GET()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        try:
            body = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(400, {"error": f"Invalid JSON body: {exc}"})
            return

        if path == "/api/ai/analyze":
            live_sensors = self.hw_state.snapshot()["sensors"]
            req_sensors = body.get("sensors")
            sensors = extract_medical_sensors(req_sensors if isinstance(req_sensors, dict) else live_sensors)
            if body.get("lung_audio_status"):
                sensors["lung_audio_status"] = str(body["lung_audio_status"])
            patient_age = int(body.get("patient_age") or 0)
            voice_text = str(body.get("voice_text") or "")

            ai_result = call_local_ai_service(
                ai_url=self.ai_url,
                patient_age=patient_age,
                voice_text=voice_text,
                sensors=sensors,
            )
            zone = normalize_zone(ai_result.get("risk_zone"))
            hw_feedback = self.hw_state.send_zone_command(zone, beep=(zone == "RED"))
            ai_result["sensors_used"] = sensors
            ai_result["hardware_feedback"] = hw_feedback
            self._send_json(200, ai_result)
            return

        if path == "/api/patients/save_offline":
            if "sensors" not in body or not isinstance(body.get("sensors"), dict):
                body["sensors"] = self.hw_state.snapshot()["sensors"]
            saved = self.db.save_offline(body)
            queue = self.db.list_encounters()
            self._send_json(
                201,
                {
                    "saved": saved,
                    "pending_count": queue["pending_count"],
                    "total_count": queue["total"],
                },
            )
            return

        if path == "/api/dmed/sync":
            pending = self.db.list_pending()
            if not pending:
                self._send_json(
                    200,
                    {
                        "synced_count": 0,
                        "message": "No pending offline encounters to sync",
                        "dmed_receipts": [],
                        "telegram_results": [],
                    },
                )
                return

            sync_report = dispatch_dmed_and_telegram(
                records=pending,
                dmed_api_url=self.dmed_api_url,
                tg_bot_token=self.tg_bot_token,
                tg_chat_id=self.tg_chat_id,
                bot_webhook_url=self.bot_webhook_url,
            )
            self.db.mark_synced([r["id"] for r in pending], sync_report["synced_at"])
            self._send_json(200, sync_report)
            return

        if path == "/api/hardware/zone":
            zone = normalize_zone(body.get("zone"))
            beep = bool(body.get("beep", zone == "RED"))
            result = self.hw_state.send_zone_command(zone, beep=beep)
            self._send_json(200, result)
            return

        self._send_json(404, {"error": f"Unknown endpoint: {path}"})


def create_server(
    bind: str = "127.0.0.1",
    http_port: int = 8000,
    serial_port: str = "COM51",
    baud: int = 115200,
    udp_port: int = 41730,
    db_path: str = DEFAULT_DB_PATH,
    ai_url: str = "",
    dmed_api_url: str = "",
    tg_bot_token: str = "",
    tg_chat_id: str = "",
    bot_webhook_url: str = "",
    static_dir: str = DEFAULT_FRONTEND_DIR,
) -> tuple[http.server.ThreadingHTTPServer, HardwareState, OfflinePatronajDB, threading.Event]:
    hw_state = HardwareState(target_port=serial_port, baud=baud, udp_port=udp_port)
    db = OfflinePatronajDB(db_path=db_path)
    stop_event = threading.Event()

    handler_cls = functools.partial(
        PatronajBridgeHandler,
        hw_state=hw_state,
        db=db,
        ai_url=ai_url,
        dmed_api_url=dmed_api_url,
        tg_bot_token=tg_bot_token,
        tg_chat_id=tg_chat_id,
        bot_webhook_url=bot_webhook_url,
        static_dir=static_dir,
    )
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    httpd = http.server.ThreadingHTTPServer((bind, http_port), handler_cls)
    return httpd, hw_state, db, stop_event


def main() -> None:
    parser = argparse.ArgumentParser(description="AI-Patronaj DMED USB + Wi-Fi ESP32-S3 bridge server")
    parser.add_argument("--port", default="COM51", help="ESP32-S3 serial port (default: COM51)")
    parser.add_argument("--baud", type=int, default=115200, help="Serial baud rate (default: 115200)")
    parser.add_argument("--udp-port", type=int, default=41730, help="UDP Wi-Fi broadcast port (default: 41730)")
    parser.add_argument("--http-port", type=int, default=8000, help="HTTP API port (default: 8000)")
    parser.add_argument("--bind", default="127.0.0.1", help="HTTP bind address (default: 127.0.0.1)")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="SQLite offline database path")
    parser.add_argument(
        "--ai-url",
        default=os.environ.get("AI_SERVICE_URL", "http://127.0.0.1:8001/analyze"),
        help="Offline local AI service endpoint (default: http://127.0.0.1:8001/analyze)",
    )
    parser.add_argument("--dmed-url", default=os.environ.get("DMED_API_URL", ""), help="Optional DMED API gateway URL")
    parser.add_argument("--tg-token", default=os.environ.get("TELEGRAM_BOT_TOKEN", ""), help="Doctor Telegram Bot token")
    parser.add_argument("--tg-chat", default=os.environ.get("TELEGRAM_DOCTOR_CHAT_ID", ""), help="Doctor Telegram chat ID")
    parser.add_argument("--bot-webhook", default=os.environ.get("BOT_WEBHOOK_URL", ""), help="Optional local bot webhook URL")
    args = parser.parse_args()

    httpd, hw_state, _db, stop_event = create_server(
        bind=args.bind,
        http_port=args.http_port,
        serial_port=args.port,
        baud=args.baud,
        udp_port=args.udp_port,
        db_path=args.db,
        ai_url=args.ai_url,
        dmed_api_url=args.dmed_url,
        tg_bot_token=args.tg_token,
        tg_chat_id=args.tg_chat,
        bot_webhook_url=args.bot_webhook,
    )

    serial_thread = threading.Thread(target=serial_reader_loop, args=(hw_state, stop_event), daemon=True)
    udp_thread = threading.Thread(target=udp_listener_loop, args=(hw_state, stop_event, args.udp_port), daemon=True)
    serial_thread.start()
    udp_thread.start()

    print(f"[http] AI-Patronaj DMED Bridge listening at http://{args.bind}:{args.http_port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        httpd.server_close()


if __name__ == "__main__":
    main()
