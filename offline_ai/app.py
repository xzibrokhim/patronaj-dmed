import logging
import os
from contextlib import asynccontextmanager
from threading import Lock
from time import perf_counter

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

import httpx
import sounddevice as sd
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

import speech
from prepare_assets import MODELS, ROOT
from protocols import retrieve
from triage import MODEL, OLLAMA, Finding, Reply, ServiceError, Visit, assess, classify, has_urgent_vitals, reports_chest_pain

@asynccontextmanager
async def lifespan(app):
    speech.recognizer()
    speech.synthesizer()
    for label in ("Qizil zona", "Sariq zona", "Yashil zona"):
        speech.synthesize(label, instruction=True)
    print("Tayyor.  POST http://localhost:8001/analyze  |  Ctrl+C", flush=True)
    yield


app = FastAPI(title="Offline Digital Bridge", docs_url=None, redoc_url=None, lifespan=lifespan)
visit_lock = Lock()


@app.exception_handler(ServiceError)
async def handle_error(request, exc):
    return JSONResponse(status_code=exc.status, content={"detail": exc.message})


@app.get("/health")
def health(response: Response):
    missing = [f"{folder}/{file.split('/')[-1]}" for folder, _, _, files in MODELS for file in files
               if not (ROOT / folder / file.split("/")[-1]).is_file()]
    try:
        with httpx.Client(timeout=3, trust_env=False) as client:
            tags = client.get(OLLAMA + "/api/tags").raise_for_status().json()["models"]
        if MODEL not in [item["name"] for item in tags]:
            missing.append(MODEL)
    except (httpx.HTTPError, ValueError, KeyError):
        missing.append("Ollama")
    response.status_code = 503 if missing else 200
    return {"ready": not missing, "missing": missing}


@app.post("/transcribe")
async def transcribe(request: Request):
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > 1_000_000:
            raise ServiceError(413, "Audio 30 soniyadan uzun.")
        data.extend(chunk)
    return {"voice_text": await run_in_threadpool(speech.transcribe, bytes(data))}


@app.post("/analyze", response_model=Reply)
def analyze(visit: Visit, response: Response, speak: bool = True):
    if not visit_lock.acquire(blocking=False):
        raise ServiceError(409, "Oldingi tashrif davom etmoqda. Tugagach qayta yuboring.")
    try:
        start = perf_counter()
        try:
            finding = classify(visit)
            response.headers["X-Assessment-Source"] = "ollama-and-sensors"
        except ServiceError:
            if has_urgent_vitals(visit.sensors):
                response.headers["X-Assessment-Source"] = "urgent-sensor-fallback"
            elif reports_chest_pain(visit.voice_text):
                response.headers["X-Assessment-Source"] = "urgent-symptom-fallback"
            else:
                raise
            finding = Finding(kind="unclear", quote=visit.voice_text)
        result = assess(visit, finding)
        urls = dict.fromkeys(passage.url for passage in retrieve(visit))
        if urls:
            response.headers["Link"] = ", ".join(f'<{url}>; rel="cite-as"' for url in urls)
        response.headers["X-Voice-Status"] = "disabled"
        if speak:
            try:
                samples = speech.synthesize(result.voice_reply_uz, instruction=True)
                sd.play(samples, speech.RATE)
                response.headers["X-Playback-Start-Ms"] = str(round((perf_counter() - start) * 1000))
                sd.wait()
                response.headers["X-Voice-Status"] = "played"
            except Exception:
                logging.getLogger(__name__).warning("Local audio output unavailable")
                response.headers["X-Voice-Status"] = "unavailable"
        return result
    finally:
        visit_lock.release()
