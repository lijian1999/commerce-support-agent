# 任务 23：把评测作为 LangSmith 的实验来跑

先读 `AGENTS.md`，再读接口约定 `docs/eval-contracts.md` 和数据结构 `evals/schema.py`。本任务新建这些文件：`evals/langsmith_run.py`、`tests/test_eval_langsmith.py`。不要改别的文件。

## 背景

Case 的唯一来源是 Git 里的文件。本任务把它们同步成 LangSmith 上的一个数据集，再把一次评测跑成这个数据集上的一个实验：每条 Case 的每次运行是一条 Trace，检查结果作为这条 Trace 的评分挂上去，在 LangSmith 的界面里能逐条点开看。

已有的部件：`evals.runner.run_case(case, run_index, case_hash)` 跑一条 Case；`evals.runner.needs_serial(case)` 判断它是否会改库或读订单状态；`evals.runner.cleanup_sessions()` 清理评测留下的工单；`evals.report.evaluate_run(case, case_run, judge, votes)` 评一次运行，返回 RunResult；`evals.manifest.case_hash(case)`。

LangSmith 的客户端（`langsmith` 0.14）：`Client()` 从环境变量里读配置，这些环境变量在导入 `app.config` 时已经从 `.env` 加载。下面用到的调用方式都已经对着真实服务验证过，照着写。

## 第一步：同步数据集

常量 `DATASET_NAME = "commerce-support-agent-evals"`。

- `example_id(case_id: str) -> uuid.UUID`：`uuid.uuid5(uuid.NAMESPACE_URL, f"csa-case:{case_id}")`。同一条 Case 永远对应同一个样例。
- `example_payload(case: Case) -> dict`：返回 `{"id": example_id(case.id), "inputs": {"case_id": case.id, "turns": [每一轮的 user], "env": case.env.model_dump(mode="json")}, "outputs": {"expect": [每一轮的 expect.model_dump(mode="json", exclude_defaults=True)]}, "metadata": {"case_hash": case_hash(case), "business": case.business, "situation": case.situation, "set": case.set, "split": case.split, "source": case.source}}`。
- `sync_dataset(client, cases: list[Case]) -> dict`：
  1. `client.has_dataset(dataset_name=DATASET_NAME)` 为假时 `client.create_dataset(dataset_name=DATASET_NAME, description="commerce-support-agent 的离线评测集，来源是 Git 里的 evals/cases/")`；否则 `client.read_dataset(dataset_name=DATASET_NAME)`。两者都返回带 `id` 的数据集对象。
  2. `existing = {str(e.id): e for e in client.list_examples(dataset_id=dataset.id)}`。
  3. 对每条 Case：样例不存在 → 归入「新增」；存在但 `(e.metadata or {}).get("case_hash")` 和现在的 `case_hash` 不同 → 归入「更新」；相同 → 不动。
  4. 新增的一次性创建：`client.create_examples(dataset_id=dataset.id, examples=[payload, …])`（列表为空时不调用）。
  5. 更新的逐条：`client.update_example(example_id=payload["id"], inputs=payload["inputs"], outputs=payload["outputs"], metadata=payload["metadata"])`。
  6. 返回 `{"dataset_id": str(dataset.id), "created": 新增数, "updated": 更新数, "unchanged": 不动的个数}`。数据集里多出来的样例（本地已经没有的 Case）不删。

## 第二步：一个只看 Trace 的评分

`count_trace(run) -> dict`：`run` 是 LangSmith 交给 evaluator 的运行树，子运行在 `run.child_runs` 里（可能是 `None`），逐层往下。遍历整棵树（含 `run` 自己），返回 `{"execute_sql": 名字是 execute_sql 的运行数, "llm": run_type 是 llm 的运行数}`。

## 第三步：`execute_langsmith`

`execute_langsmith(cases: list[Case], repeats: int, workers: int, judge: bool, votes: int, report_id: str, state: dict, client=None) -> tuple[list[CaseRun], list[dict], dict]`。`client` 为 `None` 时用 `langsmith.Client()`。

