"""Prepare a private project-local Dify 1.9.1 checkout. Start only with --start."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", action="store_true", help="Run Docker Compose after preparation")
    args = parser.parse_args()
    destination = ROOT / ".runtime/dify-runtime"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        subprocess.run(["git", "-c", "credential.helper=", "clone", "--depth", "1", "--branch", "1.9.1",
                        "https://github.com/langgenius/dify.git", str(destination)], cwd=ROOT, check=True)
    tag = subprocess.check_output(["git", "describe", "--tags", "--exact-match", "HEAD"], cwd=destination, text=True).strip()
    if tag != "1.9.1":
        raise RuntimeError("已有目录版本不是1.9.1，停止以免修改其他安装")
    docker = destination / "docker"
    config = docker / ".env"
    if not config.exists():
        text = (docker / ".env.example").read_text(encoding="utf-8")
        text = text.replace("EXPOSE_NGINX_PORT=80\n", "EXPOSE_NGINX_PORT=18080\n")
        text = text.replace("EXPOSE_NGINX_SSL_PORT=443\n", "EXPOSE_NGINX_SSL_PORT=18443\n")
        config.write_text(text, encoding="utf-8")
    override = docker / "echo.override.yaml"
    shutil.copyfile(ROOT / "deploy/dify-compose.override.yaml", override)
    cmd = ["docker", "compose", "--project-name", "echo-shop-dify", "-f", "docker-compose.yaml",
           "-f", "echo.override.yaml", "up", "-d"]
    print("Dify配置位于：" + str(docker))
    print("执行目录：" + str(docker))
    print(" ".join(cmd))
    print("启动后访问 http://localhost:18080/install，导入 dsl/echo_shop.workflow.yml。")
    if args.start:
        subprocess.run(cmd, cwd=docker, check=True)

if __name__ == "__main__":
    main()
