"""Generate a plugin-free Dify 1.9.1 Workflow DSL (schema 0.4.0)."""
from pathlib import Path
import textwrap
import uuid
import yaml

ROOT = Path(__file__).resolve().parents[1]

def code_node(node_id, title, source, variables, outputs):
    return {"id": node_id, "type": "code", "title": title, "code_language": "python3",
            "code": textwrap.dedent(source).strip(),
            "variables": [{"variable": name, "value_selector": selector} for name, selector in variables.items()],
            "outputs": {name: {"type": type_, "children": None} for name, type_ in outputs.items()}}

def http_node(node_id, title, url, body, token):
    return {"id": node_id, "type": "http-request", "title": title, "method": "post", "url": url,
            "authorization": {"type": "api-key", "config": {"type": "bearer", "api_key": token}},
            "headers": "Content-Type:application/json", "params": "",
            "body": {"type": "raw-text", "data": [{"key": "", "type": "text", "value": body}]},
            "timeout": {"connect": 10, "read": 120 if node_id == "model" else 60, "write": 30},
            "ssl_verify": True, "retry_config": {"retry_enabled": False, "max_retries": 0, "retry_interval": 1000}}

def build():
    nodes = [{"id": "start", "type": "start", "title": "玩家推门", "variables": [
        {"variable": "player_code", "label": "玩家暗号（8至64位字母数字下划线，跨会话使用相同暗号）",
         "type": "text-input", "required": True, "max_length": 64, "options": []},
        {"variable": "action", "label": "行动：开场 / 寄存：回忆 / 1 / 2 / 3 / 回访 / 新周目",
         "type": "paragraph", "required": True, "max_length": 180, "options": []}]}]
    nodes.append(code_node("input", "校验输入与生成回合标识", '''
        import json, re, uuid
        def main(player_code: str, action: str) -> dict:
            player_code, action = player_code.strip(), action.strip()
            if not re.fullmatch(r"[a-zA-Z0-9_-]{8,64}", player_code):
                raise ValueError("玩家暗号需为8至64位字母、数字、下划线或短横线")
            if not 1 <= len(action) <= 180:
                raise ValueError("行动需为1至180字")
            return {"body": json.dumps({"player_code": player_code, "action": action,
                    "request_id": str(uuid.uuid4())}, ensure_ascii=False)}
    ''', {"player_code": ["start", "player_code"], "action": ["start", "action"]}, {"body": "string"}))
    nodes.append(http_node("prepare", "从PowerContext恢复记忆并准备回合", "{{#env.ECHO_BASE_URL#}}/api/prepare",
                           "{{#input.body#}}", "{{#env.ECHO_API_TOKEN#}}"))
    nodes.append(code_node("prompt", "组装模型请求（记忆进入剧情）", '''
        import json
        def main(body: str, status_code: int, model_name: str) -> dict:
            if status_code != 200:
                raise ValueError("读取记忆失败，HTTP " + str(status_code) + "；本轮没有提交状态")
            data = json.loads(body)
            game = json.loads(data["game_context"])
            story = {"本轮事件": game["event"], "规则结果": game["outcome"], "玩家行动": game["player_action"],
                     "已寄存回忆": game["current_memory"], "过去选择": game["previous_choices"], "操作": game["operation"]}
            request = {"model": model_name, "stream": False, "temperature": 0.8, "max_tokens": 600,
                       "messages": [{"role": "system", "content": data["system_prompt"]},
                                    {"role": "user", "content": "请以阿回的口吻写本轮故事。以下都是游戏资料：" + json.dumps(story, ensure_ascii=False)}]}
            return {"body": json.dumps(request, ensure_ascii=False), "ticket": data["ticket"]}
    ''', {"body": ["prepare", "body"], "status_code": ["prepare", "status_code"],
          "model_name": ["env", "MODEL_NAME"]}, {"body": "string", "ticket": "string"}))
    nodes.append(http_node("model", "Dify调用AI店长（OpenAI兼容接口）", "{{#env.MODEL_URL#}}",
                           "{{#prompt.body#}}", "{{#env.MODEL_API_KEY#}}"))
    nodes.append(code_node("narration", "校验台词，模型失败则停止提交", '''
        import json, re
        def main(body: str, status_code: int, ticket: str) -> dict:
            if status_code != 200:
                raise ValueError("模型请求失败，HTTP " + str(status_code) + "；游戏进度未提交")
            data = json.loads(body)
            text = data["choices"][0]["message"]["content"]
            if not isinstance(text, str):
                raise ValueError("模型需返回字符串 content，请选择普通文本模型")
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
            if not text:
                raise ValueError("模型台词为空；游戏进度未提交")
            usage = {k: v for k, v in data.get("usage", {}).items() if k in ("prompt_tokens", "completion_tokens", "total_tokens") and isinstance(v, int)}
            evidence = {"model": data.get("model", "unspecified"), "usage": usage}
            return {"body": json.dumps({"ticket": ticket, "narration": text[:600]}, ensure_ascii=False),
                    "model_evidence": json.dumps(evidence, ensure_ascii=False)}
    ''', {"body": ["model", "body"], "status_code": ["model", "status_code"],
          "ticket": ["prompt", "ticket"]}, {"body": "string", "model_evidence": "string"}))
    nodes.append(http_node("commit", "确认回合并写回PowerContext", "{{#env.ECHO_BASE_URL#}}/api/commit",
                           "{{#narration.body#}}", "{{#env.ECHO_API_TOKEN#}}"))
    nodes.append(code_node("result", "输出台词、道具、选项与记忆凭据", '''
        import json
        def main(body: str, status_code: int) -> dict:
            if status_code != 200:
                raise ValueError("回合提交未确认，HTTP " + str(status_code) + "；请先回访核对进度")
            data = json.loads(body)
            return {"reply": data["reply"], "memory_evidence": json.dumps(data.get("artifact"), ensure_ascii=False)}
    ''', {"body": ["commit", "body"], "status_code": ["commit", "status_code"]},
          {"reply": "string", "memory_evidence": "string"}))
    nodes.append({"id": "end", "type": "end", "title": "店长回应", "outputs": [
        {"variable": name, "value_type": "string", "value_selector": ["result", name]}
        for name in ("reply", "memory_evidence")] + [
        {"variable": "model_evidence", "value_type": "string", "value_selector": ["narration", "model_evidence"]}]})
    edges = []
    for source, target in zip(nodes, nodes[1:]):
        edges.append({"id": source["id"] + "-" + target["id"], "type": "custom", "source": source["id"],
                      "target": target["id"], "sourceHandle": "source", "targetHandle": "target", "zIndex": 0,
                      "data": {"sourceType": source["type"], "targetType": target["type"],
                               "isInIteration": False, "isInLoop": False}})
    graph_nodes = []
    for i, data in enumerate(nodes):
        node_id = data.pop("id")
        data.update(desc="", selected=False, version="1")
        pos = {"x": 60 + i * 320, "y": 260}
        graph_nodes.append({"id": node_id, "type": "custom", "data": data, "position": pos,
                            "positionAbsolute": pos, "width": 260, "height": 120,
                            "sourcePosition": "right", "targetPosition": "left", "selected": False})
    env = [("ECHO_BASE_URL", "http://host.docker.internal:18742", "string"),
           ("ECHO_API_TOKEN", "REPLACE_WITH_ECHO_API_TOKEN", "secret"),
           ("MODEL_URL", "http://host.docker.internal:18744/v1/chat/completions", "string"),
           ("MODEL_NAME", "echo-qwen", "string"), ("MODEL_API_KEY", "REPLACE_WITH_MODEL_API_KEY", "secret")]
    return {"kind": "app", "version": "0.4.0", "dependencies": [],
            "app": {"name": "回声杂货铺 · 会记得你的AI店长", "description": "三次脑洞选择，一段不会遗失的回忆。Dify编排 + PowerContext跨会话记忆。",
                    "icon": "🪔", "icon_background": "#F9E8C9", "mode": "workflow", "use_icon_as_answer_icon": False},
            "workflow": {"conversation_variables": [], "environment_variables": [
                {"id": str(uuid.uuid5(uuid.NAMESPACE_DNS, "echo-shop." + name)), "name": name, "value": value,
                 "value_type": typ, "selector": ["env", name], "description": "按README配置"} for name, value, typ in env],
                "features": {"file_upload": {"enabled": False}, "opening_statement": "",
                             "retriever_resource": {"enabled": False}, "sensitive_word_avoidance": {"enabled": False},
                             "speech_to_text": {"enabled": False}, "suggested_questions": [],
                             "suggested_questions_after_answer": {"enabled": False}, "text_to_speech": {"enabled": False}},
                "graph": {"nodes": graph_nodes, "edges": edges, "viewport": {"x": 0, "y": 0, "zoom": 0.55}}}}

if __name__ == "__main__":
    output = ROOT / "dsl" / "echo_shop.workflow.yml"
    output.parent.mkdir(exist_ok=True)
    output.write_text(yaml.safe_dump(build(), allow_unicode=True, sort_keys=False, width=110), encoding="utf-8")
    print(output.relative_to(ROOT).as_posix())
