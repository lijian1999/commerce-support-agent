"""评测里由代码判定的检查：一轮的结局、逐项检查、汇总。全是纯函数，不连数据库、不调模型。"""
import fnmatch
import re
from typing import Any

from evals.schema import CheckResult, STAGES, TurnExpect, TurnRecord, make_check

# execute_sql 的步骤名（子任务编号 + execute），用于数 SQL 的执行次数
_EXECUTE_RE = re.compile(r"^t\d+/execute$")
# 千分位逗号：两边都是数字时才去掉，如 1,199.00 → 1199.00
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d)")


def turn_outcome(record: TurnRecord) -> str:
    """按 docs/eval-contracts.md 第 4 节的规则推出这一轮的结局，命中第一条就返回。"""
    if record.get("error"):
        return "error"
    cancel = record.get("cancel")
    if isinstance(cancel, dict) and cancel.get("outcome"):
        return f"cancel:{cancel['outcome']}"
    if record.get("handoff"):
        return "handoff"
    task_results = record.get("task_results") or {}
    if not task_results:
        return "answered"
    statuses = [t.get("status") for t in task_results.values()]
    if all(s == "ok" for s in statuses):
        return "answered"
    if any(s == "ok" for s in statuses):
        return "partial"
    if any(t.get("error_kind") == "login_required" for t in task_results.values()):
        return "login_required"
    return "declined"


def turn_passed(results: list[CheckResult]) -> bool:
    """gating 为真的检查里没有失败的，这一轮就算通过。"""
    return not any(r["gating"] and r["passed"] is False for r in results)


def first_failed_stage(results: list[CheckResult]) -> str | None:
    """gating 为真且失败的检查里，环节在 STAGES 中最靠前的那个。"""
    failed = [r["stage"] for r in results if r["gating"] and r["passed"] is False]
    if not failed:
        return None
    return min(failed, key=STAGES.index)


def redline_violations(results: list[CheckResult]) -> list[CheckResult]:
    """红线违反：redline 为真且失败的结果。"""
    return [r for r in results if r["redline"] and r["passed"] is False]


# ---- 从记录里取数的几个办法 ----

def _sql_tasks(record: TurnRecord) -> list[dict]:
    """查库任务的结果。"""
    return [t for t in (record.get("task_results") or {}).values() if t.get("agent") == "sql"]


def _rag_tasks(record: TurnRecord) -> list[dict]:
    """成功的检索任务，每个的 data["chunks"] 按名次排列。"""
    return [t for t in (record.get("task_results") or {}).values()
            if t.get("agent") == "rag" and t.get("status") == "ok"]


def _sql_exec_count(record: TurnRecord) -> int:
    """SQL 执行次数：t<n>/execute 的步骤数，cancel/execute 不算。"""
    return sum(1 for s in record.get("steps") or [] if _EXECUTE_RE.match(s.get("name", "")))


def _model_steps(record: TurnRecord) -> int:
    """有模型参与的步骤数。"""
    return sum(1 for s in record.get("steps") or [] if s.get("kind") == "model")


def _router_reason_len(record: TurnRecord) -> int:
    """路由理由的字数，取不到记 0。"""
    for s in record.get("steps") or []:
        if s.get("name") == "router":
            return len((s.get("detail") or {}).get("reason") or "")
    return 0


def _normalize(text: str) -> str:
    """字面比较前的规整：转小写、去掉千分位逗号、去掉加粗标记。"""
    return _THOUSANDS_RE.sub("", text.lower()).replace("**", "")


# ---- 固定产出 ----

def _check_run_error(record: TurnRecord) -> CheckResult:
    error = record.get("error")
    return make_check("run_error", "turn", not error, detail=error or "")


def _check_sql_bound(record: TurnRecord) -> CheckResult:
    sql, sql_tasks = _sql_exec_count(record), len(_sql_tasks(record))
    return make_check("sql_bound", "turn", sql <= sql_tasks,
                      detail=f"SQL 执行 {sql} 次，查库任务 {sql_tasks} 个",
                      data={"sql": sql, "sql_tasks": sql_tasks})


def _check_cost(record: TurnRecord) -> CheckResult:
    data = {"model_steps": _model_steps(record), "sql": _sql_exec_count(record),
            "seconds": record.get("seconds"), "router_reason_len": _router_reason_len(record)}
    return make_check("cost", "turn", None, gating=False, data=data)


# ---- 按期望产出 ----

def _check_route(expect: TurnExpect, record: TurnRecord) -> CheckResult:
    assert expect.route is not None
    actual, prefer = record.get("route"), expect.route.prefer
    accepted = actual == prefer or actual in expect.route.accept
    return make_check("route", "router", accepted, detail=f"路由 {actual}，首选 {prefer}",
                      data={"route": actual, "prefer": prefer, "prefer_hit": actual == prefer})


def _check_outcome(expect: TurnExpect, record: TurnRecord) -> CheckResult:
    assert expect.outcome is not None
    actual = turn_outcome(record)
    wanted = expect.outcome if isinstance(expect.outcome, list) else [expect.outcome]
    return make_check("outcome", "turn", actual in wanted, detail=f"结局 {actual}",
                      data={"outcome": actual})


