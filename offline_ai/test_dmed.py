import json
from dataclasses import replace
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
from fastapi.testclient import TestClient

from app import app
from dhp_sync import DHP_PLAYGROUND, Playground, build_transaction
from patronage_demo import RecordedVisit, create_receiver_app, create_tablet_app
from protocols import IITT, PNEUMONIA, retrieve
from test_bridge import EXAMPLE, NORMAL
from test_patronage_demo import record, serve_locally
from triage import Finding, Visit, classify

PLAYGROUND = Playground("https://sso.dhp.uz/token", "demo-id", "demo-secret", {"P-104": "synthetic-104"})


def test_relevant_passages_are_bundled_locally_and_the_api_keeps_five_fields():
    assert {p.url for p in retrieve(EXAMPLE)} == {IITT, PNEUMONIA}
    normal = Visit.model_validate(dict(patient_age=62, voice_text="Shikoyati yo‘q.", sensors=NORMAL))
    assert retrieve(normal) == ()
    cyrillic = EXAMPLE.model_copy(update={"voice_text": "Нафаси қисяпти."})
    assert {p.url for p in retrieve(cyrillic)} == {IITT, PNEUMONIA}
    with patch("app.classify", return_value=Finding(kind="breathing", quote="nafas qisishidan")):
        response = TestClient(app).post("/analyze?speak=false", json=EXAMPLE.model_dump())
    assert response.status_code == 200 and len(response.json()) == 5
    assert IITT in response.headers["Link"] and PNEUMONIA in response.headers["Link"]


def test_retrieved_passages_enter_ollama_context():
    with patch("triage.httpx.Client") as constructor:
        post = constructor.return_value.__enter__.return_value.post
        post.return_value.json.return_value = {"message": {"content": '{"kind":"breathing","quote":"nafas qisishidan"}'}}
        assert classify(EXAMPLE).kind == "breathing"
    context = post.call_args.kwargs["json"]["messages"][0]["content"]
    assert "pulse oximetry helps assess hypoxemia" in context


def test_fhir_payload_uses_confirmed_patient_and_skips_missing_measurements():
    visit = RecordedVisit.model_validate(record())
    transaction = build_transaction(visit, "synthetic-104")
    encounter, pulse = transaction["entry"]
    assert encounter["resource"]["subject"]["reference"] == "Patient/synthetic-104"
    assert encounter["resource"]["reason"][0]["value"][0]["concept"]["text"] == visit.voice_text
    assert pulse["resource"]["encounter"]["reference"] == encounter["fullUrl"]
    assert pulse["resource"]["valueQuantity"]["code"] == "/min"
    assert pulse["resource"]["code"]["coding"][0]["code"] == "8867-4"
    assert all(entry["resource"]["identifier"][0]["value"] == entry["fullUrl"] for entry in transaction["entry"])
    assert transaction == build_transaction(visit, "synthetic-104")


def test_successful_transaction_receipt(tmp_path):
    visit = record() | {"sensors": EXAMPLE.model_dump()["sensors"]}
    calls = []

    def remote(request):
        if request.url.host == "sso.dhp.uz":
            return httpx.Response(200, json={"access_token": "synthetic-token"})
        calls.append(request.method)
        entries = json.loads(request.content)["entry"]
        assert len(entries) == 5
        assert {e["resource"]["valueQuantity"]["value"] for e in entries[1:]} == {118, 91, 38.4, 25}
        return httpx.Response(200, json={
            "resourceType": "Bundle", "type": "transaction-response",
            "entry": [{"response": {"status": "201 Created", "location": f"{e['request']['url']}/fixture"}}
                      for e in entries],
        })

    deliver = Playground.deliver
    with patch.object(Playground, "deliver", lambda self, item, patient, **callbacks:
                      deliver(self, item, patient, transport=httpx.MockTransport(remote), **callbacks)):
        receiver = TestClient(create_receiver_app(tmp_path / "demo.db", PLAYGROUND))
        first = receiver.post("/visits", json=visit)
        assert first.status_code == 200, first.text
        assert receiver.post("/visits", json=visit).json() == first.json()
    assert calls == ["POST"]


def test_tablet_sync_waits_for_complete_fhir_receipt_across_restart(tmp_path):
    requests = []
    searches = []
    mode = "offline"

    def remote(request):
        if request.url.host == "sso.dhp.uz":
            assert parse_qs(request.content.decode())["grant_type"] == ["client_credentials"]
            return httpx.Response(200, json={"access_token": "synthetic-token"})
        assert request.headers["Authorization"] == "Bearer synthetic-token"
        if request.method == "GET":
            identifier = request.url.params["identifier"]
            searches.append(identifier)
            hits = [{"resource": entry["resource"]} for entry in requests[-1]["entry"]
                    if entry["fullUrl"] == identifier.split("|")[1]]
            return httpx.Response(200, json={"resourceType": "Bundle", "type": "searchset",
                                             "entry": hits if mode == "online" else []})
        assert str(request.url) == DHP_PLAYGROUND
        body = json.loads(request.content)
        requests.append(body)
        if mode == "offline":
            raise httpx.ConnectError("test disconnect")
        created = [{"response": {"status": "201 Created", "location": f"{e['request']['url']}/synthetic"}}
                   for e in body["entry"]]
        if mode == "partial":
            created[-1]["response"]["status"] = "422 Unprocessable Content"
        return httpx.Response(200, json={"resourceType": "Bundle", "type": "transaction-response", "entry": created})

    deliver = Playground.deliver
    db = tmp_path / "queue.db"
    visit = record()
    with patch.object(Playground, "deliver", lambda self, item, patient, **callbacks:
                      deliver(self, item, patient, transport=httpx.MockTransport(remote), **callbacks)):
        with serve_locally(create_receiver_app(db, PLAYGROUND)) as receiver:
            tablet = TestClient(create_tablet_app(receiver, dhp=True))
            assert "DHP · FHIR" in tablet.get("/").text
            assert tablet.post("/api/sync", json=visit | {"patient_code": "unmapped"}).status_code == 422
            assert not requests
            assert tablet.post("/api/sync", json=visit).status_code == 503
            mode = "partial"
            assert tablet.post("/api/sync", json=visit).status_code == 503
            assert tablet.post("/api/sync", json=visit).status_code == 503
            assert tablet.get("/api/journal").json() == []
        switched = replace(PLAYGROUND, patient_ids={"P-104": "other-patient"})
        assert TestClient(create_receiver_app(db, switched)).post("/visits", json=visit).status_code == 409
        mode = "online"
        with serve_locally(create_receiver_app(db, PLAYGROUND)) as receiver:
            tablet = TestClient(create_tablet_app(receiver, dhp=True))
            receipt = tablet.post("/api/sync", json=visit)
            assert receipt.status_code == 200, receipt.text
            assert tablet.post("/api/sync", json=visit).json() == receipt.json()
            assert len(tablet.get("/api/journal").json()) == 1
    assert len(requests) == 2 and requests[0] == requests[1]
    assert len(searches) == 3
