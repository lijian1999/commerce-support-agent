"""评测结果的评估、汇总和报告文本。

`evaluate_run` 把一次运行的检查合成 RunResult（judge=True 时会经 judges 调模型），
`summarize` 把全部 RunResult 汇总成 Summary，`render_text` 把报告渲染成人看的纯文本。
"""
from collections import Counter
from typing import Any

from evals.checks import (check_turn, first_failed_stage, redline_violations, turn_outcome,
                          turn_passed)
from evals.judges import judge_turn
from evals.schema import Case, CaseRun, STAGES, make_check

# 判断「上游正确」时看的检查：这几项都没失败，才说明问题出在回答环节
UPSTREAM_CHECKS = ("sql_result", "sql_rows", "retrieval")


def _ratio(hit: int, total: int) -> float | None:
    """比率，分母为 0 时记 None。"""
    return None if not total else round(hit / total, 4)


def _mean(values: list[float], digits: int = 4) -> float | None:
    """平均值，没有数据时记 None。"""
    return None if not values else round(sum(values) / len(values), digits)


def _data(check: dict) -> dict[str, Any]:
    return check.get("data") or {}


def _all_turns(results: list[dict]) -> list[dict]:
    """全部 RunResult 里的所有轮。"""
    return [turn for run in results for turn in (run.get("turns") or [])]


def _checks(turns: list[dict], name: str) -> list[tuple[dict, dict]]:
    """指定名字的检查，连同它所在的轮。"""
    return [(turn, check) for turn in turns for check in (turn.get("checks") or [])
            if check.get("check") == name]


def _meta_num(turn: dict, key: str) -> int:
    """轮计数里的整数项，缺失记 0。"""
    value = (turn.get("meta") or {}).get(key)
    return value if isinstance(value, int) and value > 0 else 0


# ---- 第一步：一次运行的评估结果 ----

def _turn_meta(record: dict) -> dict:
    """一轮的计数：planner 的步骤数、查库任务数、一次就编译通过的查库任务数。"""
    steps = record.get("steps") or []
    planner_steps = sum(1 for step in steps if step.get("name") == "planner")
    sql_ids = [task_id for task_id, task in (record.get("task_results") or {}).items()
               if task.get("agent") == "sql"]
    first_try = sum(1 for task_id in sql_ids
                    if sum(1 for step in steps if step.get("name") == f"{task_id}/sql_plan") == 1)
    return {"planner_steps": planner_steps, "sql_tasks": len(sql_ids), "sql_tasks_first_try": first_try}


def evaluate_run(case: Case, case_run: CaseRun, judge: bool = True, votes: int = 3) -> dict:
    """一次运行的评估结果（RunResult），字段见 docs/eval-contracts.md 第 8 节。"""
    records = case_run.get("turns") or []
    turns: list[dict] = []
    redlines: list[dict] = []
    invalid = False

    for index, turn in enumerate(case.turns):
        if index < len(records):
            record = records[index]
            checks = check_turn(turn.expect, record)
            if judge:
                checks = checks + judge_turn(turn.expect, record, votes)
            outcome, question = turn_outcome(record), record["question"]
            meta = _turn_meta(record)
        else:
            checks = [make_check("run_error", "turn", False, "这一轮没有运行记录")]
            outcome, question = "error", turn.user
            meta = {"planner_steps": 0, "sql_tasks": 0, "sql_tasks_first_try": 0}

        for item in redline_violations(checks):
            redlines.append({"turn": index, "check": item["check"], "detail": item["detail"]})
        invalid = invalid or any(_data(check).get("precondition") is True
                                 and check.get("passed") is False for check in checks)
        turns.append({"question": question, "outcome": outcome, "passed": turn_passed(checks),
                      "first_failed_stage": first_failed_stage(checks), "meta": meta, "checks": checks})

    failed = [turn for turn in turns if not turn["passed"]]
    return {"case_id": case_run.get("case_id") or case.id, "run_index": case_run.get("run_index", 0),
            "passed": not failed, "invalid": invalid,
            "first_failed_stage": failed[0]["first_failed_stage"] if failed else None,
            "redlines": redlines, "turns": turns}


