# Digital Bridge for Rural Areas — Task #10

Offline-first patronage nurse tablet web dashboard (`web/`) and district doctor Telegram console (`telegram_bot/`), designed with **Swiss Clinical Precision** UI standards.

> For information only; not a substitute for professional medical advice. This is not a medical device. A patronage nurse records objective observations and triage signals; final medical diagnosis is made strictly by a licensed physician.

---

## 📁 Folder Structure (Separated Edition)

```text
digital-bridge-task-10/
├── README.md                ← You are here (Setup + AI Handoff Guide)
├── web/                     ← Standalone Website + FastAPI Backend (Port 8000)
│   ├── start-web.bat        ← Double-click on Windows to start website & API
│   ├── run.ps1              ← Creates .venv, installs deps, sets .env, starts Uvicorn
│   ├── requirements.txt
│   ├── .env.example
│   └── app/
│       ├── main.py          ← FastAPI API, SQLite, security headers, Telegram push
│       └── static/          ← Daylight Clinical UI (HTML, CSS, JS, IndexedDB, SVG)
└── telegram_bot/            ← Standalone District Doctor Telegram Bot (aiogram 3.x)
    ├── start-bot.bat        ← Double-click on Windows to start Telegram bot
    ├── start-bot.ps1        ← Creates .venv (if needed), installs deps, starts bot
    ├── requirements.txt
    ├── .env.example
    └── app/
        ├── telegram_bot.py  ← District doctor aiogram bot + callback actions
        └── main.py          ← Shared config/DB helpers
```

---

## 🚀 Quick Start After Unzipping (Zero Setup Hassle)

### 1. Start the Website (`web/`)
1. Extract the ZIP archive completely (**Right-click → Extract All...**).
2. Open the **`web`** folder and double-click **`start-web.bat`** *(or in PowerShell: `Set-ExecutionPolicy -Scope Process Bypass; .\run.ps1`)*.
3. Open **`http://127.0.0.1:8000`** in your browser.
4. On the **Nurse Login screen**, choose a preset nurse profile (e.g. `Salimova Nilufar · PN-204`) and enter PIN **`2026`**.

Leave the **Server** switch off to run the demo without Telegram. Add sample data from the queue when needed.

### 2. Start the Telegram Bot (`telegram_bot/`)
1. Create a bot token in `@BotFather` on Telegram.
2. Open **`web/.env`** (created automatically when `start-web.bat` runs) or copy `telegram_bot/.env.example` to `telegram_bot/.env` and set:
   ```dotenv
   TELEGRAM_BOT_TOKEN=123456:ABC-your-bot-token
   DOCTOR_USER_ID=
   BACKEND_URL=http://127.0.0.1:8000
   INTERNAL_API_KEY=<keep auto-generated key identical in web/.env and telegram_bot/.env>
   ```
3. Open the **`telegram_bot`** folder and double-click **`start-bot.bat`** *(or in PowerShell: `.\start-bot.ps1`)*.
4. Click the one-time setup link printed in the console (`https://t.me/<your_bot>?start=...`) and press **Start** in Telegram.
5. Turn **Server** on, switch to **Onlayn**, and use **Yozuvlarni yuborish** if records remain in the queue. When Telegram is configured, the doctor receives a patient card.

---

## 🤖 AI HANDOFF GUIDE (FOR THE NEXT AI ASSISTANT)

> **Instructions for the AI reading this repository on the backend developer's laptop:**
> Below is the exact architecture, frontend state behavior, and REST API contract. If you are replacing `web/app/main.py` with another backend (e.g., Django/FastAPI/Node), implement these exact endpoints and JSON schemas so `web/app/static/js/main.js` and `telegram_bot/app/telegram_bot.py` work without modification.

### 1. How the Frontend Works (`web/app/static/`)
- **Strict CSP**: `app/main.py` enforces `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:;`. Never add inline `style="..."` attributes or external CDN URLs to `index.html`.
- **Nurse Authentication (`#loginOverlay`)**:
  - Client-side shift lock stored in `sessionStorage` (`db_session_active = "1"`) and nurse profile persisted in IndexedDB `settings` (`key: "nurse-profile"` -> `{name, id, facility}`).
  - Preset buttons populate `#loginNurseName`, `#loginNurseId`, `#loginNurseFacility`, `#loginNursePin` (4–8 digits, default `2026`).
