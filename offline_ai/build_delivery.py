"""Pack a portable, offline Windows x64 release from this prepared development machine."""
import argparse
from pathlib import Path
import hashlib
import json
import os
import sys
import sysconfig
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from prepare_assets import MODELS, ROOT
from triage import MODEL

PROJECT = ROOT.parent
OUTPUT = PROJECT / "dist/Offline-Digital-Bridge-Windows.zip"
APP_FILES = ("START.cmd", "VOICE-DEMO.cmd", "README.md", "example.json", "app.py", "speech.py",
             "triage.py", "voice_visit.py", "launch.py", "prepare_assets.py", "verify_delivery.py",
             "protocols.py", "dhp_sync.py", "patronage_demo.py", "PATRONAJ-DEMO.cmd")


def add_tree(archive, source, destination, skip=()):
    for directory, folders, files in os.walk(source):
        folders[:] = [d for d in folders if d not in {"__pycache__", *skip}]
        for filename in files:
            if filename.endswith((".pyc", ".pyo")) or filename in {"_virtualenv.pth", "_virtualenv.py"}:
                continue
            file = Path(directory) / filename
            archive.write(file, destination / file.relative_to(source))


def main(output):
    output.parent.mkdir(exist_ok=True)
    partial = output.with_suffix(".partial")
    base = Path(sys.base_prefix)
    name, tag = MODEL.split(":")
    manifest = ROOT / "models/manifests/registry.ollama.ai/library" / name / tag
    with ZipFile(partial, "w", compression=ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
        for name in APP_FILES:
            archive.write(PROJECT / name, name)
        add_tree(archive, PROJECT / "patronage", Path("patronage"))
        for file in ("python.exe", "python3.dll", "python312.dll", "vcruntime140.dll",
                     "vcruntime140_1.dll", "LICENSE.txt"):
            archive.write(base / file, f"python/{file}")
        runtime_dlls = ROOT / "lib/ollama/cuda_v12"
        for dll in (*runtime_dlls.glob("msvcp140*.dll"), runtime_dlls / "concrt140.dll"):
            archive.write(dll, "python/" + dll.name)
        add_tree(archive, base / "DLLs", Path("python/DLLs"))
        add_tree(archive, base / "Lib", Path("python/Lib"), skip=("site-packages", "test", "idlelib"))
        add_tree(archive, Path(sysconfig.get_path("purelib")), Path("python/Lib/site-packages"))
        archive.write(ROOT / "ollama.zip", ".runtime/ollama.zip", compress_type=ZIP_STORED)
        for folder, _, _, files in MODELS:
            for name in files:
                asset = ROOT / folder / Path(name).name
                archive.write(asset, asset.relative_to(PROJECT))
        contents = json.loads(manifest.read_bytes())
        archive.write(manifest, manifest.relative_to(PROJECT))
        for layer in [contents["config"], *contents["layers"]]:
            blob = ROOT / "models/blobs" / layer["digest"].replace(":", "-")
            with blob.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != layer["digest"].split(":")[1]:
                    raise ValueError(f"Model fayli buzilgan: {blob.name}")
            archive.write(blob, blob.relative_to(PROJECT), compress_type=ZIP_STORED)
    partial.replace(output)
    print(output, output.stat().st_size, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUTPUT)
    main(parser.parse_args().output)
