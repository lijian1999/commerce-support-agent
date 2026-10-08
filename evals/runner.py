"""评测的运行器：把一条 Case 交给 Agent 跑，记下每一轮的记录；跑完复原数据库。"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from langgraph.checkpoint.memory import InMemorySaver

from app.db import connect
from app.graph import build_graph
from app.rag.index import IndexStore
from app.runner import make_context, run_turn
from evals.schema import POISONED_INDEX_DIR, SESSION_PREFIX, Case, CaseRun, TurnRecord

# 评测会话在两张记录表里的前缀，清理时按它删
_PREFIX = SESSION_PREFIX + "%"


def read_order_status(order_ids: list[str]) -> dict[str, str]:
    """读这些订单现在的状态。库里没有的订单不出现在结果里。"""
    if not order_ids:
        return {}
    conn = connect("root")
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT order_id, status FROM orders WHERE order_id IN ({', '.join(['%s'] * len(order_ids))})",
                list(order_ids),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return {row["order_id"]: row["status"] for row in rows}


def restore_orders(order_ids: list[str]) -> None:
    """把这些订单改回处理中，并删掉它们的取消记录。"""
    if not order_ids:
        return
    placeholders = ", ".join(["%s"] * len(order_ids))
    conn = connect("root")
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE orders SET status = %s, cancelled_at = NULL WHERE order_id IN ({placeholders})",
                ["Processing", *order_ids],
            )
            cur.execute(f"DELETE FROM order_cancellations WHERE order_id IN ({placeholders})",
                        list(order_ids))
        conn.commit()
    finally:
        conn.close()


def cleanup_sessions() -> dict:
    """删掉这次评测留下的取消记录和工单，按会话编号的前缀找。"""
    conn = connect("root")
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM order_cancellations WHERE session_id LIKE %s", (_PREFIX,))
            cancellations = cur.rowcount
            cur.execute("DELETE FROM handoff_tickets WHERE session_id LIKE %s", (_PREFIX,))
            tickets = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return {"cancellations": cancellations, "tickets": tickets}


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


def needs_serial(case: Case) -> bool:
    """这条 Case 会改库或要读库里的订单状态，不能和别的 Case 同时跑。"""
    if case.restore:
        return True
    return any(turn.confirm or turn.expect.db_orders for turn in case.turns)


def run_cases(cases: list[Case], repeats: int = 3, workers: int = 4,
              hashes: dict[str, str] | None = None,
              progress: Callable[[int, int, CaseRun], None] | None = None) -> list[CaseRun]:
    """每条 Case 跑 repeats 次。不改库的并行跑，改库的和需要读库状态的排到最后串行跑。"""
    # Chroma 客户端第一次创建不是线程安全的，先在主线程建好
    IndexStore()
    if any(case.env.index == "poisoned" for case in cases):
        store = IndexStore(POISONED_INDEX_DIR)
        if not store.read_pointer():
            raise RuntimeError("带诱导文档的索引还没有构建，先运行 uv run python -m evals.poisoned_index")

    jobs: list[tuple[Case, int]] = [(case, run_index) for case in cases for run_index in range(repeats)]
    total = len(jobs)
    runs: list[CaseRun] = []
    done = 0
    lock = threading.Lock()

    def finish(case_run: CaseRun) -> None:
        nonlocal done
        with lock:
            done += 1
            finished = done
            runs.append(case_run)
        if progress:
            progress(finished, total, case_run)

    def worker(job: tuple[Case, int]) -> CaseRun:
        case, run_index = job
        return run_case(case, run_index, (hashes or {}).get(case.id, ""))

    parallel = [job for job in jobs if not needs_serial(job[0])]
    serial = [job for job in jobs if needs_serial(job[0])]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker, job) for job in parallel]
        for future in futures:
            finish(future.result())
    for job in serial:
        finish(worker(job))

    cleanup_sessions()
    return sorted(runs, key=lambda run: (run["case_id"], run["run_index"]))
