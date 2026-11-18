from contextlib import contextmanager
from threading import Thread
from time import monotonic, sleep
from uuid import uuid4

import pytest
import uvicorn
from fastapi import Response
from fastapi.testclient import TestClient

from patronage_demo import (
    LOOPBACK,
    create_receiver_app,
    create_tablet_app,
    local_app,
    loopback_listener,
)


def record():
    return {
        "id": str(uuid4()),
        "patient_code": "P-104",
        "patient_age": 62,
        "voice_text": "Bemor bosh og‘rig‘idan shikoyat qildi.",
        "captured_at": "2026-11-18T08:00:00Z",
        "sensors": {"heart_rate_bpm": 85},
    }


@contextmanager
def serve_locally(app):
    with loopback_listener(0) as listener:
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, ws="none"))
        worker = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        worker.start()
        try:
            deadline = monotonic() + 5
            while not server.started and worker.is_alive() and monotonic() < deadline:
                sleep(0.01)
            assert server.started
            yield f"http://{LOOPBACK}:{listener.getsockname()[1]}"
        finally:
            server.should_exit = True
            worker.join(timeout=5)


def test_tablet_to_receiver(tmp_path):
    visit = record()
    with serve_locally(create_receiver_app(tmp_path / "visits.db")) as url:
        client = TestClient(create_tablet_app(url))
        for path in ("/", "/assets/style.css", "/assets/app.js", "/example.json", "/sw.js"):
            assert client.get(path).status_code == 200
        receipt = client.post("/api/sync", json=visit)
        assert receipt.status_code == 200, receipt.text
        assert receipt.json()["id"] == visit["id"]
        journal = client.get("/api/journal").json()
        assert [row["id"] for row in journal] == [visit["id"]]
        assert journal[0]["sensors"]["heart_rate_bpm"] == 85
        assert journal[0]["sensors"]["spo2_percent"] is None
        assert journal[0]["received_at"] == receipt.json()["received_at"]


def test_retry_after_lost_receipt_preserves_original_receipt(tmp_path):
    receiver = create_receiver_app(tmp_path / "visits.db")
    drop_receipt = True

    @receiver.middleware("http")
    async def lose_first_receipt(request, call_next):
        nonlocal drop_receipt
        response = await call_next(request)
        if drop_receipt and request.method == "POST" and response.status_code == 200:
            drop_receipt = False
            return Response(status_code=503)
        return response

    with serve_locally(receiver) as url:
        client = TestClient(create_tablet_app(url))
        visit = record()
        assert client.post("/api/sync", json=visit).status_code == 503
        first = client.get("/api/journal").json()
        assert len(first) == 1
        repeated = client.post("/api/sync", json=visit)
        assert repeated.status_code == 200
        assert repeated.json() == {"id": visit["id"], "received_at": first[0]["received_at"]}
        assert client.post("/api/sync", json={**visit, "patient_age": 55}).status_code == 409
        assert client.get("/api/journal").json() == first


def test_invalid_record_cannot_be_received(tmp_path):
    with serve_locally(create_receiver_app(tmp_path / "visits.db")) as url:
        client = TestClient(create_tablet_app(url))
        visit = record()
        assert client.post("/api/sync", json={**visit, "id": "invalid"}).status_code == 422
        assert client.post("/api/sync", json={**visit, "voice_text": "123"}).status_code == 422
        assert client.get("/api/journal").json() == []


@pytest.mark.parametrize("ai_available", [False, True])
def test_local_ai_contract_and_sync_are_independent(tmp_path, ai_available):
    ai_app = local_app("AI API fixture")
    payload = {
        "patient_age": 62,
        "voice_text": "Bemor bosh og‘rig‘idan shikoyat qildi.",
        "sensors": {
            "heart_rate_bpm": 85,
            "spo2_percent": 97,
            "temperature_c": 36.7,
            "respiration_rate": 17,
            "tremor_detected": False,
            "lung_audio_status": "normal",
        },
    }
    reply = {
        "risk_zone": "YELLOW",
        "risk_score": 50,
        "preliminary_diagnosis_uz": "Sintetik javob",
        "nurse_instructions_uz": ["Sintetik ko‘rsatma"],
        "voice_reply_uz": "Sariq zona",
    }

    @ai_app.post("/analyze")
    def analyze(visit: dict, speak: bool):
        assert visit == payload
        assert speak is False
        return reply if ai_available else Response(status_code=503)

    with serve_locally(create_receiver_app(tmp_path / "visits.db")) as receiver_url, serve_locally(ai_app) as ai_url:
        client = TestClient(create_tablet_app(receiver_url, ai_url))
        response = client.post("/api/analyze", json=payload)
        assert response.status_code == (200 if ai_available else 503)
        visit = record()
        if ai_available:
            assert response.json() == reply
            visit["assessment"] = reply
        assert client.post("/api/sync", json=visit).status_code == 200
        expected = reply if ai_available else None
        assert client.get("/api/journal").json()[0]["assessment"] == expected
