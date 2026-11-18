"""Automated unit & HTTP integration tests for AI-Patronaj DMED backend."""

import json
import os
import tempfile
import threading
import unittest
import urllib.request

from live_server import (
    build_dmed_encounter_payload,
    create_server,
    deterministic_triage_fallback,
)


class FakeSerial:
    def __init__(self) -> None:
        self.is_open = True
        self.written: list[bytes] = []

    def write(self, data: bytes) -> int:
        self.written.append(data)
        return len(data)

    def flush(self) -> None:
        pass


class PatronajServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test_patronaj.db")
        self.httpd, self.hw_state, self.db, self.stop_event = create_server(
            bind="127.0.0.1",
            http_port=0,
            db_path=self.db_path,
            ai_url="",  # Use deterministic fallback during offline test.
        )
        self.port = self.httpd.server_address[1]
        self.server_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.server_thread.start()

    def tearDown(self) -> None:
        self.stop_event.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmpdir.cleanup()

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_live_sensors_and_bidirectional_zone_command(self) -> None:
        fake_ser = FakeSerial()
        self.hw_state.set_serial_handle(fake_ser, "COM51")
        self.hw_state.set_packet(
            "COM51",
            {
                "hr": 118,
                "spo2": 90,
                "temp": 38.6,
                "resp_rate": 26,
                "tremor": False,
                "tremor_hz": 0.2,
                "co2": 480,
                "g": 1.01,
                "fall": False,
                "finger": True,
                "button_start": False,
                "zone": "GREEN",
            },
            transport="usb",
        )

        status, live = self._request("GET", "/api/sensors/live")
        self.assertEqual(status, 200)
        self.assertTrue(live["connected"])
        self.assertEqual(live["sensors"]["heart_rate_bpm"], 118)
        self.assertEqual(live["sensors"]["spo2_percent"], 90)
        self.assertAlmostEqual(live["sensors"]["temperature_c"], 38.6)

        status, ai_resp = self._request(
            "POST",
            "/api/ai/analyze",
            {
                "patient_age": 64,
                "voice_text": "Bemor ko'krakda og'riq va nafas qisishidan shikoyat qilyapti",
                "lung_audio_status": "wheezing_detected",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(ai_resp["risk_zone"], "RED")
        self.assertTrue(ai_resp["hardware_feedback"]["delivered"])
        self.assertEqual(fake_ser.written[-1], b"ZONE:RED:1\n")

    def test_offline_save_and_dmed_sync_flow(self) -> None:
        self.hw_state.set_packet(
            "COM51",
            {
                "hr": 74,
                "spo2": 98,
                "temp": 36.6,
                "resp_rate": 16,
                "tremor": False,
                "finger": True,
            },
            transport="usb",
        )
        status, saved_resp = self._request(
            "POST",
            "/api/patients/save_offline",
            {
                "patient_name": "Karimova Dilnoza",
                "patient_age": 54,
                "patient_pinfl": "31205720110023",
                "mahalla": "Oqdaryo MFY",
                "voice_text": "Profilaktik ko'rik, shikoyat yo'q",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(saved_resp["pending_count"], 1)
        dmed = saved_resp["saved"]["dmed_payload"]
        self.assertEqual(dmed["system"], "DMED-UZ-MINZDRAV")
        self.assertEqual(dmed["patient"]["pinfl_or_temp_id"], "31205720110023")

        status, queue_before = self._request("GET", "/api/patients")
        self.assertEqual(status, 200)
        self.assertEqual(queue_before["pending_count"], 1)
        self.assertEqual(queue_before["synced_count"], 0)

        status, sync_resp = self._request("POST", "/api/dmed/sync", {})
        self.assertEqual(status, 200)
        self.assertEqual(sync_resp["synced_count"], 1)
        self.assertEqual(sync_resp["dmed_receipts"][0]["status"], "DMED_SANDBOX_ACCEPTED")

        status, queue_after = self._request("GET", "/api/patients")
        self.assertEqual(status, 200)
        self.assertEqual(queue_after["pending_count"], 0)
        self.assertEqual(queue_after["synced_count"], 1)

    def test_dmed_payload_and_fallback_triage(self) -> None:
        sensors = {
            "heart_rate_bpm": 122,
            "spo2_percent": 89,
            "temperature_c": 38.9,
            "respiration_rate": 28,
            "tremor_detected": True,
            "tremor_hz": 5.2,
            "lung_audio_status": "crackles_detected",
        }
        ai_res = deterministic_triage_fallback(68, "Nafas qisyapti", sensors)
        self.assertEqual(ai_res["risk_zone"], "RED")
        self.assertGreaterEqual(ai_res["risk_score"], 70)

        bundle = build_dmed_encounter_payload(
            record_id="P-DEMO01",
            created_at="2026-11-18T10:00:00Z",
            patient_name="Aliyev Vali",
            patient_age=68,
            patient_pinfl="",
            mahalla="Qorako'l MFY",
            voice_text="Nafas qisyapti",
            sensors=sensors,
            ai_result=ai_res,
        )
        self.assertEqual(bundle["clinical_assessment"]["dmed_priority"], "emergency")
        self.assertEqual(len(bundle["observations"]), 6)


if __name__ == "__main__":
    unittest.main()
