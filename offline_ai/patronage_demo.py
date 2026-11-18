"""Local tablet and receiving services for the synthetic patronage demo."""

import argparse
import json
import socket
import sqlite3
import sys
import webbrowser
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread
from time import monotonic, sleep

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AwareDatetime, Field, UUID4

from dhp_sync import DeliveryError, Playground
from triage import LungAudioStatus, Reply, StrictModel, Visit

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "patronage"
LOOPBACK = "127.0.0.1"
TABLET_PORT = 8002
RECEIVER_PORT = 8003
AI_URL = f"http://{LOOPBACK}:8001"
RECEIVER_UNAVAILABLE = "Sinov serveri javob bermadi. Tashrif navbatda qoladi."


class RecordedSensors(StrictModel):
    heart_rate_bpm: int | None = Field(default=None, ge=1, le=320)
    spo2_percent: int | None = Field(default=None, ge=1, le=100)
    temperature_c: float | None = Field(default=None, ge=20, le=50, allow_inf_nan=False)
    respiration_rate: int | None = Field(default=None, ge=1, le=100)
    tremor_detected: bool | None = None
    lung_audio_status: LungAudioStatus | None = None


class RecordedVisit(Visit):
    id: UUID4 = Field(strict=False)
    patient_code: str = Field(pattern=r"^[A-Za-z0-9_-]{1,32}$")
    captured_at: AwareDatetime = Field(strict=False)
    sensors: RecordedSensors = Field(default_factory=RecordedSensors)
    assessment: Reply | None = None


class Receipt(StrictModel):
    id: UUID4 = Field(strict=False)
    received_at: AwareDatetime = Field(strict=False)


class ReceivedVisit(RecordedVisit):
    received_at: AwareDatetime = Field(strict=False)


def local_app(title):
    return FastAPI(title=title, openapi_url=None, docs_url=None, redoc_url=None)


def create_receiver_app(db_path, playground=None):
    app = local_app("Patronaj sinov serveri")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS visits (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                received_at TEXT NOT NULL
            )
        """)
        if playground:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS dhp_delivery (
                    visit_id TEXT PRIMARY KEY,
                    patient_id TEXT NOT NULL,
                    delivered_at TEXT,
                    submitted INTEGER NOT NULL DEFAULT 0
                )
            """)

    @app.post("/visits", response_model=Receipt)
    def receive_visit(visit: RecordedVisit):
        payload = visit.model_dump_json()
        visit_id = str(visit.id)
        received_at = datetime.now(timezone.utc).isoformat()
        patient = playground.patient_ids.get(visit.patient_code) if playground else None
        if playground and patient is None:
            raise HTTPException(422, "Tashrif kodi DHP sinov bemoriga bog‘lanmagan.")
        with closing(sqlite3.connect(db_path, timeout=2)) as connection, connection:
            connection.execute(
                "INSERT INTO visits VALUES (?, ?, ?) ON CONFLICT(id) DO NOTHING",
                (visit_id, payload, received_at),
            )
            stored_payload, stored_at = connection.execute(
                "SELECT payload, received_at FROM visits WHERE id = ?", (visit_id,),
            ).fetchone()
            if stored_payload != payload:
                raise HTTPException(409, "Bu tashrif kodi boshqa yozuvga tegishli.")
            if playground:
                connection.execute("INSERT INTO dhp_delivery (visit_id, patient_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
                                   (visit_id, patient))
                bound_patient, delivered_at = connection.execute(
                    "SELECT patient_id, delivered_at FROM dhp_delivery WHERE visit_id = ?", (visit_id,)
                ).fetchone()
                if bound_patient != patient:
                    raise HTTPException(409, "Bu tashrif boshqa DHP bemoriga bog‘langan.")
        if playground and not delivered_at:
            def set_submission(value, expected):
                with closing(sqlite3.connect(db_path, timeout=2)) as conn, conn:
                    return conn.execute(
                        "UPDATE dhp_delivery SET submitted = ? "
                        "WHERE visit_id = ? AND submitted = ? AND delivered_at IS NULL",
                        (value, visit_id, expected),
                    ).rowcount == 1

            try:
                playground.deliver(visit, bound_patient, claim=lambda: set_submission(1, 0),
                                   reset=lambda: set_submission(0, 1))
            except DeliveryError as exc:
                raise HTTPException(exc.status, exc.detail) from exc
            with closing(sqlite3.connect(db_path, timeout=2)) as connection, connection:
                delivered_at = connection.execute(
                    "UPDATE dhp_delivery SET delivered_at = COALESCE(delivered_at, ?) "
                    "WHERE visit_id = ? RETURNING delivered_at",
                    (datetime.now(timezone.utc).isoformat(), visit_id),
                ).fetchone()[0]
        if playground:
            stored_at = delivered_at
        return Receipt(id=visit.id, received_at=stored_at)

    @app.get("/visits", response_model=list[ReceivedVisit])
    def journal():
        sql = ("SELECT visits.payload, d.delivered_at FROM visits JOIN dhp_delivery d ON d.visit_id = visits.id "
               "WHERE d.delivered_at IS NOT NULL ORDER BY d.delivered_at DESC"
               if playground else "SELECT payload, received_at FROM visits ORDER BY received_at DESC")
        with closing(sqlite3.connect(db_path, timeout=2)) as connection:
            records = connection.execute(sql).fetchall()
        return [json.loads(payload) | {"received_at": received_at} for payload, received_at in records]

    return app


