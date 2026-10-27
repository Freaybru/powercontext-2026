"""Real PowerContext/API acceptance, including process restarts. No live model or Dify engine."""
from __future__ import annotations
import datetime
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

import httpx

from check_dsl import check, load_dsl, run_code

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / ".runtime/evidence"
EVIDENCE.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))
from backend.game import rule_narration

PC_PORT, API_PORT = 18751, 18752
API = f"http://127.0.0.1:{API_PORT}"
run_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
runtime = ROOT / ".runtime/acceptance" / run_id
runtime.mkdir(parents=True)
env = dict(os.environ, POWERCONTEXT_HOME=str(runtime / "powercontext"), POWERCONTEXT_SERVER_WORKSPACE=str(ROOT),
           POWERCONTEXT_SERVER_HTTP_PORT=str(PC_PORT), POWERCONTEXT_SERVER_MCP_ENABLED="false",
           POWERCONTEXT_SERVER_LOGGING_LEVEL="WARNING", POWERCONTEXT_SERVER_LOGGING_FILE="false",
           POWERCONTEXT_URL=f"http://127.0.0.1:{PC_PORT}", POWERCONTEXT_TOKEN="",
           ECHO_API_TOKEN=secrets.token_urlsafe(32), ECHO_SIGNING_SECRET=secrets.token_urlsafe(48),
           DIFY_API_URL="", DIFY_APP_API_KEY="", PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1",
           TEMP=str(runtime), TMP=str(runtime), TMPDIR=str(runtime))
env.pop("POWERCONTEXT_SERVER_RUNTIME_ARTIFACT_PROCESSING_ROLE", None)
headers = {"Authorization": "Bearer " + env["ECHO_API_TOKEN"]}
client = httpx.Client(timeout=30, trust_env=False)
children, handles = [], []
results = []

