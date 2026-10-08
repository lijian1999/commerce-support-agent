# 任务 19：评测的运行器，和带诱导文档的索引

先读 `AGENTS.md`，再读接口约定 `docs/eval-contracts.md`（重点是第 3 节）和数据结构 `evals/schema.py`。本任务新建这些文件：`evals/runner.py`、`evals/poisoned_index.py`、`tests/test_eval_runner.py`。不要改别的文件。

## 背景

运行器把一条 Case（一段会话加环境）交给 Agent 跑，把每一轮的全部步骤和最终状态记成 `TurnRecord`。它和命令行用同一个入口 `app.runner.run_turn`。取消订单的 Case 会真的改库，所以运行器负责跑完后复原。

少数 Case 要用一个夹带了诱导文档的知识库索引（`case.env.index == "poisoned"`）。这个索引建在 `evals.schema.POISONED_INDEX_DIR`，不碰线上的索引目录。

## 第一步：`evals/runner.py` 里和数据库有关的三个函数

都用 `connect("root")`，SQL 一律参数化。

- `read_order_status(order_ids: list[str]) -> dict[str, str]`：返回这些订单现在的 `status`；库里没有的订单不出现在结果里；空列表直接返回 `{}`，不连库。
- `restore_orders(order_ids: list[str]) -> None`：把这些订单改回 `status = 'Processing'`、`cancelled_at = NULL`，并删掉 `order_cancellations` 里这些订单的记录。空列表什么都不做。
- `cleanup_sessions() -> dict`：删掉 `order_cancellations` 和 `handoff_tickets` 里 `session_id` 以 `evals.schema.SESSION_PREFIX` 开头的记录，返回 `{"cancellations": 删掉的条数, "tickets": 删掉的条数}`。

## 第二步：`run_case`

```python
def session_id(case: Case, run_index: int) -> str:
    return f"{SESSION_PREFIX}{case.id}-{run_index}"


def run_case(case: Case, run_index: int = 0, case_hash: str = "") -> CaseRun:
    """把一条 Case 跑一遍：每一轮记一个 TurnRecord；跑完复原它声明要复原的订单。"""
    graph = build_graph(checkpointer=InMemorySaver())
    thread = session_id(case, run_index)
    index_dir = str(POISONED_INDEX_DIR) if case.env.index == "poisoned" else None
    turns: list[TurnRecord] = []
    try:
        for turn in case.turns:
            ctx = make_context(case.env.customer, case.env.today, case.env.fault,
                               session_id=thread, index_dir=index_dir)
            steps: list[dict] = []
            interrupts: list[dict] = []
            values: dict = {}
            error: str | None = None
            started = time.time()
            text, resume = turn.user, None
            try:
                while True:
                    interrupted = False
                    for kind, payload in run_turn(graph, ctx, thread, text, resume):
                        if kind == "step":
                            steps.append(payload)
                        elif kind == "interrupt":
                            interrupts.append(payload)
                            resume = {"confirm": turn.confirm, "order_id": payload["order_id"]}
                            text = None
                            interrupted = True
                        else:
                            values = payload
                    if not interrupted:
                        break
            except Exception as exc:  # 一轮出错不影响后面的轮次，错误记进记录里
                error = f"{type(exc).__name__}: {exc}"
            turns.append({
                "question": turn.user,
                "seconds": round(time.time() - started, 1),
                "error": error,
                "route": values.get("route"),
                "answer": values.get("answer"),
                "sources": values.get("sources") or [],
                "plan": values.get("plan"),
                "task_results": values.get("task_results") or {},
                "handoff": values.get("handoff"),
                "cancel": values.get("cancel"),
                "interrupts": interrupts,
                "steps": steps,
                "db_orders": read_order_status(list(turn.expect.db_orders)),
            })
    finally:
        restore_orders(case.restore)
    return {"case_id": case.id, "case_hash": case_hash, "run_index": run_index, "turns": turns}
```

上面的代码原样使用，补上需要的 import（`build_graph` 来自 `app.graph`，`make_context`、`run_turn` 来自 `app.runner`，`InMemorySaver` 来自 `langgraph.checkpoint.memory`）。

```sh
uv run python -c "
from evals.schema import Case
from evals.runner import run_case, cleanup_sessions
c = Case.model_validate({'id': 'smoke', 'business': '订单查询', 'situation': 'x', 'env': {'customer': 'CUST-001'}, 'turns': [{'user': '我在键盘上一共花了多少钱'}]})
r = run_case(c)
t = r['turns'][0]
print(r['case_id'], r['run_index'], t['route'], t['error'], t['steps'][0]['name'], '237' in (t['answer'] or ''), t['db_orders'])
print(cleanup_sessions())"
```

应输出两行（这条命令会调用真实模型）：

```
smoke 0 sql None router True {}
{'cancellations': 0, 'tickets': 0}
```

