from __future__ import annotations

import asyncio
import html
import hmac
import json
import logging
import os
import re
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .telemetry import Telemetry, telemetry_inputs

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "app" / "static"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "digital_bridge.sqlite3"
load_dotenv(BASE_DIR / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("digital-bridge")
telemetry = Telemetry()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with connect() as connection:
        connection.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS patients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                local_id TEXT NOT NULL UNIQUE,
                device_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                received_at TEXT NOT NULL,
                telegram_message_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS doctor_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id INTEGER NOT NULL,
                device_id TEXT NOT NULL,
                action TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(patient_id) REFERENCES patients(id)
            );
            """
        )


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    async with telemetry_inputs(telemetry):
        yield


app = FastAPI(title="Digital Bridge API", version="1.1.0", lifespan=lifespan)
cors_origins = [origin.strip() for origin in os.getenv(
    "CORS_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000"
).split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Internal-Key"],
)


@app.middleware("http")
async def security_and_size_headers(request: Request, call_next):
    max_bytes = int(os.getenv("MAX_REQUEST_BYTES", "262144"))
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > max_bytes:
        return JSONResponse(status_code=413, content={"detail": "Request body too large"})
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=(self)"
    if request.url.path.startswith(("/docs", "/redoc")):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; "
            "style-src 'self' https://cdn.jsdelivr.net; img-src 'self' data: https://fastapi.tiangolo.com; "
            "connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        )
    else:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        )
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


def clean_text(value: str) -> str:
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    value = value.replace("<", "").replace(">", "")
    return re.sub(r"\s+", " ", value).strip()


class CleanTextModel(BaseModel):
    @field_validator("*", mode="before")
    @classmethod
    def sanitize_strings(cls, value: Any) -> Any:
        return clean_text(value) if isinstance(value, str) else value


class PatientInfo(CleanTextModel):
    name: str = Field(min_length=2, max_length=120)
    age: int = Field(ge=0, le=120)
    village: str = Field(min_length=2, max_length=120)


class Vitals(BaseModel):
    pulse: float | None = Field(default=None, ge=0, le=300)
    spo2: float | None = Field(default=None, ge=0, le=100)
    temperature: float | None = Field(default=None, ge=0, le=50)
    respiration: float | None = Field(default=None, ge=0, le=100)
    tremor: float | None = Field(default=None, ge=0, le=100)
    ecg_mv: int | None = Field(default=None, ge=0, le=5000)
    ecg_leads_ok: bool | None = None
    air_co2_ppm: int | None = Field(default=None, ge=0, le=10000)
    finger_on_sensor: bool | None = None
    temp_ok: bool | None = None


class ReviewAssessment(CleanTextModel):
    level: Literal["unvalidated", "demo_low", "demo_review", "demo_high"] = "unvalidated"
    score: int | None = Field(default=None, ge=0, le=100)
    summary: str = Field(min_length=10, max_length=500)


class Consent(BaseModel):
    granted: Literal[True]
    version: Literal["prototype-notice-v1"]
    captured_at: datetime


class PatientPayload(CleanTextModel):
    local_id: str = Field(min_length=3, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$")
    device_id: str = Field(min_length=3, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$")
    patient: PatientInfo
    vitals: Vitals
    complaint: str = Field(min_length=2, max_length=1000)
    nurse_diagnosis: str = Field(default="", max_length=1000)
    ai_assessment: ReviewAssessment
    consent: Consent
    captured_at: datetime


def get_doctor_user_id() -> int | None:
    configured = os.getenv("DOCTOR_USER_ID", "").strip()
    if configured.isdecimal() and int(configured) > 0:
        return int(configured)
    with connect() as connection:
        row = connection.execute("SELECT value FROM app_settings WHERE key = 'doctor_user_id'").fetchone()
    return int(row["value"]) if row else None


def register_doctor_user(user_id: int) -> None:
    with connect() as connection:
        connection.execute("INSERT INTO app_settings(key, value) VALUES ('doctor_user_id', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(user_id),))


class SyncRequest(BaseModel):
    patients: list[PatientPayload] = Field(min_length=1, max_length=50)


class DoctorAction(CleanTextModel):
    patient_id: int = Field(gt=0)
    action: str = Field(min_length=2, max_length=60, pattern=r"^[A-Za-z0-9_-]+$")
    message: str = Field(min_length=1, max_length=600)


def patient_card(patient_id: int, payload: dict[str, Any]) -> str:
    patient = payload.get("patient", {})
    vitals = payload.get("vitals", {})
    assessment = payload.get("ai_assessment", {})
    assessment_labels = {
        "demo_low": "PAST DEMO SIGNAL",
        "demo_review": "KO‘RIB CHIQISH DEMO SIGNALI",
        "demo_high": "TEZKOR E’TIBOR DEMO SIGNALI",
        "unvalidated": "TAHLIL QILINMAGAN",
    }
    assessment_label = assessment_labels.get(str(assessment.get("level")), "TAHLIL QILINMAGAN")
    assessment_score = assessment.get("score")
    score_text = f" · {assessment_score}/100" if assessment_score is not None else ""
    return (
        "<b>DIGITAL BRIDGE — YANGI YOZUV</b>\n"
        "────────────────────\n"
        f"<b>#{patient_id} · {html.escape(str(patient.get('name', 'TODO')))}</b>\n"
        f"{html.escape(str(patient.get('age', '—')))} yosh · {html.escape(str(patient.get('village', '—')))}\n\n"
        f"Puls: <b>{vitals.get('pulse', '—')} BPM</b>\n"
        f"SpO₂: <b>{vitals.get('spo2', '—')}%</b>\n"
        f"Harorat: <b>{vitals.get('temperature', '—')}°C</b>\n"
        f"MQ-135 CO₂: <b>{vitals.get('air_co2_ppm', '—')} ppm</b> · ECG: <b>{vitals.get('ecg_mv', '—')} mV</b>\n"
        f"Nafas: <b>{vitals.get('respiration', '—')}/daq</b>\n\n"
        f"<b>Bemor aytgan shikoyat:</b> {html.escape(str(payload.get('complaint') or 'Kiritilmagan'))}\n\n"
        f"<b>Hamshira kuzatuvi (ko‘rik qaydi):</b> {html.escape(str(payload.get('nurse_diagnosis') or 'Kiritilmagan'))}\n"
        "<i>Hamshira ob’ektiv kuzatuvi; yakuniy tashxis shifokor tomonidan qo‘yiladi.</i>\n\n"
        f"<b>Avtomatik ko‘rib chiqish:</b> {assessment_label}{score_text}\n"
        "<i>Faqat ma’lumot uchun; professional tibbiy maslahat o‘rnini bosmaydi. "
        "Bu ichki hackathon test qoidasi, klinik xavf bahosi yoki tashxis emas.</i>"
    )


def action_keyboard(patient_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Qabul qilindi", callback_data=f"confirm:{patient_id}"),
                InlineKeyboardButton(text="Javob", callback_data=f"reply:{patient_id}"),
            ],
            [InlineKeyboardButton(text="Tez yordam ehtiyojini qayd etish", callback_data=f"ambulance:{patient_id}")],
        ]
    )


async def send_telegram_alert(patient_id: int, payload: dict[str, Any]) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    user_id = get_doctor_user_id()
    if not token or not user_id:
        return
    bot = Bot(token=token)
    try:
        message = await bot.send_message(user_id, "Planshetdan yozuv olinmoqda…  1/2")
        await asyncio.sleep(0.45)
        await bot.edit_message_text("Yozuv tayyorlanmoqda…  2/2", chat_id=user_id, message_id=message.message_id)
        await asyncio.sleep(0.45)
        await bot.edit_message_text(
            patient_card(patient_id, payload),
            chat_id=user_id,
            message_id=message.message_id,
            parse_mode="HTML",
            reply_markup=action_keyboard(patient_id),
        )
        with connect() as connection:
            connection.execute("UPDATE patients SET telegram_message_id = ? WHERE id = ?", (message.message_id, patient_id))
    except Exception:
        logger.exception("Telegram delivery failed for patient %s", patient_id)
    finally:
        await bot.session.close()


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "digital-bridge",
        "telegram_configured": bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip() and get_doctor_user_id()),
        "demo_mode": os.getenv("DEMO_MODE", "true").lower() == "true",
    }


CURRENT_HW_ZONE = "GREEN"


@app.get("/api/sensors/live")
async def sensors_live() -> dict[str, Any]:
    return telemetry.snapshot()


class AiAnalyzeRequest(CleanTextModel):
    patient_name: str = Field(default="Bemor", max_length=120)
    patient_age: int = Field(default=40, ge=0, le=120)
    mahalla: str = Field(default="Qishloq MFY", max_length=120)
    voice_text: str = Field(default="", max_length=1000)
    sensors: dict[str, Any] = Field(default_factory=dict)


class ZoneCommandRequest(CleanTextModel):
    zone: str = Field(default="GREEN", max_length=16)
    beep: bool = True


class SaveOfflineRequest(CleanTextModel):
    id: str = Field(default="", max_length=120)
    patient_name: str = Field(default="Noma'lum bemor", max_length=120)
    patient_age: int = Field(default=40, ge=0, le=120)
    mahalla: str = Field(default="Qishloq MFY", max_length=120)
    voice_text: str = Field(default="Ko'rik qaydi", max_length=1000)
    nurse_diagnosis: str = Field(default="", max_length=1000)
    sensors: dict[str, Any] = Field(default_factory=dict)


@app.post("/api/ai/analyze")
def ai_analyze(req: AiAnalyzeRequest) -> dict[str, Any]:
    global CURRENT_HW_ZONE
    s = req.sensors
    hr = int(s.get("heart_rate_bpm", 78) or 78)
    spo2 = int(s.get("spo2_percent", 98) or 98)
    temp = float(s.get("temperature_c", 36.6) or 36.6)
    co2 = int(s.get("air_co2_ppm", 435) or 435)
    finger = bool(s.get("finger_on_sensor", True))
    score = 0
    notes: list[str] = []
    if finger and hr >= 105:
        score += 20
        notes.append(f"Taxikardiya belgisi: puls {hr} BPM")
    if finger and 0 < spo2 <= 93:
        score += 35
        notes.append(f"Past saturatsiya: SpO₂ {spo2}%")
    if temp >= 38.0:
        score += 20
        notes.append(f"Yuqori tana harorati: {temp} °C")
    if co2 >= 1100:
        score += 15
        notes.append(f"MQ-135 havo/nafas gazi ko'rsatkichi yuqori: {co2} ppm")
    if any(k in req.voice_text.lower() for k in ("nafas", "og'riq", "og‘riq", "hush", "qis")):
        score += 15
        notes.append("Shikoyat matnida e'tibor talab qiluvchi belgilar qayd etildi.")
    score = max(0, min(100, score))
    zone = "RED" if score >= 60 else "YELLOW" if score >= 25 else "GREEN"
    CURRENT_HW_ZONE = zone
    if not notes:
        notes.append("Ko'rsatkichlar barqaror demo doirasida; standart kuzatuv davom ettiriladi.")
    summary_map = {
        "RED": "Tezkor shifokor ko'rigi uchun demo signal (klinik tashxis emas).",
        "YELLOW": "Shifokor ko'rib chiqishi uchun demo signal.",
        "GREEN": "Barqaror demo ko'rsatkichlar.",
    }
    return {
        "risk_zone": zone,
        "risk_score": score,
        "preliminary_diagnosis_uz": summary_map[zone],
        "nurse_instructions_uz": notes,
        "voice_reply_uz": summary_map[zone],
        "hardware_feedback": {"zone": zone, "beep": zone == "RED", "delivered": True},
    }


@app.post("/api/hardware/zone")
def hardware_zone(req: ZoneCommandRequest) -> dict[str, Any]:
    global CURRENT_HW_ZONE
    z = req.zone.strip().upper()
    CURRENT_HW_ZONE = z if z in ("RED", "YELLOW", "GREEN") else "GREEN"
    return {"zone": CURRENT_HW_ZONE, "beep": req.beep, "delivered": True, "timestamp": utc_now()}


@app.post("/api/patients/save_offline", status_code=201)
def save_offline(req: SaveOfflineRequest) -> dict[str, Any]:
    local_id = req.id or f"P-{uuid.uuid4().hex[:8].upper()}"
    now = utc_now()
    payload = {
        "local_id": local_id,
        "device_id": "web-client",
        "patient": {"name": req.patient_name, "age": req.patient_age, "village": req.mahalla},
        "vitals": {
            "pulse": req.sensors.get("heart_rate_bpm"),
            "spo2": req.sensors.get("spo2_percent"),
            "temperature": req.sensors.get("temperature_c"),
            "respiration": req.sensors.get("respiration_rate"),
            "ecg_mv": req.sensors.get("ecg_mv"),
            "ecg_leads_ok": req.sensors.get("ecg_leads_ok"),
            "air_co2_ppm": req.sensors.get("air_co2_ppm"),
            "finger_on_sensor": req.sensors.get("finger_on_sensor"),
            "temp_ok": req.sensors.get("temp_ok"),
        },
        "complaint": req.voice_text,
        "nurse_diagnosis": req.nurse_diagnosis,
        "ai_assessment": {"level": "unvalidated", "score": 0, "summary": "Oflayn saqlandi (demo)."},
        "consent": {"granted": True, "version": "prototype-notice-v1", "captured_at": now},
        "captured_at": now,
    }
    with connect() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO patients(local_id, device_id, payload, received_at) VALUES (?, ?, ?, ?)",
            (local_id, "web-client", json.dumps(payload, ensure_ascii=False), now),
        )
        total = connection.execute("SELECT COUNT(*) FROM patients").fetchone()[0]
    return {"saved": {"id": local_id, "created_at": now}, "pending_count": total, "total_count": total}


@app.post("/api/dmed/sync")
def dmed_sync(background_tasks: BackgroundTasks) -> dict[str, Any]:
    synced_at = utc_now()
    receipts: list[dict[str, Any]] = []
    with connect() as connection:
        rows = connection.execute("SELECT id, local_id, payload FROM patients ORDER BY id DESC LIMIT 25").fetchall()
        for row in rows:
            receipts.append({
                "patient_id": row["local_id"],
                "dmed_encounter_id": f"DMED-PAT-{row['id']}",
                "status": "DMED_SANDBOX_ACCEPTED",
                "synced_at": synced_at,
            })
            if not connection.execute("SELECT telegram_message_id FROM patients WHERE id = ?", (row["id"],)).fetchone()["telegram_message_id"]:
                background_tasks.add_task(send_telegram_alert, int(row["id"]), json.loads(row["payload"]))
    return {"synced_at": synced_at, "synced_count": len(receipts), "dmed_receipts": receipts, "telegram_results": []}


@app.get("/api/patients")
def list_patients() -> dict[str, Any]:
    with connect() as connection:
        rows = connection.execute("SELECT id, local_id, payload, received_at FROM patients ORDER BY id DESC LIMIT 50").fetchall()
    return {
        "total": len(rows),
        "patients": [{"id": r["local_id"], "received_at": r["received_at"], **json.loads(r["payload"])} for r in rows],
    }


@app.post("/api/patients/sync")
def sync_patients(request: SyncRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    acknowledgements: list[dict[str, Any]] = []
    with connect() as connection:
        for item in request.patients:
            payload = item.model_dump(mode="json")
            row = connection.execute("SELECT id FROM patients WHERE local_id = ?", (item.local_id,)).fetchone()
            created = row is None
            if created:
                cursor = connection.execute(
                    "INSERT INTO patients(local_id, device_id, payload, received_at) VALUES (?, ?, ?, ?)",
                    (item.local_id, item.device_id, json.dumps(payload, ensure_ascii=False), utc_now()),
                )
                patient_id = int(cursor.lastrowid)
                background_tasks.add_task(send_telegram_alert, patient_id, payload)
            else:
                patient_id = int(row["id"])
            acknowledgements.append({
                "local_id": item.local_id,
                "server_id": patient_id,
                "status": "created" if created else "duplicate",
            })
    return {
        "ok": True,
        "acknowledgements": acknowledgements,
        "telegram": "queued" if os.getenv("TELEGRAM_BOT_TOKEN", "").strip() and get_doctor_user_id() else "not_configured",
    }


def record_action(action: DoctorAction) -> dict[str, Any]:
    with connect() as connection:
        patient = connection.execute("SELECT device_id FROM patients WHERE id = ?", (action.patient_id,)).fetchone()
        if not patient:
            raise HTTPException(status_code=404, detail="Patient not found")
        cursor = connection.execute(
            "INSERT INTO doctor_actions(patient_id, device_id, action, message, created_at) VALUES (?, ?, ?, ?, ?)",
            (action.patient_id, patient["device_id"], action.action, action.message, utc_now()),
        )
        return {"ok": True, "reply_id": int(cursor.lastrowid)}


@app.post("/api/internal/doctor-actions")
def internal_doctor_action(action: DoctorAction, x_internal_key: str | None = Header(default=None)) -> dict[str, Any]:
    expected = os.getenv("INTERNAL_API_KEY", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="Internal API key is not configured")
    if not x_internal_key or not hmac.compare_digest(x_internal_key, expected):
        raise HTTPException(status_code=401, detail="Invalid internal key")
    return record_action(action)


@app.post("/api/demo/doctor-reply")
def demo_doctor_reply(action: DoctorAction) -> dict[str, Any]:
    if os.getenv("DEMO_MODE", "true").lower() != "true":
        raise HTTPException(status_code=403, detail="Demo mode disabled")
    return record_action(action)


@app.get("/api/doctor-replies")
def doctor_replies(device_id: str = Query(min_length=3, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$"), after_id: int = Query(default=0, ge=0)) -> dict[str, Any]:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT id, patient_id, action, message, created_at
            FROM doctor_actions
            WHERE device_id = ? AND id > ?
            ORDER BY id ASC LIMIT 100
            """,
            (device_id, after_id),
        ).fetchall()
    return {"replies": [dict(row) for row in rows]}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
