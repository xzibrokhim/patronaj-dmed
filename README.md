# AI-Patronaj DMED — Hardware ECG/SpO2 Bio-Node + Web Dashboard (Task #10)

> **Official Track:** Sogʻliqni saqlash vazirligi va farmatsevtika sanoati — **Задача №10: Patronaj plansheti tizimi (Digital Bridge for Rural Areas)**  
> **Focused MVP Hardware:** `ESP32-S3-N16R8` + `AD8232` (ECG) + `GY-MAX30102` (HR & SpO2) + `DS18B20` (Body Temp Probe) + `MQ-135` (Breath/Air Gas) + `OLED 0.96"/1.3"` + Onboard `GPIO 48` Triage RGB LED.

---

## Hardware Pinout (`ESP32-S3-N16R8`)

| Sensor / Module | Module Pin | ESP32-S3 Pin | Notes |
|---|---|---|---|
| **AD8232 (ECG)** | `3.3V` / `GND` / `OUTPUT` / `LO-` / `LO+` | `3V3` / `GND` / `GPIO 2` / `GPIO 6` / `GPIO 7` | 50 Hz ECG waveform (`ecg_wave`) |
| **GY-MAX30102 (HR/SpO2)** | `VCC` / `GND` / `SDA` / `SCL` | `3V3` / `GND` / `GPIO 8` / `GPIO 9` | I2C (`0x57`) |
| **DS18B20 (Steel Probe)** | Red (`VCC`) / Black (`GND`) / Yellow (`DATA`) | `3V3` / `GND` / `GPIO 5` | 4.7k pull-up resistor between `GPIO 5` and `3V3` recommended |
| **MQ-135 (Gas/Breath)** | `VCC` / `GND` / `AO` | `5Vin` / `GND` / `GPIO 1` | ADC1 analog output |
| **OLED 0.96"/1.3" (I2C)** | `VCC` / `GND` / `SDA` / `SCL` | `3V3` / `GND` / `GPIO 8` / `GPIO 9` | Shared I2C (`0x3C`) |
| **Onboard RGB LED** | Built-in WS2812 | `GPIO 48` | Lights Green / Yellow / Red on risk zone |
| **Buzzer / Button (opt.)** | `+` / `BTN` | `GPIO 10` / `GPIO 11` | Pulse beep + RED alarm |

---

## Unified API Contract (Backend on `http://127.0.0.1:8000`)

### 1. Live Sensor Telemetry (`GET /api/sensors/live`)
Returns latest readings from `ESP32-S3` (USB Serial `115200` baud or Wi-Fi UDP `41730`):
```json
{
  "connected": true,
  "transport": "usb",
  "port": "COM51",
  "updatedAt": "2026-11-18T12:15:30Z",
  "sensors": {
    "heart_rate_bpm": 78,
    "spo2_percent": 98,
    "temperature_c": 36.6,
    "temp_ok": true,
    "ecg_mv": 1640,
    "ecg_leads_ok": true,
    "ecg_wave": [2040, 2055, 2240, 2890, 1910, 2045, 2060, 2050, 2048, 2052, 2260, 2910, 1900, 2046, 2058, 2051],
    "air_co2_ppm": 445,
    "mq_raw": 510,
    "finger_on_sensor": true,
    "button_start": false,
    "zone": "GREEN"
  }
}
```

### 2. AI Triage Analysis (`POST /api/ai/analyze`)
Merges live sensor readings + text/voice complaint, returns triage zone (`GREEN` / `YELLOW` / `RED`), and pushes `ZONE:{zone}` back to `ESP32-S3` (`GPIO 48` RGB LED + Buzzer).

### 3. Save Offline (`POST /api/patients/save_offline`) & List (`GET /api/patients`)
Saves rural patient exam locally to `SQLite` (`offline_patronaj.db`) with `pending_sync` status and pre-built `DMED-UZ-MINZDRAV` FHIR/JSON payload.

### 4. Sync to DMED & Doctor Telegram Bot (`POST /api/dmed/sync`)
Synchronizes all `pending_sync` records to the DMED adapter and Doctor Telegram Bot, marking records `synced`.

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
cd backend
python -m unittest test_live_server.py -v
```