## 第三步：`run_cases`

- `needs_serial(case: Case) -> bool`：下面任一条成立就返回 `True`：`case.restore` 非空；有某一轮 `confirm` 为真；有某一轮的 `expect.db_orders` 非空。这些 Case 会改库或要读库里的订单状态，不能和别的 Case 同时跑。
- `run_cases(cases: list[Case], repeats: int = 3, workers: int = 4, hashes: dict[str, str] | None = None, progress=None) -> list[CaseRun]`：
  1. 先在当前线程里建一次 `IndexStore()`（Chroma 的客户端第一次创建不是线程安全的，必须先在主线程建好）。
  2. 如果 `cases` 里有 `env.index == "poisoned"` 的：在当前线程里建一次 `IndexStore(POISONED_INDEX_DIR)`，读它的指针；指针为空就抛 `RuntimeError("带诱导文档的索引还没有构建，先运行 uv run python -m evals.poisoned_index")`。
  3. 一条 Case 的一次重复是一个作业：`(case, run_index)`，`run_index` 从 0 到 `repeats - 1`。每个作业调用 `run_case(case, run_index, (hashes or {}).get(case.id, ""))`。
  4. `needs_serial` 为假的 Case 的作业放进 `ThreadPoolExecutor(max_workers=workers)` 并行跑；`needs_serial` 为真的在并行的那批全部结束之后，在当前线程里逐个跑。
  5. 每个作业结束时，如果给了 `progress`，调用 `progress(已完成的作业数, 作业总数, 这个作业的 CaseRun)`。
  6. 全部结束后调用一次 `cleanup_sessions()`。
  7. 返回全部 `CaseRun`，按 `(case_id, run_index)` 排序。

## 第四步：`evals/poisoned_index.py`

- `build_poisoned_index() -> dict`：
  1. 建一个临时目录，把 `config.DOCS_DIR` 整个复制进去；再把 `evals.schema.POISON_DOCS_DIR` 下的 `.md` 文件复制进去：文件名以 `TECH-` 开头的放进 `products/`，其余放进 `policies/`。
  2. `store = IndexStore(POISONED_INDEX_DIR)`，`manifest = store.build(临时目录, runtime_config(), embed_texts)`，`store.publish(manifest["version"], runtime_config())`，`store.gc(keep=1)`。
  3. 删掉临时目录。
  4. 返回 `{"version": manifest["version"], "docs": manifest["stats"]["docs"], "chunks": manifest["stats"]["chunks"], "embedded": manifest["stats"]["embedded"], "index_hash": evals.env.index_hash(manifest["version"], POISONED_INDEX_DIR)}`。
- 命令行 `uv run python -m evals.poisoned_index`：调用它，打印一行 `f"带诱导文档的索引 {version}：{docs} 篇文档，{chunks} 个片段，这次新生成 Embedding {embedded} 个，指纹 {index_hash}"`。

```sh
uv run python -m evals.poisoned_index
uv run python -m evals.poisoned_index
uv run python scripts/index.py status
```

通过的标准（会调用真实的 Embedding 服务）：
- 前两条各打印一行，都以 `带诱导文档的索引 v` 开头，都含有 `33 篇文档，271 个片段`；第二条里是 `这次新生成 Embedding 0 个`；两行末尾的指纹相同。
- 第三条的输出是 `线上: v1    上一个: 无    已构建未发布: 无`（线上的索引没有被动过）。

## 第五步：测试

`tests/test_eval_runner.py`。不调用模型：用 `monkeypatch` 把 `evals.runner.run_turn` 换成假的生成器函数，把 `evals.runner.make_context` 换成记录参数并返回 `AppContext()` 的假函数，把 `evals.runner.build_graph` 换成返回 `None` 的函数。标了「连库」的测试用真实数据库，写入的数据在测试结束时删掉（用 `connect("root")`；`handoff_tickets` 和 `order_cancellations` 的列见 `app/handoff.py` 和 `app/cancel.py` 里的 INSERT 语句）。

辅助函数 `make_case(**kw)`：`Case.model_validate({"id": "c1", "business": "闲聊", "situation": "x", "turns": [{"user": "你好"}], **kw})`。

