"""把离线评测作为 LangSmith 的实验来跑。

Git 里的 Case 同步成 LangSmith 上的一个数据集；一次评测是这个数据集上的一个实验，
每条 Case 的每次运行是一条 Trace，检查结果作为这条 Trace 的评分挂上去。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from typing import Any, Callable, Optional

from langsmith import Client

from app.rag.index import IndexStore
from evals.manifest import case_hash
from evals.report import evaluate_run
from evals.runner import cleanup_sessions, needs_serial, run_case
from evals.schema import RUNS_DIR, Case, CaseRun

DATASET_NAME = "commerce-support-agent-evals"
# feedback 第 3 条里逐个统计的检查名，按这个顺序产出评分
CHECK_NAMES = ("route", "plan", "sql_result", "sql_rows", "retrieval", "answer_literals",
               "answer_facts", "answer_must_not", "answer_grounded", "outcome")


def example_id(case_id: str) -> uuid.UUID:
    """一条 Case 对应的样例编号：同一个 id 永远算出同一个编号。"""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"csa-case:{case_id}")


def example_payload(case: Case) -> dict:
    """把一条 Case 写成 LangSmith 样例的样子。"""
    return {
        "id": example_id(case.id),
        "inputs": {"case_id": case.id,
                   "turns": [turn.user for turn in case.turns],
                   "env": case.env.model_dump(mode="json")},
        "outputs": {"expect": [turn.expect.model_dump(mode="json", exclude_defaults=True)
                               for turn in case.turns]},
        "metadata": {"case_hash": case_hash(case), "business": case.business,
                     "situation": case.situation, "set": case.set, "split": case.split,
                     "source": case.source},
    }


def sync_dataset(client: Any, cases: list[Case]) -> dict:
    """把本地的 Case 同步到数据集：没有的建，改了的更新，没变的不动。本地删掉的不删。"""
    if client.has_dataset(dataset_name=DATASET_NAME):
        dataset = client.read_dataset(dataset_name=DATASET_NAME)
    else:
        dataset = client.create_dataset(
            dataset_name=DATASET_NAME,
            description="commerce-support-agent 的离线评测集，来源是 Git 里的 evals/cases/")
    existing = {str(example.id): example for example in client.list_examples(dataset_id=dataset.id)}

    created = updated = unchanged = 0
    new_payloads: list[dict] = []
    for case in cases:
        payload = example_payload(case)
        old = existing.get(str(payload["id"]))
        if old is None:
            new_payloads.append(payload)
            created += 1
        elif (old.metadata or {}).get("case_hash") != case_hash(case):
            client.update_example(example_id=payload["id"], inputs=payload["inputs"],
                                  outputs=payload["outputs"], metadata=payload["metadata"])
            updated += 1
        else:
            unchanged += 1
    if new_payloads:
        client.create_examples(dataset_id=dataset.id, examples=new_payloads)

    return {"dataset_id": str(dataset.id), "created": created, "updated": updated,
            "unchanged": unchanged}


def count_trace(run: Any) -> dict:
    """数一条 Trace 里 execute_sql 的运行数和 llm 的运行数（整棵树，含自己）。"""
    stack = [run]
    execute_sql = llm = 0
    while stack:
        node = stack.pop()
        if node is None:
            continue
        if getattr(node, "name", None) == "execute_sql":
            execute_sql += 1
        if getattr(node, "run_type", None) == "llm":
            llm += 1
        stack.extend(getattr(node, "child_runs", None) or [])
    return {"execute_sql": execute_sql, "llm": llm}


def feedback(result: dict, trace: dict) -> list[dict]:
    """一次运行的评估结果 → LangSmith 的评分列表。"""
    checks = [check for turn in (result.get("turns") or []) for check in (turn.get("checks") or [])]
    scores: list[dict] = [
        {"key": "passed", "score": 1 if result.get("passed") else 0,
         "comment": "" if result.get("passed") else f"第一处出错的环节: {result.get('first_failed_stage')}"},
        {"key": "redline_violations", "score": len(result.get("redlines") or []),
         "comment": "；".join(f"{item.get('check')}: {item.get('detail')}"
                              for item in result.get("redlines") or [])},
    ]
    for name in CHECK_NAMES:
        items = [check for check in checks
                 if check.get("check") == name and check.get("passed") is not None]
        if not items:
            continue
        ok = all(check.get("passed") for check in items)
        scores.append({"key": name, "score": 1 if ok else 0,
                       "comment": "；".join(check.get("detail") or "" for check in items
                                            if not check.get("passed"))})
    scores.append({"key": "trace_execute_sql", "score": trace["execute_sql"], "comment": ""})
    scores.append({"key": "trace_llm_calls", "score": trace["llm"], "comment": ""})
    return scores


def _with_retry(action: Callable[[], Any], attempts: int = 4, wait: float = 3.0) -> Any:
    """开跑之前的几次同步网络调用偶尔会因为连接问题失败，等一下再试。"""
    for attempt in range(attempts):
        try:
            return action()
        except Exception as exc:  # 只重试连接类的错误，其余原样抛出
            if "Connection" not in type(exc).__name__ or attempt == attempts - 1:
                raise
            time.sleep(wait * (attempt + 1))


def _partial_path(report_id: str):
    return RUNS_DIR / f"{report_id}.partial.jsonl"


def _append_partial(report_id: str, case_run: CaseRun) -> None:
    """每跑完一次就落盘一行：进程中途断了，已经跑完的记录还在。全部跑完后这个文件会被删掉。"""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    with _partial_path(report_id).open("a", encoding="utf-8") as f:
        f.write(json.dumps(case_run, ensure_ascii=False) + "\n")


def execute_langsmith(cases: list[Case], repeats: int, workers: int, judge: bool, votes: int,
                      report_id: str, state: dict,
                      client: Optional[Any] = None) -> tuple[list[CaseRun], list[dict], dict]:
    """把一批 Case 在 LangSmith 上跑成一个实验，返回运行记录、评估结果和实验信息。"""
    client = client or Client()
    synced = _with_retry(lambda: sync_dataset(client, cases))
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
            _append_partial(report_id, case_run)
        return case_run

    def evaluator(run, example):
        case = by_id[example.inputs["case_id"]]
        result = evaluate_run(case, run.outputs, judge, votes)
        with lock:
            results.append(result)
        return {"results": feedback(result, count_trace(run))}

    examples = _with_retry(lambda: list(client.list_examples(dataset_id=synced["dataset_id"],
                                         example_ids=[example_id(case.id) for case in cases])))
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
    _partial_path(report_id).unlink(missing_ok=True)
    key: Callable[[dict], tuple] = lambda item: (item["case_id"], item["run_index"])
    info = {"experiment": experiment.experiment_name, "url": getattr(experiment, "url", None) or "",
            "rows": len(rows), **synced}
    return sorted(runs, key=key), sorted(results, key=key), info