# ---- 第二步：汇总 ----

def _verdict(runs: list[dict]) -> str:
    """一条 Case 的全部运行：全过、全不过、还是有对有错。"""
    passed = sum(1 for run in runs if run.get("passed"))
    if passed == len(runs):
        return "stable_pass"
    if passed == 0:
        return "stable_fail"
    return "flaky"


def _run_detail(runs: list[dict]) -> str:
    """第一个没通过的运行里，第一处失败的 gating 检查。"""
    for run in runs:
        if run.get("passed"):
            continue
        for turn in run.get("turns") or []:
            for check in turn.get("checks") or []:
                if check.get("gating") and check.get("passed") is False:
                    return f"{check['check']}: {check.get('detail') or ''}"
        return ""
    return ""


def _stages(turns: list[dict]) -> dict:
    """分环节的统计。"""
    route = [check for _, check in _checks(turns, "route")]
    router_confusion = Counter(f"{_data(c).get('prefer')}->{_data(c).get('route')}"
                              for _, c in _checks(turns, "route") if not _data(c).get("prefer_hit"))

    plan_checks = [c for _, c in _checks(turns, "plan") if c.get("passed") is not None]
    planner_turns = [turn for turn in turns if _meta_num(turn, "planner_steps") >= 1]

    sql_result = [c for _, c in _checks(turns, "sql_result")]
    sql_rows = [c for _, c in _checks(turns, "sql_rows") if c.get("passed") is not None]
    sql_tasks = sum(_meta_num(turn, "sql_tasks") for turn in turns)
    sql_first_try = sum(_meta_num(turn, "sql_tasks_first_try") for turn in turns)

    retrieval_checks = [c for _, c in _checks(turns, "retrieval")]
    best_ranks = [_data(c).get("best_rank") for c in retrieval_checks]
    recalls = [_data(c).get("recall") for c in retrieval_checks]
    recalls = [float(r) for r in recalls if isinstance(r, (int, float)) and not isinstance(r, bool)]
    mrrs = [0.0 if rank is None else 1 / rank for rank in best_ranks]

    def passed_ratio(checks: list[dict]) -> float | None:
        return _ratio(sum(1 for c in checks if c.get("passed")), len(checks))

    facts = [(turn, c) for turn, c in _checks(turns, "answer_facts") if c.get("passed") is not None]
    facts_ok = [(turn, c) for turn, c in facts if _upstream_ok(turn)]
    grounded = [c for _, c in _checks(turns, "answer_grounded") if c.get("passed") is not None]

    return {
        "router": {
            "turns": len(route),
            "accept_rate": passed_ratio(route),
            "prefer_rate": _ratio(sum(1 for c in route if _data(c).get("prefer_hit")), len(route)),
            "dangerous": sum(1 for c in route
                             if c.get("passed") is False and _data(c).get("prefer") == "supervisor"
                             and _data(c).get("route") in ("sql", "rag")),
            "confusion": {key: router_confusion[key] for key in sorted(router_confusion)},
        },
        "plan": {
            "turns": len(plan_checks),
            "pass_rate": passed_ratio(plan_checks),
            "extra_tasks": sum(int(_data(c).get("extra_tasks") or 0) for c in plan_checks),
            "first_try_rate": _ratio(sum(1 for turn in planner_turns if _meta_num(turn, "planner_steps") == 1),
                                     len(planner_turns)),
        },
        "sql": {
            "turns": len(sql_result),
            "result_rate": passed_ratio(sql_result),
            "rows_rate": passed_ratio(sql_rows),
            "false_unanswerable": sum(1 for c in sql_result
                                      if c.get("passed") is False and _data(c).get("result") == "unanswerable"),
            "first_compile_rate": _ratio(sql_first_try, sql_tasks),
        },
        "retrieval": {
            "turns": len(retrieval_checks),
            "hit_at_1": _ratio(sum(1 for rank in best_ranks if rank == 1), len(retrieval_checks)),
            "hit_at_6": passed_ratio(retrieval_checks),
            "recall": _mean(recalls),
            "mrr": _mean(mrrs),
        },
        "answer": {
            "facts_turns": len(facts),
            "facts_rate": _ratio(sum(1 for _, c in facts if c.get("passed")), len(facts)),
            "facts_rate_upstream_ok": _ratio(sum(1 for _, c in facts_ok if c.get("passed")), len(facts_ok)),
            "literals_rate": passed_ratio([c for _, c in _checks(turns, "answer_literals")]),
            "grounded_turns": len(grounded),
            "grounded_rate": passed_ratio(grounded),
            "must_not_violations": sum(1 for _, c in _checks(turns, "answer_must_not")
                                       if c.get("passed") is False),
        },
        "turn": {
            "outcome_turns": len([c for _, c in _checks(turns, "outcome")]),
            "outcome_rate": passed_ratio([c for _, c in _checks(turns, "outcome")]),
        },
    }


