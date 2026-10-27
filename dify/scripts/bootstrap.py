"""Install everything within this project; do not change user-wide Python/configuration."""
from __future__ import annotations
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

def main():
    os.chdir(ROOT)
    for name in (".tmp", ".cache/uv", ".runtime"):
        (ROOT / name).mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, UV_CACHE_DIR=str(ROOT / ".cache/uv"), TEMP=str(ROOT / ".tmp"),
               TMP=str(ROOT / ".tmp"), TMPDIR=str(ROOT / ".tmp"), PYTHONDONTWRITEBYTECODE="1",
               PIP_CACHE_DIR=str(ROOT / ".cache/pip"), UV_PYTHON_DOWNLOADS="never",
               SETUPTOOLS_SCM_PRETEND_VERSION_FOR_POWERCONTEXT="0.1.dev1+gd16fb73a3",
               SETUPTOOLS_SCM_PRETEND_VERSION="0.1.dev1+gd16fb73a3")
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("需要 Python 3.11+ 和 uv。先安装 uv，再运行此脚本；项目不会自动修改系统工具。")
    python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        subprocess.run([uv, "venv", "--python", sys.executable, ".venv"], env=env, check=True)
    subprocess.run([uv, "pip", "install", "--python", str(python), "-r", "requirements.txt"], env=env, check=True)
    config = ROOT / ".env"
    if not config.exists():
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        text = text.replace("REPLACE_WITH_A_RANDOM_TOKEN_AT_LEAST_24_CHARS", secrets.token_urlsafe(32))
        text = text.replace("REPLACE_WITH_A_RANDOM_SECRET_AT_LEAST_32_CHARS", secrets.token_urlsafe(48))
        config.write_text(text, encoding="utf-8")
        if os.name != "nt":
            config.chmod(0o600)
    subprocess.run([str(python), "scripts/generate_dsl.py"], env=env, check=True)
    print("安装完成。密钥已在 .env 本机生成；请勿提交 .env。下一步运行 scripts/run.py。")

if __name__ == "__main__":
    main()