def spawn(name, argv):
    log = (runtime / (name + ".log")).open("a", encoding="utf-8")
    handles.append(log)
    process = subprocess.Popen([sys.executable, "-u", *argv], cwd=ROOT, env=env, stdout=log,
                               stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    children.append(process)
    return process

def wait_ready(process, url):
    for _ in range(120):
        if process.poll() is not None:
            raise RuntimeError("子进程退出，检查本次 .runtime/acceptance 中日志")
        try:
            if client.get(url, timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError("启动超时")

def stop(process):
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=15)

def assert_unavailable(url):
    try:
        client.get(url, timeout=1)
    except httpx.HTTPError:
        return
    raise AssertionError("测试端口仍有服务响应，不能声称完成了进程重启")

def request(path, body, expected=200, auth=True):
    response = client.post(API + path, json=body, headers=headers if auth else {})
    if response.status_code != expected:
        raise AssertionError(f"{path}: expected {expected}, got {response.status_code}: {response.text[:400]}")
    return response.json()

def prepared(player, action, request_id=None):
    body = {"player_code": player, "action": action}
    if request_id:
        body["request_id"] = request_id
    return request("/api/prepare", body)

def finish(data):
    detail = json.loads(data["game_context"])
    return request("/api/commit", {"ticket": data["ticket"], "narration": rule_narration(detail)[:600]})

def record(name, details=None):
    results.append({"check": name, "status": "passed", "details": details})
    print("PASS: " + name, flush=True)

def main():
    check()
    assert_unavailable(API + "/health")
    assert_unavailable(f"http://127.0.0.1:{PC_PORT}/health/live")
    pc = spawn("powercontext", ["-c", "from powercontext.cli.app import main; main()", "server", "run", "--no-env-file"])
    wait_ready(pc, f"http://127.0.0.1:{PC_PORT}/health/live")
    backend = spawn("backend", ["-m", "uvicorn", "backend.app:app", "--port", str(API_PORT), "--no-access-log"])
    wait_ready(backend, API + "/health")
    record("真实PowerContext服务与后端就绪")
    request("/api/prepare", {"player_code": "sample_001", "action": "开场"}, 401, auth=False)
    request("/api/prepare", {"player_code": "x", "action": "开场"}, 422)
    record("鉴权与输入校验")
    player = "acceptance_001"
    deposit = prepared(player, "寄存：奶奶在蓝铁盒里藏了一颗橘子糖", "deposit-unique-001")
    saved = finish(deposit)
    assert saved["artifact"]["revision"] == 1
    replay = finish(deposit)
    assert replay["replayed"] and replay["artifact"] == saved["artifact"]
    record("真实记忆创建与重复提交幂等", saved["artifact"])
    unfinished = prepared(player, "1")
    visit = prepared(player, "回访")
    assert json.loads(visit["game_context"])["state"]["turn"] == 0
    assert "蓝铁盒" in visit["memory_read"]["stored_memory"]
    request("/api/commit", {"ticket": unfinished["ticket"] + "x", "narration": "篡改"}, 400)
    record("模型阶段未完成时不推进状态，拒绝篡改票据")
    competing = prepared(player, "2")
    first = finish(unfinished)
    request("/api/commit", {"ticket": competing["ticket"], "narration": "过期回合"}, 409)
    record("并发回合冲突保护", first["artifact"])
    for action in ("1", "1"):
        final = finish(prepared(player, action))
    assert "灯火守夜人" in final["reply"] and "3/3" in final["reply"]
    record("三轮剧情、道具与温暖结局", final["artifact"])
    scope_id = prepared(player, "回访")["memory_read"]["scope_id"]
    artifact_id = final["artifact"]["artifact_id"]
    revision_records = []
    for revision in (1, final["artifact"]["revision"]):
        response = client.get(f"http://127.0.0.1:{PC_PORT}/v1/scopes/{scope_id}/artifacts/atomic-memory/{artifact_id}/revisions/{revision}")
        response.raise_for_status()
        revision_records.append(response.json())
    (EVIDENCE / "powercontext_readback.json").write_text(
        json.dumps({"description": "实际HTTP读取的不可变历史revision；内容为测试虚构回忆与规则演示台词", "records": revision_records},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    # Restart both services. Only PowerContext on-disk data and the configured signing secret survive.
    before = {"powercontext_pid": pc.pid, "backend_pid": backend.pid}
    stop(backend)
    assert_unavailable(API + "/health")
    stop(pc)
    assert_unavailable(f"http://127.0.0.1:{PC_PORT}/health/live")
    pc = spawn("powercontext", ["-c", "from powercontext.cli.app import main; main()", "server", "run", "--no-env-file"])
    wait_ready(pc, f"http://127.0.0.1:{PC_PORT}/health/live")
    backend = spawn("backend", ["-m", "uvicorn", "backend.app:app", "--port", str(API_PORT), "--no-access-log"])
    wait_ready(backend, API + "/health")
    recalled = prepared(player, "回访")
    assert "蓝铁盒" in recalled["memory_read"]["stored_memory"]
    assert recalled["memory_read"]["artifact"] == final["artifact"]
    assert json.loads(recalled["game_context"])["state"]["turn"] == 3
    record("PowerContext和后端双进程重启后跨会话恢复", {**recalled["memory_read"], "before": before,
           "after": {"powercontext_pid": pc.pid, "backend_pid": backend.pid}, "both_ports_unavailable_between_restarts": True})
    stranger = prepared("acceptance_002", "回访")
    assert not stranger["memory_read"]["stored_memory"] and stranger["memory_read"]["artifact"] is None
    assert stranger["memory_read"]["scope_id"] != recalled["memory_read"]["scope_id"]
    record("不同玩家Scope隔离")
    chapter = finish(prepared(player, "新周目"))
    assert "蓝铁盒" in chapter["reply"] and "第 2 周目" in chapter["reply"] and "0/3" in chapter["reply"]
    record("新周目保留回忆与结局，重置轮次")
    for who, action, ending in [("chaos_user_001", "2", "宇宙整活王"), ("moon_user_001", "3", "月光合伙人")]:
        for _ in range(3):
            result = finish(prepared(who, action))
        assert ending in result["reply"]
    record("另外两种结局可达")
    # Execute the four shipped Code nodes on CPython; real companion/PC HTTP, simulated model content.
    nodes = {n["id"]: n["data"] for n in load_dsl()["workflow"]["graph"]["nodes"]}
    start = run_code(nodes["input"], {"player_code": player, "action": "回访"})
    prep_response = request("/api/prepare", json.loads(start["body"]))
    model_request = run_code(nodes["prompt"], {"body": json.dumps(prep_response), "status_code": 200, "model_name": "SIMULATED"})
    messages = json.loads(model_request["body"])["messages"]
    assert "蓝铁盒" in messages[1]["content"]
    simulated = {"choices": [{"message": {"content": "【模拟模型响应】阿回记得蓝铁盒里的橘子糖。"}}]}
    commit_body = run_code(nodes["narration"], {"body": json.dumps(simulated), "status_code": 200,
                                               "ticket": model_request["ticket"]})
    committed = request("/api/commit", json.loads(commit_body["body"]))
    output = run_code(nodes["result"], {"body": json.dumps(committed), "status_code": 200})
    assert "蓝铁盒" in output["reply"]
    record("DSL四个代码节点本地执行与真实HTTP串联（模型响应模拟，未使用Dify引擎）")
    evidence = {"run_id": run_id, "time_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "powercontext_commit": "d16fb73a3a887e280699f93b4a5d4c1bf6cf6ed0", "checks": results,
                "validation_boundary": {"real_powercontext_http": True, "real_backend_http": True,
                                        "process_restart": True, "dify_runtime_import": False,
                                        "real_model_inference": False, "model_integration": "simulated response only"}}
    path = EVIDENCE / "local_acceptance.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    (EVIDENCE / "sample_reply.txt").write_text(final["reply"], encoding="utf-8")
    print("证据已保存：" + str(path), flush=True)

if __name__ == "__main__":
    try:
        main()
    finally:
        for process in children:
            stop(process)
        for log in handles:
            log.close()
        client.close()