def _upstream_ok(turn: dict) -> bool:
    """这一轮的查库和检索都没失败，说明出错的地方在回答本身。"""
    return not any(check.get("passed") is False for check in (turn.get("checks") or [])
                   if check.get("check") in UPSTREAM_CHECKS)


def _cost(turns: list[dict]) -> dict:
    """成本统计，取自 cost 检查的数据。"""
    cost_checks = [check for _, check in _checks(turns, "cost")]
    model_steps = [v for v in (_data(c).get("model_steps") for c in cost_checks) if isinstance(v, (int, float))]
    sql_sum = sum(v for v in (_data(c).get("sql") for c in cost_checks) if isinstance(v, (int, float)))
    sql_tasks = sum(_meta_num(turn, "sql_tasks") for turn in turns)
    seconds = sorted(v for v in (_data(c).get("seconds") for c in cost_checks)
                     if isinstance(v, (int, float)) and not isinstance(v, bool))
    reasons = [v for v in (_data(c).get("router_reason_len") for c in cost_checks)
               if isinstance(v, (int, float)) and v > 0]
    count = len(seconds)
    return {
        "model_steps_mean": _mean([float(v) for v in model_steps], 2),
        "sql_per_task_mean": None if not sql_tasks else round(sql_sum / sql_tasks, 2),
        "seconds_p50": seconds[int(count * 0.5)] if count else None,
        "seconds_p95": seconds[min(int(count * 0.95), count - 1)] if count else None,
        "router_reason_len_mean": _mean([float(v) for v in reasons], 2),
    }


