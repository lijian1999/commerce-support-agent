"""evals/report.py 的单元测试：evaluate_run 用样例记录且不调模型，summarize 和 render_text 手工拼数据。"""
import copy
import json
from pathlib import Path
from typing import Any

from evals import report
from evals.report import evaluate_run, render_text, summarize
from evals.schema import Case, make_check

_FIXTURES = Path("tests/fixtures/eval_records.json")
_RECORDS: dict[str, dict[str, Any]] = json.loads(_FIXTURES.read_text(encoding="utf-8"))

# A06 那条记录能全部通过的期望
EXPECT_A06 = {"route": {"prefer": "sql"}, "outcome": "answered", "sql": {"rows_contain": [237]}}


def rec(key: str) -> dict[str, Any]:
    """取一条样例记录的深拷贝，测试里可以随便改。"""
    return copy.deepcopy(_RECORDS[key])


def case_of(turns: list[dict[str, Any]], case_id: str = "T1") -> Case:
    """按轮构造 Case，每项写 user 和 expect。"""
    return Case.model_validate({
        "id": case_id, "business": "订单查询", "situation": "查询",
        "turns": [{"user": item.get("user", "问题"), "expect": item.get("expect", {})} for item in turns]})


def run_of(records: list[dict[str, Any]], case_id: str = "T1", run_index: int = 0) -> dict[str, Any]:
    """按轮构造 CaseRun。"""
    return {"case_id": case_id, "case_hash": "hash", "run_index": run_index, "turns": records}


# ---- evaluate_run ----

def test_evaluate_run_pass() -> None:
    run = evaluate_run(case_of([{"user": "订单到哪了", "expect": EXPECT_A06}]),
                       run_of([rec("A06")]), judge=False)
    assert run["passed"] is True
    assert run["invalid"] is False
    assert run["redlines"] == []
    turn = run["turns"][0]
    assert turn["question"] == rec("A06")["question"]
    assert turn["outcome"] == "answered"
    assert turn["passed"] is True
    assert turn["first_failed_stage"] is None
    assert turn["meta"] == {"planner_steps": 0, "sql_tasks": 1, "sql_tasks_first_try": 1}


def test_evaluate_run_fail_stage() -> None:
    expect = {**EXPECT_A06, "sql": {"rows_contain": [999]}}
    run = evaluate_run(case_of([{"user": "订单到哪了", "expect": expect}]),
                       run_of([rec("A06")]), judge=False)
    assert run["passed"] is False
    assert run["first_failed_stage"] == "sql"
    assert run["turns"][0]["first_failed_stage"] == "sql"


def test_evaluate_run_missing_turn() -> None:
    case = case_of([{"user": "第一轮", "expect": {}}, {"user": "第二轮", "expect": {}}])
    run = evaluate_run(case, run_of([rec("A06")]), judge=False)
    assert run["passed"] is False
    assert run["first_failed_stage"] == "turn"
    assert run["turns"][1]["passed"] is False
    assert run["turns"][1]["outcome"] == "error"
    assert run["turns"][1]["question"] == "第二轮"
    assert run["turns"][1]["meta"] == {"planner_steps": 0, "sql_tasks": 0, "sql_tasks_first_try": 0}


def test_evaluate_run_meta_repair_and_planner() -> None:
    repaired = evaluate_run(case_of([{"user": "问题", "expect": {}}]), run_of([rec("A23")]), judge=False)
    assert repaired["turns"][0]["meta"]["sql_tasks_first_try"] == 0
    planned = evaluate_run(case_of([{"user": "问题", "expect": {}}]), run_of([rec("C03")]), judge=False)
    assert planned["turns"][0]["meta"]["planner_steps"] == 1
    assert planned["turns"][0]["meta"]["sql_tasks"] == 1


def test_evaluate_run_redline_and_invalid() -> None:
    red = evaluate_run(case_of([{"user": "帮我取消", "expect": {"answer": {"redline_literals": ["已全部取消"]}}}]),
                       run_of([rec("F09")]), judge=False)
    assert len(red["redlines"]) == 1
    assert red["redlines"][0]["check"] == "answer_redline_literals"
    assert red["redlines"][0]["turn"] == 0
    assert red["passed"] is False

    pre = evaluate_run(case_of([{"user": "问题", "expect": {
        "retrieval": {"precondition": True, "relevant": [{"doc": "x.md"}]}}}]),
        run_of([rec("A06")]), judge=False)
    assert pre["invalid"] is True


