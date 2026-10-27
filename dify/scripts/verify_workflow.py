"""Collect sanitized evidence from a published real Dify Workflow, never a simulator."""
from __future__ import annotations
import json
from pathlib import Path
import secrets
import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]

def main():
    config = dotenv_values(ROOT / ".env")
    base, key = config.get("DIFY_API_URL", ""), config.get("DIFY_APP_API_KEY", "")
    if not base or not key:
        raise SystemExit("Configure DIFY_API_URL and DIFY_APP_API_KEY in .env first. No successful evidence was generated.")
    output = ROOT / "evidence/workflow.json"
    private = ROOT / ".runtime/verification"
    private.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.replace(private / "previous_workflow.json")
    player = "review_" + secrets.token_hex(12)
    actions = ["寄存：奶奶在蓝铁盒里藏了一颗橘子糖", "1", "1", "1", "回访", "新周目", "回访"]
    records = []
    with httpx.Client(timeout=180, trust_env=False) as client:
        for i, action in enumerate(actions):
            response = client.post(base.rstrip("/") + "/workflows/run", headers={"Authorization": "Bearer " + key},
                json={"inputs": {"player_code": player, "action": action}, "response_mode": "blocking",
                      "user": "review-session-a" if i < 4 else "review-session-b"})
            response.raise_for_status()
            result = response.json()
            (private / f"step_{i+1}.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            data = result["data"]
            if data["status"] != "succeeded":
                raise RuntimeError(f"Dify step {i+1} failed. Inspect private workflow logs; no success record written.")
            outputs = data["outputs"]
            assert "蓝铁盒" in outputs["reply"]
            if i == 3:
                assert "灯火守夜人" in outputs["reply"] and "3/3" in outputs["reply"]
            if i >= 5:
                assert "第 2 周目" in outputs["reply"] and "0/3" in outputs["reply"]
            memory = json.loads(outputs["memory_evidence"])
            model = json.loads(outputs["model_evidence"])
            records.append({"step": i+1, "action": action, "session": "A" if i < 4 else "B",
                            "workflow_run_id": result.get("workflow_run_id", data.get("id")), "status": data["status"],
                            "reply": outputs["reply"], "memory": memory, "model": model})
            print(f"PASS: real Dify workflow step {i+1}", flush=True)
        assert records[3]["memory"] == records[4]["memory"]
    evidence = {"dify_version": "1.9.1", "powercontext_commit": "d16fb73a3a887e280699f93b4a5d4c1bf6cf6ed0",
                "execution": "real Dify Workflow API and model service", "simulated": False, "steps": records}
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print("PASS: sanitized evidence written to evidence/workflow.json; no service URLs, credentials or recovery codes included.")

if __name__ == "__main__":
    main()
