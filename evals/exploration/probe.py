"""批量跑 Agent：每个探针是一段会话（一轮或多轮），记录每一步和最终状态。

用法: uv run python evals/exploration/probe.py evals/exploration/questions/orders.json evals/exploration/runs/orders.json
问题文件: [{"id", "customer", "turns": [..], "confirm": true/false, "fault", "index_version"}]
会话编号统一以 probe- 开头，方便事后清理它产生的工单和取消记录（见 cleanup.py）。
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))   # 项目根目录
sys.path.insert(0, str(HERE))

import json
import time
from concurrent.futures import ThreadPoolExecutor

from langgraph.checkpoint.memory import InMemorySaver

from app.graph import build_graph
from app.runner import make_context, run_turn


def run_probe(p: dict) -> dict:
    graph = build_graph(checkpointer=InMemorySaver())
    thread_id = f"probe-{p['id']}"
    out = {"id": p["id"], "customer": p.get("customer"), "turns": []}
    for text in p["turns"]:
        ctx = make_context(p.get("customer"), None, p.get("fault", "off"), session_id=thread_id,
                           index_version=p.get("index_version"))
        steps, interrupts, values = [], [], {}
        t0 = time.time()
        resume = None
        first = True
        try:
            while True:
                again = False
                for kind, payload in run_turn(graph, ctx, thread_id, text if first else None, resume):
                    if kind == "step":
                        steps.append(payload)
                    elif kind == "interrupt":
                        interrupts.append(payload)
                        resume = {"confirm": bool(p.get("confirm")), "order_id": payload["order_id"]}
                        again = True
                    else:
                        values = payload
                first = False
                if not again:
                    break
            err = None
        except Exception as e:  # 记录下来，不让一个探针拖垮整批
            err = f"{type(e).__name__}: {e}"
        out["turns"].append({
            "question": text, "seconds": round(time.time() - t0, 1), "error": err,
            "route": values.get("route"), "answer": values.get("answer"),
            "sources": values.get("sources"), "plan": values.get("plan"),
            "task_results": values.get("task_results"), "handoff": values.get("handoff"),
            "cancel": values.get("cancel"), "interrupts": interrupts, "steps": steps,
        })
    return out


def show(r: dict) -> str:
    lines = [f"\n######## {r['id']}  客户={r['customer'] or '未登录'}"]
    for t in r["turns"]:
        lines.append(f"你> {t['question']}   ({t['seconds']}s)")
        if t["error"]:
            lines.append(f"  !! 异常 {t['error']}")
        for s in t["steps"]:
            summary = str(s["summary"])
            if s["name"].endswith("/retrieve") and isinstance(s["detail"], dict) and s["detail"].get("chunks") is not None:
                d = s["detail"]
                summary += f"  检索词: {d['query'][:120]}"
                for c in d["chunks"]:
                    summary += f"\n      {c.get('score', 0):.3f}  {c['doc']} § {c['section']}"
            elif s["name"].endswith("/execute") and isinstance(s["detail"], dict) and "rows" in s["detail"]:
                rows = json.dumps(s["detail"]["rows"], ensure_ascii=False)
                summary += "  " + (rows if len(rows) < 700 else rows[:700] + " …")
            elif s["name"] == "answer":
                summary = "(见下)"
            elif len(summary) > 900:
                summary = summary[:900] + " …"
            lines.append(f"  [{s['name']}] {summary}")
            for n in s["notes"]:
                lines.append(f"      ↻ {n}")
        for i in t["interrupts"]:
            lines.append(f"  ⏸ 预览 {i['order_id']} 合计 {i['total_amount']}")
        models = [s["name"] for s in t["steps"] if s["kind"] == "model"]
        sqls = sum(1 for s in t["steps"] if s["name"].endswith("/execute"))
        lines.append(f"客服> {t['answer']}")
        lines.append(f"  —— route={t['route']}  模型步骤 {len(models)}  SQL 执行 {sqls}  来源 {t['sources']}")
    return "\n".join(lines)


if __name__ == "__main__":
    probes = json.load(open(sys.argv[1]))
    from app.rag.index import IndexStore
    _warm = IndexStore()   # 先在主线程建一次 Chroma 客户端，避免多线程同时首次创建时出错
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run_probe, probes))
    json.dump(results, open(sys.argv[2], "w"), ensure_ascii=False, indent=1)
    for r in results:
        print(show(r))