- **Offline Storage (`web/app/static/js/storage.js`)**:
  - Uses browser **IndexedDB** (`digital-bridge-prototype`, version `2`) with 3 stores:
    - `patients` (`keyPath: "local_id"`) — unsynced (`status: "pending"`) and synced (`status: "synced"`) records.
    - `replies` (`keyPath: "id"`) — doctor replies fetched from backend.
    - `settings` (`keyPath: "key"`) — `language` (`uz`/`ru`), `api-enabled` (`boolean`), `sensor-scenario` (`normal`/`review`/`urgent`), `device-id`, `nurse-profile`.
- **Top Command Bar Switches**:
  - **Server (`#apiEnabled`) off**: Simulated telemetry and demo sync. Records remain available for server sync later.
  - **Server (`#apiEnabled`) on + Onlayn (`#modeToggle`)**: Connects to the backend using the endpoints below.
- **Nurse Scope Rule**: Field `nurse_diagnosis` in payloads is labeled **"Hamshira kuzatuvi va ko‘rik qaydi"** / **"Наблюдения медсестры и данные осмотра"** because nurses record objective observations while only a doctor establishes a diagnosis.

### 2. Required Backend Endpoints & Exact JSON Contracts

#### `GET /api/health`
Health check used for diagnostics.
```json
{
  "ok": true,
  "service": "digital-bridge",
  "telegram_configured": true,
  "demo_mode": true
}
```

#### `GET /api/sensors/live`
Polled every **1000 ms** when `#modeToggle` is ONLINE and `#apiEnabled` is checked.
```json
{
  "pulse": 118,
  "spo2": 91,
  "temperature": 38.4,
  "respiration": 25,
  "tremor": 0.72,
  "source": "simulated-backend-sensor",
  "timestamp": "2026-11-18T12:00:00+00:00"
}
```

#### `POST /api/patients/sync`
Called when syncing pending records (with exponential retry backoff). Must be **idempotent on `local_id`**.
- **Request Body**:
```json
{
  "patients": [
    {
      "local_id": "demo-queue-001-1763460000",
      "device_id": "tablet-abc123",
      "patient": {
        "name": "Tursunov Abdulla",
        "age": 64,
        "village": "Bo'stonliq MFY"
      },
      "vitals": {
        "pulse": 118,
        "spo2": 91,
        "temperature": 38.4,
        "respiration": 25,
        "tremor": 0.72
      },
      "complaint": "Kuchli quruq yo'tal, nafas qisishi",
      "nurse_diagnosis": "Bemor yarim o'tirgan holatga keltirildi, nafas tezligi yuqori",
      "ai_assessment": {
        "level": "demo_high",
        "score": 90,
        "summary": "Ichki test qoidasi tezkor e'tibor signalini berdi."
      },
      "consent": {
        "granted": true,
        "version": "prototype-notice-v1",
        "captured_at": "2026-11-18T12:00:00.000Z"
      },
      "captured_at": "2026-11-18T12:00:00.000Z"
    }
  ]
}
```
*(Valid `ai_assessment.level` values: `"demo_low"`, `"demo_review"`, `"demo_high"`, `"unvalidated"`.)*
- **Response Body**:
```json
{
  "ok": true,
  "acknowledgements": [
    {
      "local_id": "demo-queue-001-1763460000",
      "server_id": 1,
      "status": "created"
    }
  ],
  "telegram": "queued"
}
```

#### `GET /api/doctor-replies?device_id=<device_id>&after_id=<last_reply_id>`
Polled every **5000 ms** when ONLINE + `#apiEnabled` is checked.
```json
{
  "replies": [
    {
      "id": 1,
      "patient_id": 1,
      "action": "confirmed",
      "message": "Shifokor ma’lumotni ko‘rdi va qabul qildi.",
      "created_at": "2026-11-18T12:01:00+00:00"
    }
  ]
}
```

#### `POST /api/internal/doctor-actions`
Called by `telegram_bot` when the doctor taps an inline button (`confirm:<id>`, `ambulance:<id>`, `template:<code>:<id>`). Requires header `X-Internal-Key: <INTERNAL_API_KEY>`.
```json
{
  "patient_id": 1,
  "action": "confirmed",
  "message": "Shifokor ma’lumotni ko‘rdi va qabul qildi."
}
```
