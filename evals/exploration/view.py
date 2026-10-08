"""把一批运行结果打印成可读的文本：每一步一行，检索列出命中的片段，最后是回答。

用法: uv run python evals/exploration/view.py evals/exploration/runs/orders.json [回答截断长度] [起始编号]
"""
import json, sys
n = int(sys.argv[2]) if len(sys.argv) > 2 else 400
start = sys.argv[3] if len(sys.argv) > 3 else ""
for r in json.load(open(sys.argv[1])):
    if r["id"] < start: continue
    print(f"\n### {r['id']} 客户={r['customer'] or '未登录'}")
    for t in r["turns"]:
        print(f"你> {t['question']}  ({t['seconds']}s)" + (f"  !!{t['error']}" if t["error"] else ""))
        for s in t["steps"]:
            d = s["detail"] if isinstance(s["detail"], dict) else {}
            if s["name"] == "router":
                print(f"  [router] {d.get('route')} | {d.get('reason')}")
            elif s["name"] == "planner":
                print("  [planner] " + str(s["summary"]).replace("\n", "\n     "))
            elif s["name"].endswith("/sql_plan"):
                print(f"  [{s['name']}] {str(s['summary'])[:420]}")
            elif s["name"].endswith("/compile"):
                if "sql" in d: print(f"  [{s['name']}] params={d['params']}")
                else: print(f"  [{s['name']}] {str(s['summary'])[:300]}")
            elif s["name"].endswith("/execute"):
                rows = json.dumps(d.get("rows"), ensure_ascii=False)
                print(f"  [{s['name']}] {d.get('row_count')} 行 {rows[:500]}")
            elif s["name"].endswith("/retrieve"):
                print(f"  [{s['name']}] 检索词: {str(d.get('query'))[:160]}")
                for c in d.get("chunks") or []:
                    print(f"       {c['score']:.3f} {c['doc']} § {c['section']}")
            elif s["name"] == "answer":
                pass
            else:
                print(f"  [{s['name']}] {str(s['summary'])[:300]}")
            for note in s["notes"]: print(f"       ↻ {note}")
        for i in t["interrupts"]: print(f"  ⏸ 预览 {i['order_id']}")
        models = sum(1 for s in t["steps"] if s["kind"] == "model")
        sqls = sum(1 for s in t["steps"] if s["name"].endswith("/execute"))
        a = (t["answer"] or "").replace("\n", " ⏎ ")
        print(f"客服> {a[:n]}{' …' if len(a) > n else ''}")
        print(f"  —— route={t['route']} 模型 {models} SQL {sqls} 来源 {t['sources']}")
