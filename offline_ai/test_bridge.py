from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from fastapi.testclient import TestClient

import app
import speech
from triage import Finding, ServiceError, Visit, classify


EXAMPLE = Visit.model_validate_json(Path(__file__).with_name("example.json").read_bytes())
NORMAL = dict(heart_rate_bpm=76, spo2_percent=98, temperature_c=36.6, respiration_rate=16,
              tremor_detected=False, lung_audio_status="normal")
client = TestClient(app.app)


@pytest.mark.parametrize("text,kind,sensors,zone", [
    ("Shikoyati yo‘q", "no_complaints", NORMAL, "GREEN"),
    ("Ahvoli yaxshi, bemorning hech qanday shikoyati yo‘q.", "no_complaints", NORMAL, "GREEN"),
    ("Шикояти йўқ", "no_complaints", NORMAL, "GREEN"),
    ("Shikoyati yo‘q. Nafasi qisilyapti.", "no_complaints", NORMAL, "YELLOW"),
    ("Ko‘kragim og‘riyapti", "chest_pain", NORMAL, "RED"),
    ("Hozir ko‘kragim juda qattiq og‘riyapti", "other_symptom", NORMAL, "RED"),
    ("Кўкрак қафасида кучли оғриқ бор", "other_symptom", NORMAL, "RED"),
    ("Ko‘krakda og‘riq yo‘q, boshim og‘riyapti", "other_symptom", NORMAL, "YELLOW"),
    ("Ko‘kragim og‘rimaydi, faqat boshim og‘riyapti", "other_symptom", NORMAL, "YELLOW"),
    ("Boshim og‘riyapti", "other_symptom", NORMAL, "YELLOW"),
    ("Shikoyati yo‘q", "no_complaints", dict(NORMAL, spo2_percent=90), "RED"),
    ("Boshim og‘riyapti", "no_complaints", NORMAL, "YELLOW"),
])
def test_paired_cases(text, kind, sensors, zone):
    with patch("app.classify", return_value=Finding(kind=kind, quote=text)):
        res = client.post("/analyze?speak=false", json=dict(patient_age=62, voice_text=text, sensors=sensors))
    assert res.status_code == 200 and res.json()["risk_zone"] == zone
    assert set(res.json()) == set(app.Reply.model_fields)


@pytest.mark.parametrize("change", [dict(patient_age=8), dict(voice_text="  "),
                                    dict(sensors=dict(NORMAL, spo2_percent=101)),
                                    dict(sensors=dict(NORMAL, lung_audio_status="unknown")),
                                    dict(sensors={"heart_rate_bpm": 77})])
def test_rejects_incomplete_or_unsupported(change):
    assert client.post("/analyze", json={**EXAMPLE.model_dump(), **change}).status_code == 422


@pytest.mark.parametrize("status,word", [
    ("not_recorded", "yozilmagan"),
    ("crackles_detected", "Nafas olish bilan bog‘liq"),
])
def test_team_backend_lung_audio_status(status, word):
    text = "Shikoyati yo‘q"
    visit = {"patient_age": 62, "voice_text": text, "sensors": dict(NORMAL, lung_audio_status=status)}
    with patch("app.classify", return_value=Finding(kind="no_complaints", quote=text)):
        response = client.post("/analyze?speak=false", json=visit)
    assert response.status_code == 200
    assert response.json()["risk_zone"] == "YELLOW"
    assert word in response.json()["preliminary_diagnosis_uz"]


def test_audio_failure_keeps_written_result_and_urgent_fallback():
    with patch("app.classify", side_effect=ServiceError(503, "model")), patch("speech.synthesize", side_effect=OSError):
        result = client.post("/analyze", json=EXAMPLE.model_dump())
        assert result.json()["risk_zone"] == "RED"
        assert result.headers["X-Voice-Status"] == "unavailable"
        assert result.headers["X-Assessment-Source"] == "urgent-sensor-fallback"
        chest = client.post("/analyze", json={**EXAMPLE.model_dump(), "sensors": NORMAL})
        assert chest.json()["risk_zone"] == "RED"
        assert chest.headers["X-Assessment-Source"] == "urgent-symptom-fallback"
        mild = {**EXAMPLE.model_dump(), "sensors": NORMAL, "voice_text": "Boshim og‘riyapti"}
        assert client.post("/analyze", json=mild).status_code == 503


def test_silence_and_bad_audio():
    for data in (b"not audio", speech.wav_bytes(np.zeros(16000))):
        assert client.post("/transcribe", content=data).status_code == 422
    assert client.post("/transcribe", content=b"x" * 1_000_001).status_code == 413


def test_ambiguous_transcript_requests_clarification():
    with patch("app.classify", return_value=Finding(kind="unclear", quote="xxx")):
        res = client.post("/analyze", json=dict(patient_age=20, sensors=NORMAL, voice_text="xxx"))
    assert res.status_code == 422


@pytest.mark.parametrize("content,status", [
    ("broken json", 503),
    ('{"kind":"chest_pain","quote":"unreported text"}', 422),
])
def test_invalid_model_evidence(content, status):
    with patch("triage.httpx.Client") as constructor:
        post = constructor.return_value.__enter__.return_value.post
        post.return_value.json.return_value = {"message": {"content": content}}
        with pytest.raises(ServiceError) as info:
            classify(EXAMPLE)
        assert info.value.status == status
        payload = post.call_args.kwargs["json"]
        assert Visit.model_validate_json(payload["messages"][-1]["content"]) == EXAMPLE
