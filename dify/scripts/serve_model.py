"""Download a checksum-pinned open model and CPU runtime into the project and serve it."""
from __future__ import annotations
import argparse
import hashlib
import os
from pathlib import Path
import platform
import secrets
import subprocess
import tarfile
from urllib.request import urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1]
REVISION = "91cad51170dc346986eccefdc2dd33a9da36ead9"
MODEL_NAME = "qwen2.5-1.5b-instruct-q4_k_m.gguf"
MODEL_SHA = "6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e"
RELEASE = "b11929"
RUNTIMES = {
    "Linux": ("llama-b11929-bin-ubuntu-x64.tar.gz", "85b83ffa80831002ec0ed1c4cb1494fbcd75c0a0e77003cfe7eab32c21b5fa15"),
    "Windows": ("llama-b11929-bin-win-cpu-x64.zip", "39d7adde2cdf758a1af7f9f150a71d16dc0392acd1890c3e9ea31549ebb901b1"),
}

def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

def download(url, path, checksum):
    if path.exists() and sha256(path) == checksum:
        return
    temporary = path.with_suffix(path.suffix + ".part")
    with urlopen(url, timeout=90) as response, temporary.open("wb") as output:
        for block in iter(lambda: response.read(1024 * 1024), b""):
            output.write(block)
    if sha256(temporary) != checksum:
        raise RuntimeError("Download checksum mismatch; the unverified file will not be executed.")
    temporary.replace(path)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18744)
    args = parser.parse_args()
    system = platform.system()
    if system not in RUNTIMES or platform.machine().lower() not in {"x86_64", "amd64"}:
        raise SystemExit("Bundled download instructions support x86-64 Linux/Windows; use another compatible model service on other platforms.")
    folder = ROOT / ".runtime/model"
    runtime = folder / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    filename, checksum = RUNTIMES[system]
    archive = folder / filename
    download(f"https://github.com/ggml-org/llama.cpp/releases/download/{RELEASE}/{filename}", archive, checksum)
    executable = "llama-server.exe" if system == "Windows" else "llama-server"
    candidates = list(runtime.rglob(executable))
    if not candidates:
        if system == "Windows":
            with zipfile.ZipFile(archive) as source:
                for member in source.namelist():
                    if not (runtime / member).resolve().is_relative_to(runtime.resolve()):
                        raise RuntimeError("Unsafe runtime archive path")
                source.extractall(runtime)
        else:
            with tarfile.open(archive) as source:
                source.extractall(runtime, filter="data")
        candidates = list(runtime.rglob(executable))
    if len(candidates) != 1:
        raise RuntimeError("Runtime executable could not be identified unambiguously")
    model = folder / MODEL_NAME
    print("Preparing the official Qwen model (about 1.1 GB); files stay in .runtime/model.", flush=True)
    download(f"https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/{REVISION}/{MODEL_NAME}", model, MODEL_SHA)
    key = folder / "api.key"
    if not key.exists():
        key.write_text(secrets.token_urlsafe(32) + "\n", encoding="utf-8")
        if system != "Windows":
            key.chmod(0o600)
    print("MODEL_NAME=echo-qwen; configure the model URL for your network and copy .runtime/model/api.key into Dify's MODEL_API_KEY secret.", flush=True)
    argv = [str(candidates[0]), "-m", str(model), "--host", args.host, "--port", str(args.port),
            "--threads", str(min(6, os.cpu_count() or 2)), "--ctx-size", "4096", "--parallel", "1",
            "--alias", "echo-qwen", "--api-key-file", str(key), "--no-webui"]
    process = subprocess.Popen(argv, cwd=ROOT, creationflags=subprocess.CREATE_NO_WINDOW if system == "Windows" else 0)
    try:
        process.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=15)

if __name__ == "__main__":
    main()