def _check_handoff_reason(expect: TurnExpect, record: TurnRecord) -> CheckResult:
    assert expect.handoff_reason is not None
    handoff = record.get("handoff")
    actual = handoff.get("reason") if isinstance(handoff, dict) else None
    return make_check("handoff_reason", "turn", actual == expect.handoff_reason,
                      detail=f"转人工原因 {actual}", data={"reason": actual})


def _check_plan(expect: TurnExpect, record: TurnRecord) -> CheckResult:
    assert expect.plan is not None
    plan = record.get("plan")
    if not plan:
        return make_check("plan", "plan", None, detail="没有走 Supervisor", data={"tasks": 0, "extra_tasks": 0})
    tasks = plan.get("tasks") or []
    agents = [t.get("agent") for t in tasks]
    problems: list[str] = []

    # agents：每种类型的任务数不少于期望里该类型出现的次数
    wanted: dict[str, int] = {}
    for agent in expect.plan.agents:
        wanted[agent] = wanted.get(agent, 0) + 1
    for agent, need in sorted(wanted.items()):
        have = sum(1 for a in agents if a == agent)
        if have < need:
            problems.append(f"类型 {agent} 需要 {need} 个，实际 {have} 个")

    # depends：每个 [a, b] 要有类型为 a 的任务依赖某个类型为 b 的任务
    by_type: dict[str, set[str]] = {}
    for t in tasks:
        by_type.setdefault(t.get("agent"), set()).add(t.get("id"))
    for a, b in expect.plan.depends:
        ids_b = by_type.get(b, set())
        ok = any(b_id in (t.get("depends_on") or []) for t in tasks
                 if t.get("agent") == a for b_id in ids_b)
        if not ok:
            problems.append(f"缺少依赖 {a} -> {b}")

    extra = 0
    if expect.plan.max_tasks is not None:
        extra = max(0, len(tasks) - expect.plan.max_tasks)
        if extra:
            problems.append(f"任务数 {len(tasks)} 超过上限 {expect.plan.max_tasks}")

    return make_check("plan", "plan", not problems, detail="；".join(problems),
                      data={"tasks": len(tasks), "extra_tasks": extra})


def _check_sql_result(expect: TurnExpect, record: TurnRecord) -> CheckResult:
    assert expect.sql is not None
    tasks = _sql_tasks(record)
    if not tasks:
        return make_check("sql_result", "sql", False, detail="没有查库任务", data={"result": None})
    if all(t.get("status") == "ok" for t in tasks):
        actual = "ok"
    elif any(t.get("error_kind") == "login_required" for t in tasks):
        actual = "login_required"
    elif any(t.get("error_kind") == "unanswerable" for t in tasks):
        actual = "unanswerable"
    else:
        actual = "failed"
    wanted = expect.sql.result if isinstance(expect.sql.result, list) else [expect.sql.result]
    return make_check("sql_result", "sql", actual in wanted, detail=f"查库结局 {actual}",
                      data={"result": actual})


def _check_sql_rows(expect: TurnExpect, record: TurnRecord) -> CheckResult | None:
    assert expect.sql is not None
    if not (expect.sql.rows_contain or expect.sql.rows_exclude or expect.sql.row_count is not None):
        return None
    ok_tasks = [t for t in _sql_tasks(record) if t.get("status") == "ok"]
    if not ok_tasks:
        return make_check("sql_rows", "sql", None, detail="没有成功的查库结果",
                          data={"missing": [], "forbidden": [], "rows": 0})
    rows = [row for t in ok_tasks for row in (t.get("data") or {}).get("rows") or []]
    cells = [value for row in rows for value in row.values()]
    missing: list[Any] = []
    for want in expect.sql.rows_contain:
        if isinstance(want, bool):
            found = any(c is want for c in cells)
        elif isinstance(want, (int, float)):
            found = any(isinstance(c, (int, float)) and not isinstance(c, bool) and abs(c - want) < 0.005 for c in cells)
        else:
            needle = _normalize(want)
            found = any(isinstance(c, str) and needle in _normalize(c) for c in cells)
        if not found:
            missing.append(want)
    forbidden: list[Any] = []
    for bad in expect.sql.rows_exclude:
        if any(_normalize(str(c)).find(_normalize(bad)) >= 0 for c in cells):
            forbidden.append(bad)
    count_ok = expect.sql.row_count is None or len(rows) == expect.sql.row_count
    problems = missing + forbidden + ([] if count_ok else [f"行数 {len(rows)} != {expect.sql.row_count}"])
    detail = f"缺 {missing}" if missing else ""
    if forbidden:
        detail = (detail + "；").strip("；") + f"出现禁止值 {forbidden}"
    if not count_ok:
        detail = (detail + "；").strip("；") + f"行数 {len(rows)}"
    return make_check("sql_rows", "sql", not problems, detail=detail or "行内容符合期望",
                      data={"missing": missing, "forbidden": forbidden, "rows": len(rows)})


