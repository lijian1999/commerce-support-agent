# 评测设计的探查材料

写 `docs/design.md` 6.1 的评测设计初稿之前，拿真实问题跑现在的 Agent 留下的材料。它不是评测集本身，是初稿里每条设计的依据；初稿里的编号（A03、C05……）对应这里的问题和输出。

| 路径 | 内容 |
|---|---|
| `questions/*.json` | 7 批问题，共 109 段会话 |
| `outputs/*.txt` | 可读的运行输出：先看这里 |
| `runs/*.json` | 原始结果，含每一步的完整 detail |
| `../poison_docs/` | 知识库侧对抗用的三篇诱导文档（正式评测也用它） |

```sh
uv run python evals/exploration/probe.py evals/exploration/questions/orders.json /tmp/orders.json   # 批量跑一批问题
uv run python evals/exploration/view.py evals/exploration/runs/orders.json                          # 把已有结果打印成文本
uv run python evals/exploration/retrieval.py     # 单独评检索（不调大模型）
uv run python evals/exploration/repeat3.py /tmp/repeat3.json   # 16 个问题各跑 3 次
uv run python evals/exploration/judge.py         # 试模型判分，和人工标的结论对比
uv run python evals/exploration/poisoned.py /tmp/poisoned.json # 在临时目录建带诱导文档的索引再跑
uv run python evals/exploration/cleanup.py       # 复原数据库（跑过会转人工或取消订单的批次之后执行）
```

模型输出不是完全确定的，重跑的结果会和 `runs/` 里存的有出入；`runs/` 和 `outputs/` 是 2026-10-07 那一次的记录。