def forward_json(method, url, unavailable, *, data=None, timeout=3):
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=1), trust_env=False) as client:
            response = client.request(method, url, json=data)
        if response.is_server_error:
            raise HTTPException(503, unavailable)
        content = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, unavailable) from exc
    links = {"Link": response.headers["Link"]} if "Link" in response.headers else None
    return JSONResponse(status_code=response.status_code, content=content, headers=links)


def create_tablet_app(receiver_url, ai_url=AI_URL, *, dhp=False):
    app = local_app("Patronaj plansheti")
    app.mount("/assets", StaticFiles(directory=ASSETS), name="patronage_assets")

    @app.get("/")
    def tablet():
        if dhp:
            page = (ASSETS / "index.html").read_text("utf-8")
            return HTMLResponse(page.replace("Xakaton · Sinov muhiti", "DHP · FHIR sinov muhiti"),
                                headers={"Cache-Control": "no-cache"})
        return FileResponse(ASSETS / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/sw.js")
    def worker():
        return FileResponse(ASSETS / "sw.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/example.json")
    def example():
        return FileResponse(ROOT / "example.json", media_type="application/json")

    @app.post("/api/sync")
    def send_visit(visit: RecordedVisit):
        return forward_json("POST", f"{receiver_url}/visits", RECEIVER_UNAVAILABLE,
                            data=visit.model_dump(mode="json"), timeout=35 if dhp else 3)

    @app.get("/api/journal")
    def load_journal():
        return forward_json("GET", f"{receiver_url}/visits", RECEIVER_UNAVAILABLE)

    @app.post("/api/analyze")
    def analyze(visit: Visit):
        return forward_json("POST", f"{ai_url}/analyze?speak=false",
                            "Lokal AI xizmati javob bermadi. Tashrifni saqlashingiz mumkin.",
                            data=visit.model_dump(mode="json"), timeout=100)

    return app


@contextmanager
def loopback_listener(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((LOOPBACK, port))
        yield listener


def open_when_ready(tablet, receiver, url):
    deadline = monotonic() + 10
    while not tablet.should_exit and not receiver.should_exit and monotonic() < deadline:
        if tablet.started and receiver.started:
            webbrowser.open_new_tab(url)
            return
        sleep(0.1)


def main():
    parser = argparse.ArgumentParser(description="Patronaj plansheti va sinov serveri")
    parser.add_argument("--tablet-port", type=int)
    parser.add_argument("--receiver-port", type=int)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--dhp-playground", action="store_true", help="DHP FHIR sinov muhitiga yuborish")
    parser.add_argument("--open", action="store_true", help="Planshetni brauzerda ochish")
    args = parser.parse_args()

    playground = Playground.from_environment() if args.dhp_playground else None
    default_port = 8004 if playground else TABLET_PORT
    port = default_port if args.tablet_port is None else args.tablet_port
    default_receiver = 8005 if playground else RECEIVER_PORT
    receiver_port = default_receiver if args.receiver_port is None else args.receiver_port
    db = args.db or ROOT / (".runtime/patronage-dhp.sqlite3" if playground else ".runtime/patronage-demo.sqlite3")
    with loopback_listener(port) as tablet_socket, loopback_listener(receiver_port) as receiver_socket:
        url = f"http://{LOOPBACK}:{tablet_socket.getsockname()[1]}"
        receiver_url = f"http://{LOOPBACK}:{receiver_socket.getsockname()[1]}"
        receiver = uvicorn.Server(uvicorn.Config(create_receiver_app(db, playground), log_level="warning", access_log=False))
        tablet = uvicorn.Server(uvicorn.Config(create_tablet_app(receiver_url, dhp=bool(playground)),
                                              log_level="warning", access_log=False))
        receiver_thread = Thread(target=receiver.run, kwargs={"sockets": [receiver_socket]}, daemon=True)
        receiver_thread.start()
        print(f"Patronaj plansheti  {url}  |  Ctrl+C", flush=True)
        if args.open:
            Thread(target=open_when_ready, args=(tablet, receiver, url), daemon=True).start()
        try:
            tablet.run(sockets=[tablet_socket])
        finally:
            receiver.should_exit = True
            receiver_thread.join(timeout=5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Demo to‘xtatildi.", flush=True)
    except (OSError, ValueError) as exc:
        sys.exit(f"Demo ishga tushmadi. {exc}")
