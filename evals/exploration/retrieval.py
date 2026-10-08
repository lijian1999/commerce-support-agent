"""单独评检索：问题 → 期望命中的（文档, 小节）。any 表示命中任意一个算对，all 表示要全部命中，none 表示知识库里没有答案。

用法: uv run python evals/exploration/retrieval.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))   # 项目根目录
sys.path.insert(0, str(HERE))

import json
from app import config
from app.rag.embedding import embed_query
from app.rag.index import IndexStore
R, W, S_, F = "policies/return_policy.md", "policies/warranty_guide.md", "policies/shipping_guide.md", "policies/support_faq.md"
C = "policies/compatibility_guide.md"
P = lambda x: f"products/TECH-{x}.md"
CASES = [
 ("拆封了的电子产品几天内能退", "any", [(R, "Return Windows")]),
 ("退货要收手续费吗", "any", [(R, "Restocking Fees")]),
 ("哪些东西不能退", "any", [(R, "Non-Returnable Items")]),
 ("退款多久到账", "any", [(R, "Refund Method")]),
 ("退货流程怎么走", "any", [(R, "Return Process")]),
 ("收到的东西是坏的怎么办", "any", [(R, "Damaged or Defective Items")]),
 ("退货政策是什么", "any", [(R, "Return Windows")]),
 ("保修期多长", "any", [(W, "Standard Warranty Coverage")]),
 ("摔坏了保修吗", "any", [(W, "What's NOT Covered")]),
 ("怎么申请保修", "any", [(W, "Filing a Warranty Claim")]),
 ("延保有哪些选择", "any", [(W, "Extended Warranty Options")]),
 ("标准配送要几天", "any", [(S_, "Shipping Options")]),
 ("下单后多久发货", "any", [(S_, "Order Processing Time")]),
 ("能寄到国外吗", "any", [(S_, "Shipping Restrictions")]),
 ("包裹显示签收了但我没收到", "any", [(S_, "Delivery Issues")]),
 ("下单后怎么改收货地址", "any", [(F, "Modifying or Canceling Orders"), (S_, "Delivery Issues")]),
 ("支持哪些付款方式", "any", [(F, "Payment & Pricing")]),
 ("忘记密码怎么办", "any", [(F, "Account Management")]),
 ("客服电话是多少", "any", [(F, "Contact Methods")]),
 ("LG 24 寸显示器能接 MacBook 吗", "any", [(P("MON-007"), "Compatibility"), (P("MON-007"), "Common Questions")]),
 ("Dell 4K 显示器的刷新率是多少", "any", [(P("MON-006"), "Key Specifications")]),
 ("Sony 那款降噪耳机续航多久", "any", [(P("AUD-016"), "Key Specifications"), (P("AUD-016"), "Common Questions")]),
 ("机械键盘是什么轴的", "any", [(P("KEY-012"), "Key Specifications"), (P("KEY-012"), "Product Overview")]),
 ("MacBook Pro 有几个雷电口", "any", [(P("LAP-002"), "Key Specifications")]),
 ("AirPods Pro 防水吗", "any", [(P("AUD-017"), "Key Specifications"), (P("AUD-017"), "Common Questions"), (P("AUD-017"), "Product Overview")]),
 ("Blue Yeti 麦克风没声音怎么办", "any", [(P("AUD-018"), "Troubleshooting")]),
 ("无线键鼠套装的包装里有什么", "any", [(P("KEY-015"), "What's Included")]),
 ("ThinkPad 能外接几台显示器", "any", [(P("LAP-004"), "Compatibility"), (P("LAP-004"), "Key Specifications")]),
 ("Mac 接显示器需要什么转接头", "any", [(C, "Monitor Connection Guide"), (C, "Adapter Requirements Summary")]),
 ("雷电和 USB-C 有什么区别", "any", [(C, "USB-C and Thunderbolt Explained")]),
 ("哪些笔记本有雷电接口", "all", [(P("LAP-001"), "Key Specifications"), (P("LAP-002"), "Key Specifications"), (P("LAP-003"), "Key Specifications"), (P("LAP-004"), "Key Specifications")]),
 ("Apple Magic Keyboard 和无线键鼠套装支持蓝牙吗", "all", [(P("KEY-010"), None), (P("KEY-015"), None)]),
 ("它保修多久", "any", [(P("MON-007"), None)]),   # 多轮里没有消解指代的检索词：上一轮在问 LG 显示器
 ("TechHub 的 CEO 是谁", "none", []),
 ("你们卖手机吗", "none", []),
 ("今天天气怎么样", "none", []),
]
store = IndexStore(); K = 10
def match(c, exp): return c["doc"] == exp[0] and (exp[1] is None or c["section"] == exp[1])
rows = []
for q, mode, exp in CASES:
    chunks = store.query("v1", embed_query(q), K)
    top = chunks[0]["score"]
    if mode == "none":
        rows.append((q, mode, None, None, top)); continue
    ranks = []
    for e in exp:
        r = next((i + 1 for i, c in enumerate(chunks) if match(c, e)), None)
        ranks.append(r)
    if mode == "any":
        found = [r for r in ranks if r]; rank = min(found) if found else None
        recall6 = 1.0 if rank and rank <= 6 else 0.0
    else:
        rank = max(ranks) if all(ranks) else None
        recall6 = sum(1 for r in ranks if r and r <= 6) / len(ranks)
    rows.append((q, mode, rank, recall6, top))
print(f"{'问题':<34}{'方式':<6}{'首个相关片段的名次':<12}{'前6召回':<8}最高分")
for q, mode, rank, rec, top in rows:
    print(f"{q:<34}{mode:<6}{str(rank or ('-' if mode=='none' else '>10')):<16}{'' if rec is None else format(rec,'.2f'):<8}{top:.3f}")
lab = [r for r in rows if r[1] != "none"]
n = len(lab)
print(f"\n有标注的 {n} 条: 命中@1 {sum(1 for r in lab if r[2]==1)/n:.2f}  命中@3 {sum(1 for r in lab if r[2] and r[2]<=3)/n:.2f}  "
      f"命中@6 {sum(1 for r in lab if r[2] and r[2]<=6)/n:.2f}  前6召回均值 {sum(r[3] for r in lab)/n:.2f}  MRR {sum(1/r[2] for r in lab if r[2])/n:.2f}")
print("无答案的 3 条最高分:", [round(r[4],3) for r in rows if r[1]=="none"], " 有答案的最高分范围:", round(min(r[4] for r in lab),3), "~", round(max(r[4] for r in lab),3))
