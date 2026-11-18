"""Exercise live localhost models using synthetic cases. Run while START.cmd is open."""
import argparse
import ctypes
import json
from pathlib import Path
import subprocess
import threading
import time

import httpx

from speech import synthesize, wav_bytes
from triage import MODEL, OLLAMA, Reply


class MemoryStatus(ctypes.Structure):
    _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
        (name, ctypes.c_ulonglong) for name in ("total", "free", "page", "page_free", "virt", "virt_free", "extended")]


def sample_resources(stop, samples):
    while not stop.is_set():
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        samples["machine_ram_used_mib"].append((status.total - status.free) // 2**20)
        try:
            raw = subprocess.check_output(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                                          timeout=3, creationflags=subprocess.CREATE_NO_WINDOW)
            samples["gpu_used_mib"].append(int(raw.splitlines()[0]))
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        stop.wait(0.5)


def run(folder):
    folder.mkdir(parents=True, exist_ok=True)
    base = json.loads(Path(__file__).with_name("example.json").read_text("utf-8"))
    normal = dict(heart_rate_bpm=76, spo2_percent=98, temperature_c=36.6, respiration_rate=16,
                  tremor_detected=False, lung_audio_status="normal")
    cases = [("ibrohim", base, "RED"),
             ("well", dict(base, sensors=normal, voice_text="Shikoyati yo‘q"), "GREEN"),
             ("headache", dict(base, sensors=normal, voice_text="Ikki kundan beri boshim og‘riyapti"), "YELLOW"),
             ("negation", dict(base, sensors=normal, voice_text="Ko‘kragim og‘rimaydi, faqat boshim og‘riyapti"), "YELLOW"),
             ("chest", dict(base, sensors=normal, voice_text="Ko‘kragim qattiq og‘riyapti"), "RED")]
    samples = {"machine_ram_used_mib": [], "gpu_used_mib": []}
    stop = threading.Event()
    monitor = threading.Thread(target=sample_resources, args=(stop, samples), daemon=True)
    monitor.start()
    report = {"model": MODEL, "cases": []}
    try:
        with httpx.Client(base_url="http://127.0.0.1:8001", trust_env=False, timeout=150) as client:
            client.get("/health").raise_for_status()
            source = "Bemorning boshi ikki kundan beri og‘riyapti. Nafasi qisilyapti."
            audio = wav_bytes(synthesize(source))
            t0 = time.perf_counter()
            recognized = client.post("/transcribe", content=audio, headers={"Content-Type": "audio/wav"})
            recognized.raise_for_status()
            text = recognized.json()["voice_text"]
            report["synthetic_speech"] = {"source": source, "transcribed": text,
                                          "stt_seconds": round(time.perf_counter() - t0, 2)}
            cases.append(("voice_visit", dict(base, voice_text=text), "RED"))
            for name, request, zone in cases:
                t0 = time.perf_counter()
                res = client.post("/analyze", params={"speak": name in {"ibrohim", "voice_visit"}}, json=request)
                record = {"name": name, "expected": zone, "http": res.status_code,
                          "elapsed_seconds": round(time.perf_counter() - t0, 2)}
                if res.is_success:
                    result = Reply.model_validate(res.json())
                    record.update(actual=result.risk_zone, assessment=res.headers["X-Assessment-Source"],
                                  voice=res.headers["X-Voice-Status"], playback_start_ms=res.headers.get("X-Playback-Start-Ms"))
                    if name == "ibrohim":
                        (folder / "response.json").write_text(result.model_dump_json(indent=2), "utf-8")
                        (folder / "reply.wav").write_bytes(wav_bytes(synthesize(result.voice_reply_uz)))
                else:
                    record["error"] = res.text
                report["cases"].append(record)
            ps = client.get(OLLAMA + "/api/ps").json()
            report["loaded_models"] = [{k: m.get(k) for k in ("name", "size", "size_vram", "details")} for m in ps["models"]]
    finally:
        stop.set()
        monitor.join(timeout=5)
        report["sampled_machine_peaks_mib"] = {k: max(v, default=None) for k, v in samples.items()}
        (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return all(row.get("actual") == row["expected"] and row.get("assessment") == "ollama-and-sensors"
               for row in report["cases"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("demo-results"))
    args = parser.parse_args()
    raise SystemExit(0 if run(args.output) else 1)