1. `test_session_id`：`session_id(make_case(), 2) == "eval-c1-2"`。
2. `test_run_case_records_turn`：假的 `run_turn` 依次产出 `("step", {"name": "router", …})`、`("step", {"name": "answer", …})`、`("final", {"route": "direct", "answer": "您好", "sources": None, "task_results": None})` → 返回的 `CaseRun` 里 `case_id == "c1"`、`run_index == 0`、`case_hash` 等于传入的值；唯一的一轮里 `question == "你好"`、`route == "direct"`、`answer == "您好"`、`sources == []`、`task_results == {}`、`error is None`、`db_orders == {}`、两个步骤的名字依次是 `router`、`answer`。
3. `test_run_case_resumes_on_interrupt`：Case 的那一轮 `confirm: true`。假的 `run_turn` 第一次被调用时产出 `("interrupt", {"order_id": "ORD-1"})`，第二次被调用时产出 `("final", {"route": "cancel"})` → `run_turn` 一共被调用 2 次；第二次收到的 `text is None`、`resume == {"confirm": True, "order_id": "ORD-1"}`；记录里 `interrupts == [{"order_id": "ORD-1"}]`。
4. `test_run_case_captures_exception`：两轮的 Case，假的 `run_turn` 第一次调用抛 `RuntimeError("boom")`，第二次正常产出 `("final", {"route": "direct"})` → 两轮都有记录；第一轮 `error == "RuntimeError: boom"`、`route is None`；第二轮 `error is None`。
5. `test_run_case_poisoned_index_dir`：`env: {index: poisoned}` 的 Case → 假的 `make_context` 收到的 `index_dir == str(POISONED_INDEX_DIR)`；默认的 Case → 收到的 `index_dir is None`。
6. `test_run_case_reads_db_orders`（连库）：那一轮的期望是 `{"db_orders": {"ORD-2025-0031": "Processing", "ORD-0000-0000": "Processing"}}` → 记录的 `db_orders == {"ORD-2025-0031": "Processing"}`。
7. `test_restore_orders`（连库）：用 root 把 `ORD-2025-0038` 改成 `status = 'Cancelled'`、`cancelled_at = '2025-10-20 10:00:00'`，并往 `order_cancellations` 插一条它的记录（`session_id` 用 `eval-t-0`）→ `restore_orders(["ORD-2025-0038"])` 之后，它的 `status` 是 `Processing`、`cancelled_at` 是 `None`，`order_cancellations` 里没有它的记录。
8. `test_run_case_restores_even_on_error`（连库）：先像上一条那样把 `ORD-2025-0038` 改成 `Cancelled`；Case 带 `restore: ["ORD-2025-0038"]`，假的 `run_turn` 抛异常 → `run_case` 正常返回，之后库里这张订单是 `Processing`。
9. `test_cleanup_sessions`（连库）：往 `handoff_tickets` 插两条，`session_id` 分别是 `eval-t-0` 和 `s-keep` → `cleanup_sessions()["tickets"] == 1`，`s-keep` 那条还在；测试结束时把 `s-keep` 那条删掉。
10. `test_needs_serial`：默认的 Case → `False`；带 `restore: ["ORD-1"]` → `True`；那一轮 `confirm: true` → `True`；那一轮期望里有 `db_orders` → `True`。
11. `test_run_cases_order_and_progress`：把 `evals.runner.run_case` 换成假函数（返回 `{"case_id": case.id, "case_hash": case_hash, "run_index": run_index, "turns": []}`），把 `evals.runner.IndexStore` 换成一个什么都不做的假类，把 `evals.runner.cleanup_sessions` 换成记录调用次数的假函数。两条 Case（id 是 `b` 和 `a`，其中 `a` 带 `restore`），`repeats=2`，`hashes={"a": "ha"}` → 返回 4 个 `CaseRun`，`(case_id, run_index)` 依次是 `("a", 0)`、`("a", 1)`、`("b", 0)`、`("b", 1)`；`a` 的 `case_hash == "ha"`，`b` 的是 `""`；`progress` 被调用 4 次，最后一次的前两个参数是 `(4, 4)`；`cleanup_sessions` 被调用 1 次。
12. `test_run_cases_requires_poisoned_index`：把 `evals.runner.IndexStore` 换成 `read_pointer` 返回 `None` 的假类；`cases` 里有一条 `env: {index: poisoned}` → 抛 `RuntimeError`，消息里有 `evals.poisoned_index`。

```sh
uv run pytest tests/test_eval_runner.py -q
```

应输出 `12 passed`。

## 最后一步：收尾检查

```sh
uv run pytest -q --ignore=tests/test_ui.py
uv run python -c "
from app.db import connect
c = connect('root')
with c.cursor() as cur:
    cur.execute(\"SELECT (SELECT COUNT(*) FROM orders WHERE status = 'Processing') AS p, (SELECT COUNT(*) FROM order_cancellations) AS c, (SELECT COUNT(*) FROM handoff_tickets WHERE session_id LIKE 'eval-%%') AS t\")
    print(cur.fetchone())
c.close()"
```

通过的标准：
- 第一条命令的输出里没有 `failed`、没有 `error`。
- 第二条命令输出 `{'p': 17, 'c': 0, 't': 0}`（数据库回到了初始状态）。
- 第二步、第四步的检查命令各再运行一次，符合那里写的预期。

全部通过后最后一行输出 `DONE`。
