"""Start the bundled local inference engine and single-worker API on Windows."""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from zipfile import ZipFile

import httpx
import uvicorn

from prepare_assets import MODELS, ROOT
from triage import MODEL, OLLAMA


def main():
    if not (ROOT / "engine.ready").exists() and (ROOT / "ollama.zip").exists():
        print("Birinchi ishga tushirish: Ollama fayllari ochilmoqda...", flush=True)
        with ZipFile(ROOT / "ollama.zip") as bundle:
            bundle.extractall(ROOT)
        (ROOT / "engine.ready").touch()
    required = [ROOT / "ollama.exe", ROOT / "lib/ollama",
                *(ROOT / folder / Path(f).name for folder, _, _, files in MODELS for f in files)]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise RuntimeError("Yetishmagan fayllar: " + ", ".join(missing))
    for port in (8001, 11439):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError(f"{port} porti band. Avval ochilgan serverni tekshiring.")
    env = dict(os.environ, OLLAMA_MODELS=str(ROOT / "models"), OLLAMA_HOST="127.0.0.1:11439",
               OLLAMA_NO_CLOUD="1", OLLAMA_NUM_PARALLEL="1", OLLAMA_MAX_LOADED_MODELS="1")
    with (ROOT / "ollama-service.log").open("w", encoding="utf-8") as log:
        engine = subprocess.Popen([str(ROOT / "ollama.exe"), "serve"], env=env, stdout=log, stderr=log,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            with httpx.Client(trust_env=False, timeout=3) as client:
                for _ in range(60):
                    if engine.poll() is not None:
                        raise RuntimeError("Ollama ishga tushmadi. .runtime/ollama-service.log ni ko‘ring.")
                    try:
                        tags = client.get(OLLAMA + "/api/tags").raise_for_status().json()
                        break
                    except httpx.TransportError:
                        time.sleep(0.5)
                else:
                    raise RuntimeError("Ollama ishga tushishi uchun vaqt tugadi.")
                if MODEL not in [m["name"] for m in tags["models"]]:
                    raise RuntimeError(f"{MODEL} topilmadi. Paketni to‘liq chiqaring.")
                print("Lokal model xotiraga yuklanmoqda...", flush=True)
                client.post(OLLAMA + "/api/generate", timeout=180,
                            json={"model": MODEL, "stream": False, "keep_alive": "30m",
                                  "options": {"num_ctx": 2048}}).raise_for_status()
            print("Ovoz modellari yuklanmoqda. Tekshirish: http://localhost:8001/health", flush=True)
            uvicorn.run("app:app", host="127.0.0.1", port=8001, access_log=False)
        finally:
            engine.terminate()
            engine.wait(timeout=15)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, httpx.HTTPError) as exc:
        sys.exit(str(exc))
