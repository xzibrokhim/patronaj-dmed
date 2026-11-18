"""Send a patronage visit to the DHP FHIR playground as an atomic transaction."""
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid5

import httpx

DHP_PLAYGROUND = "https://playground.dhp.uz/fhir"
UZ_PROFILE = "https://dhp.uz/fhir/core/StructureDefinition/uz-core-"
FHIR_PROFILE = "http://hl7.org/fhir/StructureDefinition/"
MEDIA = "application/fhir+json"
UUID_SYSTEM = "urn:ietf:rfc:3986"
MEASUREMENTS = (
    ("heart_rate_bpm", "8867-4", "/min", "heartrate"),
    ("spo2_percent", "2708-6", "%", "oxygensat"),
    ("temperature_c", "8310-5", "Cel", "bodytemp"),
    ("respiration_rate", "9279-1", "/min", "resprate"),
)


class DeliveryError(Exception):
    def __init__(self, status, detail):
        self.status = status
        self.detail = detail
        super().__init__(detail)


def transaction_entry(resource, value):
    reference = f"urn:uuid:{value}"
    resource["identifier"] = [{"system": UUID_SYSTEM, "value": reference}]
    return {
        "fullUrl": reference,
        "resource": resource,
        "request": {"method": "POST", "url": resource["resourceType"]},
    }


def build_transaction(visit, patient_id):
    patient = {"reference": f"Patient/{patient_id}"}
    timestamp = visit.captured_at.isoformat()
    encounter = transaction_entry({
        "resourceType": "Encounter",
        "meta": {"profile": [UZ_PROFILE + "encounter"]},
        "status": "completed",
        "class": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "HH"}]}],
        "subject": patient,
        "actualPeriod": {"start": timestamp, "end": timestamp},
        "reason": [{"value": [{"concept": {"text": visit.voice_text}}]}],
    }, visit.id)
    entries = [encounter]
    for name, loinc, unit, profile in MEASUREMENTS:
        value = getattr(visit.sensors, name)
        if value is None:
            continue
        codings = [{"system": "http://loinc.org", "code": loinc}]
        if name == "spo2_percent":
            codings.append({"system": "http://loinc.org", "code": "59408-5"})
        observation = {
            "resourceType": "Observation",
            "meta": {"profile": [UZ_PROFILE + "observation", FHIR_PROFILE + "vitalsigns", FHIR_PROFILE + profile]},
            "status": "final",
            "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category",
                                      "code": "vital-signs"}]}],
            "code": {"coding": codings},
            "subject": patient,
            "encounter": {"reference": encounter["fullUrl"]},
            "effectiveDateTime": timestamp,
            "valueQuantity": {"value": value, "unit": unit, "system": "http://unitsofmeasure.org", "code": unit},
        }
        entries.append(transaction_entry(observation, uuid5(visit.id, name)))
    return {"resourceType": "Bundle", "type": "transaction", "entry": entries}


@dataclass(frozen=True)
class Playground:
    token_url: str
    client_id: str
    client_secret: str = field(repr=False)
    patient_ids: dict[str, str] = field(repr=False)
    scope: str = ""

    @classmethod
    def from_environment(cls):
        token_url = os.environ.get("DHP_TOKEN_URL", "")
        client_id = os.environ.get("DHP_CLIENT_ID", "")
        secret = os.environ.get("DHP_CLIENT_SECRET", "")
        mapping_path = os.environ.get("DHP_PATIENT_MAP", "")
        url = urlsplit(token_url)
        if (url.scheme, url.netloc) != ("https", "sso.dhp.uz") or not url.path or not all((client_id, secret, mapping_path)):
            raise ValueError("DHP_TOKEN_URL, DHP_CLIENT_ID, DHP_CLIENT_SECRET va DHP_PATIENT_MAP kerak.")
        mapping = json.loads(Path(mapping_path).read_text("utf-8-sig"))
        if not isinstance(mapping, dict) or not mapping or any(
            not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9.-]{1,64}", value)
            for value in mapping.values()
        ):
            raise ValueError("DHP_PATIENT_MAP ichida mahalliy kod va FHIR Patient ID mosligi kerak.")
        return cls(token_url, client_id, secret, mapping, os.environ.get("DHP_SCOPE", ""))

    def deliver(self, visit, patient, *, claim, reset, transport=None):
        transaction = build_transaction(visit, patient)
        auth = {
            "grant_type": "client_credentials", "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        if self.scope:
            auth["scope"] = self.scope
        try:
            with httpx.Client(timeout=httpx.Timeout(5, connect=2), trust_env=False, transport=transport) as client:
                token = client.post(self.token_url, data=auth).raise_for_status().json()["access_token"]
                if not isinstance(token, str) or not token.strip():
                    raise ValueError("Invalid access token")
                headers = {"Authorization": f"Bearer {token}", "Content-Type": MEDIA, "Accept": MEDIA}
                if not claim():
                    confirm_existing(client, headers, transaction)
                    return
                try:
                    response = client.post(DHP_PLAYGROUND, json=transaction, headers=headers)
                except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                    reset()
                    raise
            if response.status_code in (400, 409, 412, 422):
                reset()
                raise DeliveryError(422, f"DHP FHIR tashrifni rad etdi (HTTP {response.status_code}).")
            if response.status_code in (401, 403, 429):
                reset()
            response.raise_for_status()
            body = response.json()
            entries = body.get("entry", [])
            if (body.get("resourceType") != "Bundle" or body.get("type") != "transaction-response"
                    or len(entries) != len(transaction["entry"])
                    or any(not re.fullmatch(r"(?:200|201)(?: .*)?", entry["response"]["status"])
                           or not entry["response"].get("location") for entry in entries)):
                raise ValueError("Missing successful FHIR transaction receipt")
        except (httpx.HTTPError, ValueError, AttributeError, TypeError, KeyError) as exc:
            raise DeliveryError(503, "DHP qabul tasdig‘i olinmadi. Tashrif navbatda qoladi.") from exc


def confirm_existing(client, headers, transaction):
    """Reconcile a lost response without repeating a possibly committed POST."""
    for expected in transaction["entry"]:
        resource = expected["resource"]
        identifier = resource["identifier"][0]
        response = client.get(f"{DHP_PLAYGROUND}/{resource['resourceType']}", headers=headers, params={
            "identifier": f"{identifier['system']}|{identifier['value']}", "_count": 2,
        }).raise_for_status().json()
        if response.get("resourceType") != "Bundle" or response.get("type") != "searchset":
            raise ValueError("Invalid FHIR search response")
        matches = [hit["resource"] for hit in response.get("entry", [])
                   if hit.get("search", {}).get("mode", "match") == "match"]
        if len(matches) > 1:
            raise DeliveryError(409, "DHP’da bir xil tashrif identifikatori bir necha marta topildi.")
        identifiers = matches[0].get("identifier", []) if matches else []
        found_identifier = any(item.get("system") == identifier["system"]
                               and item.get("value") == identifier["value"] for item in identifiers)
        if (len(matches) != 1 or matches[0].get("resourceType") != resource["resourceType"]
                or not found_identifier
                or matches[0].get("subject", {}).get("reference") != resource["subject"]["reference"]):
            raise DeliveryError(503, "DHP tasdig‘i tekshirilmoqda. Tashrif navbatda qoladi.")
