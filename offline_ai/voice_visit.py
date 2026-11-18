"""Record on demand. The nurse must confirm the transcript before analysis."""
import argparse
import io
import json
from pathlib import Path
import wave

import httpx
import numpy as np
import sounddevice as sd


def record():
    blocks = []
    def collect(data, frames, timing, status):
        blocks.append(data.copy())
        if sum(len(block) for block in blocks) >= 480000:
            raise sd.CallbackStop

    input("Yozishni boshlash uchun Enter bosing. ")
    with sd.InputStream(samplerate=16000, channels=1, dtype="int16", callback=collect):
        input("Shikoyatni ayting (30 soniyagacha). Tugatish uchun Enter bosing. ")
    if not blocks:
        raise ValueError("Mikrofondan ovoz kelmadi.")
    out = io.BytesIO()
    with wave.open(out, "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", ""))
        audio.writeframes(np.concatenate(blocks)[:480000].tobytes())
    return out.getvalue()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("visit", type=Path, nargs="?", help="Bemor yoshi va datchiklar yozilgan JSON")
    parser.add_argument("--wav", type=Path, help="Mikrofon o‘rniga 16 kHz PCM16 mono WAV")
    args = parser.parse_args()
    if args.visit is None:
        print("DEMO: yosh va barcha datchiklar example.json dagi sun’iy bemordan olinadi.")
    visit = json.loads((args.visit or Path(__file__).with_name("example.json")).read_text("utf-8-sig"))
    print("Yosh:", visit["patient_age"], " Datchiklar:", visit["sensors"])
    with httpx.Client(base_url="http://127.0.0.1:8001", trust_env=False, timeout=120) as client:
        audio = args.wav.read_bytes() if args.wav else record()
        response = client.post("/transcribe", content=audio, headers={"Content-Type": "audio/wav"})
        response.raise_for_status()
        text = response.json()["voice_text"]
        print("Tanilgan matn:", text)
        edit = input("Sonlar, inkor va alomatlarni tekshiring. Tasdiqlash uchun +, bekor qilish uchun Enter yoki tuzatilgan matn: ").strip()
        if not edit:
            return
        visit["voice_text"] = text if edit == "+" else edit
        response = client.post("/analyze", json=visit)
        response.raise_for_status()
        print(json.dumps(response.json(), ensure_ascii=False, indent=2))
        print("Karnay:", response.headers.get("X-Voice-Status"))


if __name__ == "__main__":
    try:
        main()
    except httpx.HTTPStatusError as exc:
        raise SystemExit(exc.response.text)
    except (OSError, ValueError, httpx.TransportError, sd.PortAudioError) as exc:
        raise SystemExit(str(exc))
