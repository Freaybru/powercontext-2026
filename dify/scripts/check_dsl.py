"""Structural checks and local Code-node execution. This is not a Dify import test."""
from __future__ import annotations
import json
from pathlib import Path
import re
import yaml

ROOT = Path(__file__).resolve().parents[1]

def load_dsl():
    return yaml.safe_load((ROOT / "dsl/echo_shop.workflow.yml").read_text(encoding="utf-8"))

def run_code(data, values):
    namespace = {}
    exec(compile(data["code"], "<DSL code node>", "exec"), namespace)
    result = namespace["main"](**values)
    assert set(result) == set(data["outputs"])
    for key, value in result.items():
        if data["outputs"][key]["type"] == "string":
            assert isinstance(value, str)
    return result

def check():
    dsl = load_dsl()
    assert dsl["version"] == "0.4.0" and dsl["app"]["mode"] == "workflow"
    graph = dsl["workflow"]["graph"]
    nodes = {n["id"]: n["data"] for n in graph["nodes"]}
    assert len(nodes) == len(graph["nodes"]) == 9
    env = {x["name"] for x in dsl["workflow"]["environment_variables"]}
    assert env == {"ECHO_BASE_URL", "ECHO_API_TOKEN", "MODEL_URL", "MODEL_NAME", "MODEL_API_KEY"}
    seen = {"env"}
    for node in graph["nodes"]:
        data = node["data"]
        for selector in re.findall(r"\{\{#([^#]+)#\}\}", json.dumps(data)):
            assert selector.split(".")[0] in seen
        for var in data.get("variables", []) if data["type"] == "code" else []:
            assert var["value_selector"][0] in seen
        if data["type"] == "code":
            compile(data["code"], "<DSL code node>", "exec")
        seen.add(node["id"])
    assert len(graph["edges"]) == len(nodes) - 1
    for edge in graph["edges"]:
        assert edge["source"] in nodes and edge["target"] in nodes
    source = nodes["input"]
    out = run_code(source, {"player_code": "example_001", "action": '寄存：一段"带引号"的回忆\n第二行'})
    assert json.loads(out["body"])["action"].endswith("第二行")
    # Failed model responses and empty outputs must stop before the commit HTTP node.
    for status, body in [(429, "{}"), (200, '{"choices":[{"message":{"content":""}}]}')]:
        try:
            run_code(nodes["narration"], {"body": body, "status_code": status, "ticket": "unused"})
        except ValueError:
            pass
        else:
            raise AssertionError("模型异常必须停止工作流")
    print("PASS: DSL结构、变量引用、4个代码节点语法、JSON转义和模型失败保护。未运行Dify引擎。")

if __name__ == "__main__":
    check()
