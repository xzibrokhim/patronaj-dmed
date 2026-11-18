# AI-Patronaj DMED — Offline Hardware + AI Medical Complex (Tashkent Hackathon, Task #10)

> **Official Track:** Sogʻliqni saqlash vazirligi va farmatsevtika sanoati — **Задача №10: Patronaj plansheti tizimi (Digital Bridge for Rural Areas)**  
> **Key Differentiator:** 3D-printed multi-sensor medical hardware (`ESP32-S3`) + **100% Offline Local Voice AI** + **Local SQLite Queue** + **1-Click Sync to DMED & Doctor Telegram Bot**.

---

## Architecture & Data Flow

```text
[1. Hardware Probes (ESP32-S3-N16R8)]  -- USB Serial / Wi-Fi UDP -->  [2. Backend & DMED Bridge (Python)]
   • Finger clip: MAX30102 (HR, SpO2)                                      • SQLite offline queue
   • Body probe: DS18B20 (Temp °C)                                         • DMED FHIR/JSON payload builder
   • Chest/hand strap: MPU6050 (RR, Tremor)                                • Telegram Bot & DMED uploader
   • Stethoscope mic -> Laptop Audio / Web Audio                           • Sends GREEN/YELLOW/RED back to ESP32
   • Onboard OLED + GPIO48 RGB Triage LED + Buzzer
                                                                                  |
                                                                      http://localhost:8001/analyze
                                                                                  v
[4. Web Dashboard + Doctor TG Bot]  <--- REST API (:8000) --->  [3. Offline Voice AI (GPU/Laptop)]
   • Nurse Tablet UI (OFFLINE / SYNC)                               • Faster-Whisper (UZ/RU STT offline)
   • Live sensor charts & audio wave                                • Ollama (Qwen2.5-7B / Llama3.1-8B)
   • Doctor TG Bot (DMED card + buttons)                            • Local TTS voice instructions
```

---

## Team Roles & Folders

| Member | Role | Folder | Responsibility |
|---|---|---|---|
| **Ibrokhim Anvarov** | Team Lead, Hardware, Backend & DMED Bridge | [`hardware/`](hardware/ai_patronaj_node/ai_patronaj_node.ino), [`backend/`](backend/live_server.py) | `ESP32-S3` sensor probes & firmware, `GPIO 48` triage LED/buzzer feedback, `live_server.py`, SQLite offline queue, DMED FHIR/JSON sync adapter, final integration |
| **Fayzullo (`@Fayzullo8758`)** | Offline AI Engineer | [`ai_engine/`](ai_engine/README.md) | Local voice pipeline (`Faster-Whisper` + `Ollama` + local TTS) running 100% offline on GPU, returning structured triage JSON |
| **Abdulbosit (`@Abdulbosit1215`)** | Frontend & Telegram Bot Developer | [`frontend/`](frontend/README.md) | Nurse Tablet Web Dashboard (`OFFLINE / ONLINE SYNC` toggle, live charts, DMED status) + District Doctor Telegram Bot |
| **Anvarjon (`@Anva_official1`)** | 3D CAD, Pitch & Media | — | SolidWorks 3D-printed medical enclosure + finger clip, competitor scouting, 8-slide deck, demo video |

---

## Unified API Contract (Backend on `http://localhost:8000`)

### 1. Live Sensor Telemetry (`GET /api/sensors/live`)
Returns latest readings from `ESP32-S3` (USB Serial `115200` baud or Wi-Fi UDP `41730`):
```json
{
  "connected": true,
  "transport": "usb",
  "port": "COM51",
  "updatedAt": "2026-11-18T10:15:30Z",
  "sensors": {
    "heart_rate_bpm": 118,
    "spo2_percent": 91,
    "temperature_c": 38.4,
    "respiration_rate": 25,
    "tremor_detected": false,
    "tremor_hz": 0.4,
    "air_co2_ppm": 460,
    "fall_detected": false,
    "finger_on_sensor": true,
    "button_start": false
  }
}
```

### 2. AI Triage Analysis (`POST /api/ai/analyze`)
Frontend sends patient info + voice/text complaint; Backend automatically merges live sensor data, queries Fayzullo's AI (`http://localhost:8001/analyze`, with built-in deterministic fallback if AI service is offline), and pushes `RED` / `YELLOW` / `GREEN` to the ESP32-S3 hardware LED & buzzer.

**Request:**
```json
{
  "patient_name": "Karimov Alisher",
  "patient_age": 62,
  "mahalla": "Oqdaryo MFY",
  "voice_text": "Bemor ko'krak qafasida og'riq va nafas qisishidan shikoyat qilyapti",
  "lung_audio_status": "wheezing_detected"
}
```

**Response:**
```json
{
  "risk_zone": "RED",
  "risk_score": 88,
  "preliminary_diagnosis_uz": "O'tkir nafas yetishmovchiligi va pnevmoniya ehtimoli (SpO2 past — 91%, taxikardiya — 118 zarb/daq)",
  "nurse_instructions_uz": [
    "Bemorni yarim o'tirgan holatga keltiring va toza havo kirishini ta'minlang",
    "Shoshilinch ravishda tuman shifokoriga sinxronizatsiya orqali Qizil chaqiruv yuboring"
  ],
  "voice_reply_uz": "Diqqat, bemor qizil xavf zonasida! Saturatsiya 91 foizga tushgan.",
  "sensors_used": {
    "heart_rate_bpm": 118,
    "spo2_percent": 91,
    "temperature_c": 38.4,
    "respiration_rate": 25,
    "tremor_detected": false,
    "lung_audio_status": "wheezing_detected"
  },
  "ai_source": "local_ollama"
}
```

### 3. Save Patient Encounter Offline (`POST /api/patients/save_offline`)
Saves patient examination + vitals + AI triage + pre-built **DMED Encounter Payload** into local SQLite (`offline_patronaj.db`) with status `pending_sync`.

### 4. Sync Queue to DMED & Doctor Telegram Bot (`POST /api/dmed/sync`)
Reads all `pending_sync` records from SQLite, emits structured **DMED РУз (`FHIR R4 / DMED JSON`)** encounter bundles, notifies the District Doctor Telegram Bot, and marks rows `synced`.

### 5. Manual Hardware Feedback (`POST /api/hardware/zone`)
Sends `{"zone": "RED" | "YELLOW" | "GREEN", "beep": true}` to `ESP32-S3` (`GPIO 48` RGB LED + Buzzer + OLED).

---

## Quick Start (Backend)

```bash
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
python live_server.py --port COM51 --http-port 8000
```
Run automated tests:
```bash
python -m unittest backend/test_live_server.py -v
```