```python
def execute_langsmith(cases, repeats, workers, judge, votes, report_id, state, client=None):
    client = client or Client()
    synced = sync_dataset(client, cases)
    by_id = {case.id: case for case in cases}
    IndexStore()                          # Chroma 的客户端先在主线程建一次
    counters: dict[str, int] = {}         # 每条 Case 已经分出去几个 run_index
    lock = threading.Lock()               # 保护 counters、runs、results
    serial_lock = threading.Lock()        # 会改库或读订单状态的 Case 一次只跑一条
    runs: list[CaseRun] = []
    results: list[dict] = []

    def target(inputs: dict) -> dict:
        case = by_id[inputs["case_id"]]
        with lock:
            run_index = counters.get(case.id, 0)
            counters[case.id] = run_index + 1
        if needs_serial(case):
            with serial_lock:
                case_run = run_case(case, run_index, case_hash(case))
        else:
            case_run = run_case(case, run_index, case_hash(case))
        with lock:
            runs.append(case_run)
        return case_run

    def evaluator(run, example):
        case = by_id[example.inputs["case_id"]]
        result = evaluate_run(case, run.outputs, judge, votes)
        with lock:
            results.append(result)
        return {"results": feedback(result, count_trace(run))}

    examples = list(client.list_examples(dataset_id=synced["dataset_id"],
                                         example_ids=[example_id(case.id) for case in cases]))
    experiment = client.evaluate(
        target, data=examples, evaluators=[evaluator],
        experiment_prefix=f"eval-{report_id}", num_repetitions=repeats, max_concurrency=workers,
        metadata={"report_id": report_id, "eval_set_version": state["eval_set_version"],
                  "git_version": state["agent"]["git"], "models_hash": state["agent"]["models_hash"],
                  "index_version": state["agent"]["index_version"], "index_hash": state["agent"]["index_hash"],
                  "db_hash": state["env"]["db_hash"], "evaluator_version": state["evaluator"]["version"]},
    )
    rows = list(experiment)               # 等全部跑完
    cleanup_sessions()
    key = lambda item: (item["case_id"], item["run_index"])
    info = {"experiment": experiment.experiment_name, "url": getattr(experiment, "url", None) or "",
            "rows": len(rows), **synced}
    return sorted(runs, key=key), sorted(results, key=key), info
```

上面的代码原样使用，补上 import。再实现它用到的 `feedback`：

`feedback(result: dict, trace: dict) -> list[dict]`，把一次运行的评估结果变成 LangSmith 的评分列表，每项是 `{"key": 名字, "score": 分数, "comment": 说明}`，按这个顺序：

1. `{"key": "passed", "score": 1 或 0, "comment": 没通过时写 f"第一处出错的环节: {first_failed_stage}"，通过时写空串}`。
2. `{"key": "redline_violations", "score": len(result["redlines"]), "comment": 各条的 f"{check}: {detail}" 用 ；连接}`。
3. 对 `("route", "plan", "sql_result", "sql_rows", "retrieval", "answer_literals", "answer_facts", "answer_must_not", "answer_grounded", "outcome")` 里的每个检查名：把各轮里这个名字且 `passed` 不是 `None` 的检查收集起来；一个都没有就不产出这一项；否则产出 `{"key": 检查名, "score": 全部 passed 为真是 1 否则 0, "comment": 没通过的那些检查的 detail 用 ；连接}`。
4. `{"key": "trace_execute_sql", "score": trace["execute_sql"], "comment": ""}`。
5. `{"key": "trace_llm_calls", "score": trace["llm"], "comment": ""}`。

## 第四步：测试

`tests/test_eval_langsmith.py`，不连 LangSmith、不调模型。写一个假客户端 `FakeClient`：内存里存样例（每个样例是 `types.SimpleNamespace(id, inputs, outputs, metadata)`），实现 `has_dataset`、`create_dataset`、`read_dataset`（返回 `SimpleNamespace(id="ds1")`）、`list_examples(dataset_id, example_ids=None)`（给了 `example_ids` 时只返回这些）、`create_examples(dataset_id, examples)`、`update_example(example_id, inputs, outputs, metadata)`，并记录每个方法被调用的次数；`evaluate(target, data, evaluators, num_repetitions, **kw)`：对 `data` 里的每个样例重复 `num_repetitions` 次：`outputs = target(example.inputs)`，再用 `SimpleNamespace(outputs=outputs, child_runs=None)` 作为运行调用每个 evaluator，把它的返回值收集起来；返回 `SimpleNamespace(experiment_name="exp-1", url="http://x", __iter__=…)`（能被 `list()` 遍历出每次运行一行即可，可以用一个带 `__iter__` 的小类）。