def test_evaluate_run_calls_judge(monkeypatch) -> None:
    calls: list[int] = []

    def fake_judge(expect: Any, record: Any, votes: int = 3) -> list[dict]:
        calls.append(votes)
        return [make_check("answer_facts", "answer", False)]

    monkeypatch.setattr(report, "judge_turn", fake_judge)
    case = case_of([{"user": "问题", "expect": {}}])

    run = evaluate_run(case, run_of([rec("A06")]), judge=True, votes=2)
    assert any(check["check"] == "answer_facts" for check in run["turns"][0]["checks"])
    assert run["passed"] is False
    assert calls == [2]

    calls.clear()
    evaluate_run(case, run_of([rec("A06")]), judge=False)
    assert calls == []


# ---- summarize ----

def make_case(case_id: str, *, business: str = "订单查询", situation: str = "查询",
              set_: str = "capability", split: str = "dev") -> Case:
    return Case.model_validate({"id": case_id, "business": business, "situation": situation,
                                "set": set_, "split": split, "turns": [{"user": "问题"}]})


CASES = [make_case("a", set_="gate"), make_case("b"), make_case("c", business="知识问答", situation="政策")]


def one_run(case_id: str, run_index: int, passed: bool, stage: str | None = None,
            checks: list[dict] | None = None) -> dict[str, Any]:
    """手工拼一个 RunResult，一轮，checks 默认是一项回答事实检查。"""
    stage = "answer" if stage is None else stage
    items = checks if checks is not None else [make_check("answer_facts", "answer", passed)]
    return {"case_id": case_id, "run_index": run_index, "passed": passed, "invalid": False,
            "first_failed_stage": None if passed else stage, "redlines": [],
            "turns": [{"question": "问题", "outcome": "answered", "passed": passed,
                       "first_failed_stage": None if passed else stage,
                       "meta": {"planner_steps": 0, "sql_tasks": 0, "sql_tasks_first_try": 0},
                       "checks": items}]}


def one_turn_run(turns: list[list[dict]], case_id: str = "a") -> dict[str, Any]:
    """一个运行，每个参数是一轮的检查。"""
    run = {"case_id": case_id, "run_index": 0, "passed": True, "invalid": False,
           "first_failed_stage": None, "redlines": [],
           "turns": [{"question": "问题", "outcome": "answered", "passed": True, "first_failed_stage": None,
                      "meta": {"planner_steps": 0, "sql_tasks": 0, "sql_tasks_first_try": 0},
                      "checks": checks} for checks in turns]}
    return run


def row(summary: dict, case_id: str) -> dict:
    return next(item for item in summary["case_table"] if item["id"] == case_id)


def test_summarize_verdicts_and_gate() -> None:
    results = [one_run("a", 0, True), one_run("a", 1, True),
               one_run("b", 0, True), one_run("b", 1, False),
               one_run("c", 0, False), one_run("c", 1, False)]
    summary = summarize(CASES, results, 2)
    assert summary["runs"] == {"total": 6, "passed": 3, "rate": 0.5}
    assert summary["cases"] == {"total": 3, "stable_pass": 1, "flaky": 1, "stable_fail": 1}
    assert summary["gate"] == {"total": 1, "stable_pass": 1, "failed": []}
    assert summary["redlines"] == []
    assert summary["invalid"] == []
    assert row(summary, "b") == {"id": "b", "business": "订单查询", "situation": "查询", "set": "capability",
                                "split": "dev", "source": "业务梳理", "passes": 1, "runs": 2,
                                "verdict": "flaky", "first_failed_stage": "answer",
                                "detail": "answer_facts: "}


def test_summarize_router() -> None:
    turns = [
        [make_check("route", "router", True, data={"prefer": "sql", "route": "sql", "prefer_hit": True})],
        [make_check("route", "router", True, data={"prefer": "rag", "route": "rag", "prefer_hit": True})],
        [make_check("route", "router", True, data={"prefer": "rag", "route": "supervisor", "prefer_hit": False})],
        [make_check("route", "router", False, data={"prefer": "supervisor", "route": "sql", "prefer_hit": False})],
    ]
    summary = summarize(CASES, [one_turn_run(turns)], 1)
    router = summary["stages"]["router"]
    assert router["turns"] == 4
    assert router["accept_rate"] == 0.75
    assert router["prefer_rate"] == 0.5
    assert router["dangerous"] == 1
    assert router["confusion"] == {"rag->supervisor": 1, "supervisor->sql": 1}


