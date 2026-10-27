"""Dify companion API. PowerContext is the only durable game-state store."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from collections import defaultdict
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .game import SYSTEM_PROMPT, encode_state, initial_state, panel, transition

PC_URL = os.environ.get("POWERCONTEXT_URL", "http://127.0.0.1:18741").rstrip("/")
PC_TOKEN = os.environ.get("POWERCONTEXT_TOKEN", "")
APP_TOKEN = os.environ.get("ECHO_API_TOKEN", "")
SECRET = os.environ.get("ECHO_SIGNING_SECRET", "")
if len(APP_TOKEN) < 24 or len(SECRET) < 32:
    raise RuntimeError("请先配置至少 24 字符 ECHO_API_TOKEN 和至少 32 字符 ECHO_SIGNING_SECRET")
DIFY_URL = os.environ.get("DIFY_API_URL", "").rstrip("/")
DIFY_KEY = os.environ.get("DIFY_APP_API_KEY", "")
KIND = "echo-shop-state-v1"
locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
app = FastAPI(title="回声杂货铺", version="1.0.0")


class Turn(BaseModel):
    player_code: str = Field(min_length=8, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    action: str = Field(min_length=1, max_length=180)
    request_id: str = Field(default_factory=lambda: secrets.token_hex(16), min_length=8, max_length=100)


class Commit(BaseModel):
    ticket: str = Field(min_length=10, max_length=20000)
    narration: str = Field(min_length=1, max_length=600)


def authorize(authorization: str = Header(default="")) -> None:
    if not secrets.compare_digest(authorization, "Bearer " + APP_TOKEN):
        raise HTTPException(401, "应用接口鉴权失败")


async def pc(method: str, path: str, data=None, headers=None):
    auth = {"Authorization": "Bearer " + PC_TOKEN} if PC_TOKEN else {}
    try:
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            response = await client.request(method, PC_URL + path, json=data, headers={**auth, **(headers or {})})
    except httpx.HTTPError as exc:
        raise HTTPException(503, "PowerContext 不可用；没有切换至其他存储，请检查服务") from exc
    if response.status_code in (409, 412):
        raise HTTPException(409, "记忆已由另一个请求更新，请重新执行本次行动")
    if response.is_error:
        raise HTTPException(502, f"PowerContext 请求失败，HTTP {response.status_code}")
    return response.json(), response.headers


async def load(player_code: str):
    # The recovery code is the toy game's capability. The model never chooses scope IDs.
    digest = hmac.new(SECRET.encode(), player_code.encode(), hashlib.sha256).hexdigest()
    scope, _ = await pc("POST", "/v1/scopes", {"title": "回声杂货铺 " + digest[:12],
                       "summary": "同一玩家跨 Dify 会话的剧情、道具与寄存回忆", "idempotency_key": "echo-" + digest})
    scope_id = scope["scope_id"]
    memories, _ = await pc("POST", "/v1/atomic-memory/list",
                           {"scope_id": scope_id, "kind": KIND, "limit": 2})
    if len(memories["items"]) > 1:
        raise HTTPException(409, "发现重复游戏存档，停止写入以保护历史")
    if not memories["items"]:
        return scope_id, initial_state(), None, None
    record = memories["items"][0]
    artifact = record["artifact"]
    latest, headers = await pc("GET", f"/v1/scopes/{scope_id}/artifacts/atomic-memory/{artifact['artifact_id']}")
    artifact = {k: latest[k] for k in ("family", "artifact_id", "revision")}
    return scope_id, json.loads(latest["content"]["text"]), artifact, headers["etag"]


def seal(payload: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()).decode()
    signature = hmac.new(SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return raw + "." + signature


def unseal(ticket: str) -> dict:
    try:
        raw, signature = ticket.rsplit(".", 1)
        expected = hmac.new(SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
        if not secrets.compare_digest(signature, expected):
            raise ValueError("signature")
        payload = json.loads(base64.urlsafe_b64decode(raw))
        if payload["expires"] < time.time():
            raise HTTPException(409, "本轮等待超过 15 分钟，请重新输入行动")
        return payload
    except HTTPException:
        raise
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(400, "无效回合凭据") from exc


@app.get("/health")
async def health():
    await pc("GET", "/health/live")
    return {"status": "ok", "storage": "PowerContext",
            "dify_configured": bool(DIFY_URL and DIFY_KEY)}


@app.post("/api/prepare", dependencies=[Depends(authorize)])
async def prepare(turn: Turn):
    if len(locks) > 1000 and turn.player_code not in locks:
        raise HTTPException(503, "演示实例玩家容量已满，请重启后再试")
    async with locks[turn.player_code]:
        scope_id, state, artifact, etag = await load(turn.player_code)
        new_state, detail, changed = transition(state, turn.action)
        ticket = seal({"player_code": turn.player_code, "scope_id": scope_id, "etag": etag,
                       "artifact": artifact, "next_state": new_state, "changed": changed,
                       "request_id": turn.request_id, "expires": int(time.time()) + 900})
        return {"ticket": ticket, "system_prompt": SYSTEM_PROMPT,
                "game_context": json.dumps(detail, ensure_ascii=False), "operation": detail["operation"],
                "memory_read": {"scope_id": scope_id, "artifact": artifact, "stored_memory": state["memory"]}}


@app.post("/api/commit", dependencies=[Depends(authorize)])
async def commit(body: Commit):
    payload = unseal(body.ticket)
    async with locks[payload["player_code"]]:
        scope_id, current, artifact, etag = await load(payload["player_code"])
        if current["last_request"] == payload["request_id"]:
            return {"reply": current["last_reply"], "artifact": artifact, "replayed": True}
        if scope_id != payload["scope_id"] or etag != payload["etag"]:
            raise HTTPException(409, "另一个回合已提交，请重新执行当前行动")
        state = payload["next_state"]
        reply = body.narration.strip() + "\n\n---\n\n" + panel(state)
        if not payload["changed"]:
            return {"reply": reply, "artifact": artifact, "replayed": False, "state_changed": False}
        state["last_request"] = payload["request_id"]
        state["last_reply"] = reply
        content = {"kind": KIND, "text": encode_state(state)}
        if artifact:
            result, _ = await pc("PUT", f"/v1/scopes/{scope_id}/artifacts/atomic-memory/{artifact['artifact_id']}",
                                 {"content": content}, {"If-Match": etag})
        else:
            result, _ = await pc("POST", f"/v1/scopes/{scope_id}/artifacts",
                                 {"family": "atomic-memory", "content": content})
        return {"reply": reply, "artifact": {k: result[k] for k in ("family", "artifact_id", "revision")},
                "replayed": False, "state_changed": True}


@app.post("/api/play")
async def play(turn: Turn):
    if not DIFY_URL or not DIFY_KEY:
        raise HTTPException(503, "请先导入 DSL、发布应用并配置 DIFY_API_URL 和 DIFY_APP_API_KEY")
    try:
        async with httpx.AsyncClient(timeout=180, trust_env=False) as client:
            response = await client.post(DIFY_URL + "/workflows/run", headers={"Authorization": "Bearer " + DIFY_KEY},
                json={"inputs": {"player_code": turn.player_code, "action": turn.action},
                      "response_mode": "blocking", "user": hashlib.sha256(turn.player_code.encode()).hexdigest()})
        if response.is_error:
            raise HTTPException(502, f"Dify HTTP {response.status_code}，请检查工作流日志")
        data = response.json()["data"]
        if data["status"] != "succeeded":
            raise HTTPException(502, "Dify 执行失败，请查看工作流节点日志；失败轮次未提交游戏状态")
        return {"reply": data["outputs"]["reply"], "workflow_run_id": data["id"]}
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise HTTPException(502, "Dify 响应不可用，请检查地址、密钥和节点配置") from exc


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")
