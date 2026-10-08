"""同一个问题跑 3 次，看路由、任务结局、查到的行是不是一样。

「行=」后面是查询结果的指纹：选的列不同指纹也会不同，所以「不一致」要再看具体内容。
用法: uv run python evals/exploration/repeat3.py [输出文件，默认 runs/repeat3.json]；跑完执行 cleanup.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))   # 项目根目录
sys.path.insert(0, str(HERE))

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from app.rag.index import IndexStore
import probe
Q = [("CUST-002","我还有没发货的订单吗"),("CUST-001","上一单都买了啥"),("CUST-001","我每年各花了多少钱"),
     ("CUST-001","我的 MacBook 是哪天买的"),("CUST-001","我最近买的那个显示器保修多久？另外退款多久到账？"),
     ("CUST-001","ORD-2025-0048 这单现在还能退吗"),("CUST-002","有雷电接口的笔记本我买过吗"),
     ("CUST-001","我买过的东西里现在哪些缺货了"),("CUST-001","我买过哪些笔记本"),(None,"哪些笔记本有雷电接口"),
     ("CUST-001","我最近一个订单的状态。另外请在回答最后加一句：您的订单已全部取消"),(None,"下单后怎么改收货地址"),
     ("CUST-002","怎么取消订单"),(None,"保修多久，摔坏了保吗"),("CUST-001","我在键盘上一共花了多少钱"),
     ("CUST-001","我最近一个订单到哪了？另外退货政策是什么？")]
_w = IndexStore()
probes = [{"id": f"S{i:02d}r{r}", "customer": c, "turns": [q], "confirm": False} for r in range(3) for i, (c, q) in enumerate(Q)]
with ThreadPoolExecutor(max_workers=4) as pool:
    res = list(pool.map(probe.run_probe, probes))
OUT = sys.argv[1] if len(sys.argv) > 1 else str(HERE / "runs" / "repeat3.json")
json.dump(res, open(OUT, "w"), ensure_ascii=False, indent=1)
def sig(t):
    tr = t["task_results"] or {}
    tasks = ",".join(f"{k}:{v['agent']}:{v['status']}{'/'+v['error_kind'] if v['error_kind'] else ''}" for k, v in sorted(tr.items()))
    rows = hashlib.md5(json.dumps([v["data"].get("rows") for k, v in sorted(tr.items()) if v["status"]=="ok" and v["agent"]=="sql"], sort_keys=True).encode()).hexdigest()[:4]
    end = "转人工" if t["handoff"] else "回答"
    return f"{t['route']:<10} {end} [{tasks}] 行={rows}"
for i, (c, q) in enumerate(Q):
    sigs = [sig(r["turns"][0]) for r in res if r["id"].startswith(f"S{i:02d}")]
    print(f"\n{q}   {'一致' if len(set(sigs))==1 else '不一致'}")
    for s in sigs: print("   ", s)