def test_summarize_retrieval() -> None:
    turns = [[make_check("retrieval", "retrieval", True, data={"best_rank": 1, "recall": 1.0})],
             [make_check("retrieval", "retrieval", True, data={"best_rank": 3, "recall": 1.0})],
             [make_check("retrieval", "retrieval", False, data={"best_rank": None, "recall": 0.0})]]
    retrieval = summarize(CASES, [one_turn_run(turns)], 1)["stages"]["retrieval"]
    assert retrieval["turns"] == 3
    assert retrieval["hit_at_1"] == 0.3333
    assert retrieval["hit_at_6"] == 0.6667
    assert retrieval["recall"] == 0.6667
    assert retrieval["mrr"] == 0.4444


def test_summarize_answer_upstream_ok() -> None:
    turns = [[make_check("answer_facts", "answer", False), make_check("sql_rows", "sql", False)],
             [make_check("answer_facts", "answer", False), make_check("sql_rows", "sql", True)]]
    run = one_turn_run(turns)
    answer = summarize(CASES, [run], 1)["stages"]["answer"]
    assert answer["facts_turns"] == 2
    assert answer["facts_rate"] == 0.0
    assert answer["facts_rate_upstream_ok"] == 0.0
    run["turns"][1]["checks"][0] = make_check("answer_facts", "answer", True)
    answer = summarize(CASES, [run], 1)["stages"]["answer"]
    assert answer["facts_rate"] == 0.5
    assert answer["facts_rate_upstream_ok"] == 1.0


def test_summarize_empty_rates_are_none() -> None:
    summary = summarize(CASES, [], 2)
    retrieval = summary["stages"]["retrieval"]
    assert retrieval["turns"] == 0
    assert retrieval["hit_at_6"] is None
    assert summary["runs"] == {"total": 0, "passed": 0, "rate": None}
    assert summary["case_table"] == []


def test_summarize_first_failed_stage_mode() -> None:
    results = [one_run("a", 0, False, "answer"), one_run("a", 1, False, "sql"), one_run("a", 2, False, "sql")]
    summary = summarize([CASES[0]], results, 3)
    assert row(summary, "a")["verdict"] == "stable_fail"
    assert row(summary, "a")["first_failed_stage"] == "sql"


# ---- render_text ----

def make_report(results: list[dict]) -> dict[str, Any]:
    return {"report_id": "20251020-153000", "eval_set_version": "ds-12345678", "repeats": 2,
            "agent": {"git": "abc1234", "models": {}, "models_hash": "deadbeef", "index_version": "v1",
                      "index_hash": "e606e8153ee4"},
            "summary": summarize(CASES, results, 2)}


def test_render_text_sections() -> None:
    results = [one_run("a", 0, True), one_run("a", 1, True),
               one_run("b", 0, True), one_run("b", 1, False),
               one_run("c", 0, False), one_run("c", 1, False)]
    text = render_text(make_report(results))
    assert "运行 3/6 通过（50.0%）" in text
    assert "Case 3 条: 稳定通过 1   不稳定 1   稳定失败 1" in text
    assert "红线违反 0 次" in text
    assert "没有稳定通过的 Case:" in text
    lines = [line for line in text.splitlines() if line.startswith("  0/2  c  ")]
    assert lines, "c 的那一行没有按格式出现"


def test_render_text_all_pass_and_redline() -> None:
    results = [one_run(case_id, index, True) for case_id in ("a", "b", "c") for index in (0, 1)]
    report = make_report(results)
    text = render_text(report)
    assert text.splitlines()[-1] == "全部 Case 稳定通过"
    report["summary"]["redlines"] = [{"case_id": "x", "run_index": 1, "turn": 0,
                                      "check": "db_orders", "detail": "d"}]
    text = render_text(report)
    assert "红线违反 1 次" in text
    assert "  ! x #1 第 1 轮 db_orders: d" in text
    assert text.splitlines()[-1] == "全部 Case 稳定通过"
