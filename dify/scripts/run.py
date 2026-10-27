"""Run PowerContext and companion API, with all mutable paths inside this project."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import subprocess
import socket
import sys
import time
from urllib.request import ProxyHandler, build_opener
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
LOCAL_HTTP = build_opener(ProxyHandler({}))

def require_free_port(port):
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", int(port)))
        except OSError as exc:
            raise RuntimeError("端口" + str(port) + "已被占用，请配置其他端口或仅启动所需组件") from exc

def wait_ready(process, url, label):
    for _ in range(90):
        if process.poll() is not None:
            raise RuntimeError(label + "启动失败，请查看.runtime/logs")
        try:
            with LOCAL_HTTP.open(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(1)
    raise RuntimeError(label + "未在90秒内就绪，请查看.runtime/logs")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--component", choices=["all", "powercontext", "backend"], default="all")
    args = parser.parse_args()
    os.chdir(ROOT)
    env = dict(os.environ)
    env.update({k: v for k, v in dotenv_values(ROOT / ".env").items() if v is not None})
    pc_port = env.get("POWERCONTEXT_SERVER_HTTP_PORT", "18741")
    if args.component in ("all", "powercontext"):
        require_free_port(pc_port)
    if args.component in ("all", "backend"):
        require_free_port(env.get("ECHO_PORT", "18742"))
    runtime = ROOT / ".runtime"
    for path in (runtime, runtime / "logs", ROOT / ".tmp"):
        path.mkdir(parents=True, exist_ok=True)
    env.update(POWERCONTEXT_HOME=str(runtime / "powercontext"), POWERCONTEXT_SERVER_WORKSPACE=str(ROOT),
               POWERCONTEXT_SERVER_HTTP_PORT=pc_port, POWERCONTEXT_SERVER_MCP_ENABLED="false",
               POWERCONTEXT_SERVER_LOGGING_LEVEL="WARNING", POWERCONTEXT_SERVER_LOGGING_FILE="false",
               TEMP=str(ROOT / ".tmp"), TMP=str(ROOT / ".tmp"), TMPDIR=str(ROOT / ".tmp"),
               PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", JIEBA_CACHE_DIR=str(ROOT / ".tmp"))
    # The embedded SQLite backend requires the default role=all. Explicit memory needs no generation model.
    env.pop("POWERCONTEXT_SERVER_RUNTIME_ARTIFACT_PROCESSING_ROLE", None)
    children, handles = [], []
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        if args.component in ("all", "powercontext"):
            log = (runtime / "logs/powercontext.log").open("a", encoding="utf-8")
            handles.append(log)
            process = subprocess.Popen([sys.executable, "-u", "-c", "from powercontext.cli.app import main; main()",
                                        "server", "run", "--no-env-file"], cwd=ROOT, env=env,
                                       stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
            children.append(process)
            if args.component == "all":
                wait_ready(process, "http://127.0.0.1:" + pc_port + "/health/live", "PowerContext")
        if args.component in ("all", "backend"):
            log = (runtime / "logs/backend.log").open("a", encoding="utf-8")
            handles.append(log)
            process = subprocess.Popen([sys.executable, "-u", "-m", "uvicorn", "backend.app:app", "--host",
                                        env.get("ECHO_HOST", "127.0.0.1"), "--port", env.get("ECHO_PORT", "18742"),
                                        "--workers", "1", "--no-access-log"], cwd=ROOT, env=env,
                                       stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
            children.append(process)
            wait_ready(process, "http://127.0.0.1:" + env.get("ECHO_PORT", "18742") + "/health", "配套服务")
        print("回声杂货铺：http://127.0.0.1:" + env.get("ECHO_PORT", "18742") + "  |  按 Ctrl+C 停止。", flush=True)
        while all(p.poll() is None for p in children):
            time.sleep(1)
        raise RuntimeError("服务意外结束，请查看 .runtime/logs")
    except KeyboardInterrupt:
        print("正在停止本项目启动的服务。")
    finally:
        for process in children:
            if process.poll() is None:
                process.terminate()
        for process in children:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for handle in handles:
            handle.close()

if __name__ == "__main__":
    main()