辅助 `make_case(case_id, **kw)` 同其他测试：`Case.model_validate({"id": case_id, "business": "闲聊", "situation": "x", "turns": [{"user": "你好"}], **kw})`。

1. `test_example_id_is_stable`：同一个 id 两次相同，不同 id 不同，类型是 `uuid.UUID`。
2. `test_example_payload`：一条两轮的 Case（第二轮带期望 `{"route": {"prefer": "sql"}}`）→ `inputs["turns"]` 是两句话的列表，`inputs["case_id"]` 正确，`outputs["expect"]` 长度 2 且第二项是 `{"route": {"prefer": "sql"}}`，`metadata["case_hash"] == case_hash(case)`。
3. `test_sync_creates_then_unchanged`：空的假客户端同步两条 Case → `created == 2`、`updated == 0`；再同步一次 → `created == 0`、`updated == 0`、`unchanged == 2`，`create_examples` 总共只被调用 1 次。
4. `test_sync_updates_changed_case`：同步 `make_case("a")` 后，再同步内容变了的 `make_case("a", note="改了")` → `updated == 1`，假客户端里这个样例的 `metadata["case_hash"]` 等于新的 hash。
5. `test_count_trace`：手工搭一棵三层的运行树（用 `SimpleNamespace(name, run_type, child_runs)`），里面有 2 个名字是 `execute_sql` 的、3 个 `run_type` 是 `llm` 的，叶子节点的 `child_runs` 是 `None` → `{"execute_sql": 2, "llm": 3}`。
6. `test_feedback_pass`：一个通过的 RunResult，一轮里有 `make_check("route", "router", True)` 和 `make_check("cost", "turn", None, gating=False)`；`trace = {"execute_sql": 1, "llm": 3}` → 返回的 `key` 依次是 `passed`、`redline_violations`、`route`、`trace_execute_sql`、`trace_llm_calls`；`passed` 的分数是 1，`trace_llm_calls` 的分数是 3。
7. `test_feedback_fail_and_redline`：一个没通过的 RunResult（`first_failed_stage == "sql"`，`redlines` 里有一项 `{"turn": 0, "check": "db_orders", "detail": "d"}`），两轮里各有一个 `sql_rows` 检查，一个通过、一个没通过（`detail="缺 x"`）→ `passed` 的分数是 0、说明是 `第一处出错的环节: sql`；`redline_violations` 的分数是 1、说明是 `db_orders: d`；`sql_rows` 的分数是 0、说明是 `缺 x`。
8. `test_execute_langsmith_assigns_run_index`：`monkeypatch` 把 `evals.langsmith_run.run_case` 换成返回 `{"case_id": case.id, "case_hash": case_hash, "run_index": run_index, "turns": []}` 的假函数，`evaluate_run` 换成返回 `{"case_id": case.id, "run_index": case_run["run_index"], "passed": True, "invalid": False, "first_failed_stage": None, "redlines": [], "turns": []}` 的假函数，`IndexStore` 和 `cleanup_sessions` 换成什么都不做的假实现。两条 Case、`repeats=2`、传入假客户端 → 返回的 `runs` 有 4 个，`(case_id, run_index)` 依次是 `("a", 0)`、`("a", 1)`、`("b", 0)`、`("b", 1)`；`results` 也是 4 个且顺序相同；`info["experiment"] == "exp-1"`、`info["url"] == "http://x"`、`info["rows"] == 4`、`info["created"] == 2`。

```sh
uv run pytest tests/test_eval_langsmith.py -q
```

应输出 `8 passed`。

## 最后一步：收尾检查

```sh
uv run pytest -q --ignore=tests/test_ui.py
uv run python -c "
from app import config
from evals.schema import load_cases
from evals.manifest import current_state
from evals.langsmith_run import execute_langsmith
cases = [c for c in load_cases() if c.id in ('ord-spend-keyboards', 'chat-hello')]
runs, results, info = execute_langsmith(cases, 1, 2, False, 1, 'smoke', current_state(load_cases()))
print(len(runs), len(results), [r['passed'] for r in results], info['rows'], info['experiment'].startswith('eval-smoke'))"
```

通过的标准：
- 第一条命令的输出里没有 `failed`、没有 `error`。
- 第二条命令（会连 LangSmith、调用真实模型）的最后一行是 `2 2 [True, True] 2 True`。

全部通过后最后一行输出 `DONE`。