def _check_retrieval(expect: TurnExpect, record: TurnRecord) -> CheckResult:
    assert expect.retrieval is not None
    tasks = _rag_tasks(record)
    pre = expect.retrieval.precondition
    if not tasks:
        return make_check("retrieval", "retrieval", False, detail="没有成功的检索",
                          data={"ranks": [], "best_rank": None, "recall": 0.0, "precondition": pre})
    ranks: list[int | None] = []
    for item in expect.retrieval.relevant:
        best: int | None = None
        for task in tasks:
            for rank, chunk in enumerate((task.get("data") or {}).get("chunks") or [], start=1):
                if chunk.get("doc") == item.doc and (item.section is None or chunk.get("section") == item.section):
                    best = rank if best is None else min(best, rank)
                    break
        ranks.append(best)
    hit = sum(1 for r in ranks if r is not None)
    total = len(ranks)
    if expect.retrieval.mode == "all":
        ok = hit == total
    else:
        ok = hit > 0
    best_rank = min((r for r in ranks if r is not None), default=None)
    return make_check("retrieval", "retrieval", ok, detail=f"名次 {ranks}",
                      data={"ranks": ranks, "best_rank": best_rank,
                            "recall": (hit / total) if total else 0.0, "precondition": pre})


def _check_answer(expect: TurnExpect, record: TurnRecord) -> list[CheckResult]:
    answer = _normalize(record.get("answer") or "")
    out: list[CheckResult] = []
    if expect.answer.literals:
        missing = [x for x in expect.answer.literals if _normalize(x) not in answer]
        out.append(make_check("answer_literals", "answer", not missing,
                              detail=f"缺少 {missing}" if missing else "都出现了", data={"missing": missing}))
    if expect.answer.literals_exclude:
        found = [x for x in expect.answer.literals_exclude if _normalize(x) in answer]
        out.append(make_check("answer_literals_exclude", "answer", not found,
                              detail=f"出现了 {found}" if found else "都没出现", data={"found": found}))
    if expect.answer.redline_literals:
        found = [x for x in expect.answer.redline_literals if _normalize(x) in answer]
        out.append(make_check("answer_redline_literals", "answer", not found, redline=True,
                              detail=f"出现红线字面值 {found}" if found else "都没出现", data={"found": found}))
    return out


def _check_trace(expect: TurnExpect, record: TurnRecord) -> list[CheckResult]:
    assert expect.trace is not None
    names = [s.get("name", "") for s in record.get("steps") or []]
    out: list[CheckResult] = []
    if expect.trace.max_sql is not None:
        sql = _sql_exec_count(record)
        out.append(make_check("trace_sql", "turn", sql <= expect.trace.max_sql,
                              detail=f"SQL 执行 {sql} 次，上限 {expect.trace.max_sql}"))
    if expect.trace.max_model_steps is not None:
        steps = _model_steps(record)
        out.append(make_check("trace_model_steps", "turn", steps <= expect.trace.max_model_steps,
                              detail=f"模型步骤 {steps} 个，上限 {expect.trace.max_model_steps}"))
    if expect.trace.forbid_steps:
        found = [n for n in names if any(fnmatch.fnmatchcase(n, p) for p in expect.trace.forbid_steps)]
        out.append(make_check("trace_forbid", "turn", not found,
                              detail=f"出现了禁止的步骤 {found}" if found else "没有禁止的步骤",
                              data={"found": found}))
    if expect.trace.require_steps:
        missing = [p for p in expect.trace.require_steps
                   if not any(fnmatch.fnmatchcase(n, p) for n in names)]
        out.append(make_check("trace_require", "turn", not missing,
                              detail=f"缺少步骤 {missing}" if missing else "要求的步骤都有",
                              data={"missing": missing}))
    return out


def _check_db_orders(expect: TurnExpect, record: TurnRecord) -> CheckResult:
    actual = record.get("db_orders") or {}
    bad = {k: v for k, v in expect.db_orders.items() if actual.get(k) != v}
    return make_check("db_orders", "turn", not bad, redline=True,
                      detail=f"状态不符 {bad}" if bad else "订单状态都符合期望",
                      data={"actual": actual})


def check_turn(expect: TurnExpect, record: TurnRecord) -> list[CheckResult]:
    """跑完一轮后由代码逐项检查，产出 CheckResult 列表。"""
    results: list[CheckResult] = [_check_run_error(record), _check_sql_bound(record), _check_cost(record)]
    if expect.route is not None:
        results.append(_check_route(expect, record))
    if expect.outcome is not None:
        results.append(_check_outcome(expect, record))
    if expect.handoff_reason is not None:
        results.append(_check_handoff_reason(expect, record))
    if expect.plan is not None:
        results.append(_check_plan(expect, record))
    if expect.sql is not None:
        results.append(_check_sql_result(expect, record))
        rows = _check_sql_rows(expect, record)
        if rows is not None:
            results.append(rows)
    if expect.retrieval is not None:
        results.append(_check_retrieval(expect, record))
    if expect.answer is not None:
        results.extend(_check_answer(expect, record))
    if expect.trace is not None:
        results.extend(_check_trace(expect, record))
    if expect.db_orders:
        results.append(_check_db_orders(expect, record))
    return results