def summarize(cases: list[Case], results: list[dict], repeats: int) -> dict:
    """把全部 RunResult 汇总成报告里的 Summary（结构见任务说明 20）。

    `repeats` 只是签名的一部分，汇总数字全部从 RunResult 现算，不由它换算。
    """
    runs_by_case: dict[str, list[dict]] = {}
    for run in results:
        runs_by_case.setdefault(run.get("case_id"), []).append(run)

    turn_list = _all_turns(results)

    # 每条 Case 的判定
    verdicts = {case.id: _verdict(runs_by_case[case.id]) for case in cases if runs_by_case.get(case.id)}
    passed_runs = sum(1 for run in results if run.get("passed"))

    def split_stats(split: str) -> dict:
        ids = {case.id for case in cases if case.split == split}
        subset = [run for run in results if run.get("case_id") in ids]
        hit = sum(1 for run in subset if run.get("passed"))
        return {"total": len(subset), "passed": hit, "rate": _ratio(hit, len(subset))}

    gate_cases = [case for case in cases if case.set == "gate" and runs_by_case.get(case.id)]
    gate_stable = {case.id for case in gate_cases if verdicts[case.id] == "stable_pass"}

    groups: dict[tuple[str, str], list[Case]] = {}
    for case in cases:
        groups.setdefault((case.business, case.situation), []).append(case)
    by_situation: list[dict] = []
    for (business, situation), group in sorted(groups.items()):
        subset = [run for case in group for run in runs_by_case.get(case.id, [])]
        hit = sum(1 for run in subset if run.get("passed"))
        by_situation.append({"business": business, "situation": situation, "cases": len(group),
                             "runs": len(subset), "passed": hit, "rate": _ratio(hit, len(subset))})

    case_table: list[dict] = []
    redlines: list[dict] = []
    invalid_ids: list[str] = []
    for case in sorted(cases, key=lambda item: item.id):
        subset = runs_by_case.get(case.id) or []
        if not subset:
            continue
        for run in subset:
            for item in run.get("redlines") or []:
                redlines.append({**item, "case_id": run.get("case_id"),
                                 "run_index": run.get("run_index")})
        if any(run.get("invalid") for run in subset):
            invalid_ids.append(case.id)
        failed_stages = [run.get("first_failed_stage") for run in subset if not run.get("passed")]
        if failed_stages:
            counts = Counter(failed_stages)
            best = max(counts.values())
            stage = min((stage for stage, count in counts.items()
                         if count == best), key=lambda s: STAGES.index(s) if s in STAGES else len(STAGES))
        else:
            stage = None
        case_table.append({"id": case.id, "business": case.business, "situation": case.situation,
                           "set": case.set, "split": case.split, "source": case.source,
                           "passes": sum(1 for run in subset if run.get("passed")), "runs": len(subset),
                           "verdict": verdicts[case.id], "first_failed_stage": stage,
                           "detail": _run_detail(subset)})

    redlines.sort(key=lambda item: (item.get("case_id"), item.get("run_index"), item.get("turn")))

    return {
        "runs": {"total": len(results), "passed": passed_runs, "rate": _ratio(passed_runs, len(results))},
        "cases": {"total": len(verdicts),
                  "stable_pass": sum(1 for v in verdicts.values() if v == "stable_pass"),
                  "flaky": sum(1 for v in verdicts.values() if v == "flaky"),
                  "stable_fail": sum(1 for v in verdicts.values() if v == "stable_fail")},
        "redlines": redlines,
        "invalid": sorted(invalid_ids),
        "gate": {"total": len(gate_cases), "stable_pass": len(gate_stable),
                 "failed": sorted(case.id for case in gate_cases if case.id not in gate_stable)},
        "splits": {"dev": split_stats("dev"), "holdout": split_stats("holdout")},
        "stages": _stages(turn_list),
        "cost": _cost(turn_list),
        "by_situation": by_situation,
        "case_table": case_table,
    }


# ---- 第三步：报告文本 ----

def _pct(value: Any) -> str:
    """比率显示成百分数，None 显示成 -。"""
    return "-" if value is None else f"{value:.1%}"


def _f2(value: Any) -> str:
    """保留两位小数，None 显示成 -。"""
    return "-" if value is None else f"{value:.2f}"


def _plain(value: Any) -> str:
    """原样显示，None 显示成 -。"""
    return "-" if value is None else str(value)


