"""Download immutable speech assets during online preparation only."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parent / ".runtime"
MODELS = [
    ("stt", "aslon1213/whisper-small-uz-with-uzbekvoice-ct2",
     "a9074808b2cc21bea065814e40933ae2836b7df6",
     ["model.bin", "tokenizer.json", "config.json", "preprocessor_config.json", "vocabulary.json"]),
    ("tts", "davron112/mms-tts-uzb-cyrl-onnx",
     "6c407c05fa87c2e2783acf684227cec4966c63fd", ["onnx/model.onnx", "vocab.json", "config.json"]),
]


def fetch(job):
    folder, url, name = job
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    if target.is_file():
        return
    partial = target.with_suffix(target.suffix + ".part")
    subprocess.run(["curl.exe", "-fLsS", "--retry", "4", "-o", str(partial), url], check=True)
    partial.replace(target)


if __name__ == "__main__":
    jobs = [(ROOT / folder, f"https://huggingface.co/{repo}/resolve/{rev}/{file}", Path(file).name)
            for folder, repo, rev, files in MODELS for file in files]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(fetch, jobs))