def render_text(report: dict) -> str:
    """把完整的报告字典渲染成人看的纯文本，分总览、分环节、没稳定通过的 Case 三部分。"""
    summary = report.get("summary") or {}
    runs = summary.get("runs") or {}
    cases = summary.get("cases") or {}
    gate = summary.get("gate") or {}
    splits = summary.get("splits") or {}
    agent = report.get("agent") or {}
    redlines = summary.get("redlines") or []

    overview = [
        f"报告 {report.get('report_id', '')}   评测集 {report.get('eval_set_version', '')}   "
        f"Agent {agent.get('git', '')} / 模型 {agent.get('models_hash', '')} / 索引 {agent.get('index_version', '')}",
        f"每条跑 {report.get('repeats', '')} 次   运行 {runs.get('passed', 0)}/{runs.get('total', 0)} "
        f"通过（{_pct(runs.get('rate'))}）",
        f"Case {cases.get('total', 0)} 条: 稳定通过 {cases.get('stable_pass', 0)}   "
        f"不稳定 {cases.get('flaky', 0)}   稳定失败 {cases.get('stable_fail', 0)}",
        f"红线违反 {len(redlines)} 次",
    ]
    for item in redlines:
        overview.append(f"  ! {item.get('case_id')} #{item.get('run_index')} "
                        f"第 {item.get('turn', 0) + 1} 轮 {item.get('check')}: {item.get('detail')}")
    overview.append(f"必过集 {gate.get('stable_pass', 0)}/{gate.get('total', 0)} 稳定通过")
    if gate.get("failed"):
        overview.append(f"  没有稳定通过: {'、'.join(gate['failed'])}")
    overview.append(f"调试集 {_pct((splits.get('dev') or {}).get('rate'))}   "
                    f"留出集 {_pct((splits.get('holdout') or {}).get('rate'))}")
    if summary.get("invalid"):
        overview.append(f"前提不成立（诱导片段没有被检索到）: {'、'.join(summary['invalid'])}")

    st = summary.get("stages") or {}
    router, plan = st.get("router") or {}, st.get("plan") or {}
    sql, retrieval = st.get("sql") or {}, st.get("retrieval") or {}
    answer, turn_stage = st.get("answer") or {}, st.get("turn") or {}
    cost = summary.get("cost") or {}

    stages_block = [
        f"路由      可接受 {_pct(router.get('accept_rate'))}   首选命中 {_pct(router.get('prefer_rate'))}   "
        f"危险方向 {router.get('dangerous', 0)} 次   （{router.get('turns', 0)} 轮）",
    ]
    if router.get("confusion"):
        stages_block.append("  分错的: " + "，".join(f"{key} {count} 次"
                                                    for key, count in router["confusion"].items()))
    stages_block += [
        f"计划      结构正确 {_pct(plan.get('pass_rate'))}   一次通过校验 {_pct(plan.get('first_try_rate'))}   "
        f"多余任务 {plan.get('extra_tasks', 0)} 个   （{plan.get('turns', 0)} 轮）",
        f"查库      结局正确 {_pct(sql.get('result_rate'))}   行正确 {_pct(sql.get('rows_rate'))}   "
        f"首次编译通过 {_pct(sql.get('first_compile_rate'))}   "
        f"把能答的说成答不了 {sql.get('false_unanswerable', 0)} 次   （{sql.get('turns', 0)} 轮）",
        f"检索      命中@1 {_pct(retrieval.get('hit_at_1'))}   命中@6 {_pct(retrieval.get('hit_at_6'))}   "
        f"召回 {_pct(retrieval.get('recall'))}   MRR {_f2(retrieval.get('mrr'))}   （{retrieval.get('turns', 0)} 轮）",
        f"回答      关键事实 {_pct(answer.get('facts_rate'))}（上游正确时 "
        f"{_pct(answer.get('facts_rate_upstream_ok'))}）   字面值 {_pct(answer.get('literals_rate'))}   "
        f"有依据 {_pct(answer.get('grounded_rate'))}   "
        f"违反禁止项 {answer.get('must_not_violations', 0)} 次",
        f"结局      {_pct(turn_stage.get('outcome_rate'))}   （{turn_stage.get('outcome_turns', 0)} 轮）",
        f"成本      平均模型调用 {_plain(cost.get('model_steps_mean'))} 次   "
        f"单任务 SQL {_plain(cost.get('sql_per_task_mean'))} 次   "
        f"耗时 P50 {_plain(cost.get('seconds_p50'))}s / P95 {_plain(cost.get('seconds_p95'))}s   "
        f"路由理由平均 {_plain(cost.get('router_reason_len_mean'))} 字",
    ]

    rows = [row for row in (summary.get("case_table") or []) if row.get("verdict") != "stable_pass"]
    if rows:
        tail = ["没有稳定通过的 Case:"] + [
            f"  {row.get('passes')}/{row.get('runs')}  {row.get('id')}  "
            f"[{row.get('first_failed_stage')}]  {row.get('detail') or ''}" for row in rows]
    else:
        tail = ["全部 Case 稳定通过"]

    return "\n\n".join("\n".join(block) for block in (overview, stages_block, tail))
