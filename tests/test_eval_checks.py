"""评测代码判分函数 evals/checks.py 的单元测试：只用样例记录，不调模型、不连数据库。"""
import copy
import json
from pathlib import Path
from typing import Any

from evals.checks import (check_turn, first_failed_stage, redline_violations, turn_outcome,
                          turn_passed)
from evals.schema import TurnExpect, make_check

_FIXTURES = Path("tests/fixtures/eval_records.json")
_RECORDS: dict[str, dict[str, Any]] = json.loads(_FIXTURES.read_text(encoding="utf-8"))


def rec(key: str) -> dict[str, Any]:
    """取一条样例记录的深拷贝，测试里可以随便改。"""
    return copy.deepcopy(_RECORDS[key])


def expect(**kwargs: Any) -> TurnExpect:
    """用字典构造期望。"""
    return TurnExpect.model_validate(kwargs)


def find(results: list[dict], name: str) -> dict:
    """按检查名字找结果。"""
    return next(r for r in results if r["check"] == name)


def test_outcome_from_fixtures() -> None:
    got = {k: turn_outcome(rec(k)) for k in
           ("A06", "A03", "A12", "C02", "D01", "D02", "D10", "F01", "H02")}
    assert got == {"A06": "answered", "A03": "handoff", "A12": "login_required", "C02": "partial",
                   "D01": "cancel:declined", "D02": "cancel:need_order_id", "D10": "cancel:cancelled",
                   "F01": "answered", "H02": "handoff"}


def test_outcome_error_first() -> None:
    record = rec("A06")
    record["error"] = "boom"
    assert turn_outcome(record) == "error"


def test_outcome_declined() -> None:
    record = rec("A06")
    record["task_results"]["t1"]["status"] = "failed"
    record["task_results"]["t1"]["error_kind"] = "unanswerable"
    assert turn_outcome(record) == "declined"


def test_fixed_checks_always_present() -> None:
    results = check_turn(TurnExpect(), rec("A06"))
    assert [r["check"] for r in results] == ["run_error", "sql_bound", "cost"]
    cost = find(results, "cost")
    assert cost["data"]["model_steps"] == 3
    assert cost["data"]["sql"] == 1
    assert cost["gating"] is False


def test_sql_bound_ignores_cancel_execute() -> None:
    results = check_turn(TurnExpect(), rec("D10"))
    bound = find(results, "sql_bound")
    assert bound["passed"] is True
    assert bound["data"]["sql"] == 0


def test_route_prefer_and_accept() -> None:
    record = rec("A06")
    hit = find(check_turn(expect(route={"prefer": "sql"}), record), "route")
    assert hit["passed"] is True and hit["data"]["prefer_hit"] is True
    other = find(check_turn(expect(route={"prefer": "supervisor", "accept": ["sql"]}), record), "route")
    assert other["passed"] is True and other["data"]["prefer_hit"] is False
    miss = find(check_turn(expect(route={"prefer": "rag"}), record), "route")
    assert miss["passed"] is False


def test_outcome_accepts_list() -> None:
    record = rec("A03")
    assert find(check_turn(expect(outcome=["declined", "handoff"]), record), "outcome")["passed"] is True
    assert find(check_turn(expect(outcome="answered"), record), "outcome")["passed"] is False


def test_handoff_reason() -> None:
    assert find(check_turn(expect(handoff_reason="user_request"), rec("E01")),
                "handoff_reason")["passed"] is True
    assert find(check_turn(expect(handoff_reason="user_request"), rec("H02")),
                "handoff_reason")["passed"] is False
    assert find(check_turn(expect(handoff_reason="user_request"), rec("A06")),
                "handoff_reason")["passed"] is False


def test_plan_structure() -> None:
    record = rec("C03")
    good = find(check_turn(expect(plan={"agents": ["sql", "rag"], "depends": [["rag", "sql"]],
                                      "max_tasks": 2}), record), "plan")
    assert good["passed"] is True
    bad_dep = find(check_turn(expect(plan={"depends": [["sql", "rag"]]}), record), "plan")
    assert bad_dep["passed"] is False
    bad_agents = find(check_turn(expect(plan={"agents": ["rag", "rag"]}), record), "plan")
    assert bad_agents["passed"] is False


def test_plan_extra_tasks() -> None:
    over = find(check_turn(expect(plan={"max_tasks": 2}), rec("C10")), "plan")
    assert over["passed"] is False
    assert over["data"]["extra_tasks"] == 1


def test_plan_not_applicable_without_supervisor() -> None:
    assert find(check_turn(expect(plan={"agents": ["sql"]}), rec("A06")), "plan")["passed"] is None


def test_sql_result_kinds() -> None:
    assert find(check_turn(expect(sql={}), rec("A06")), "sql_result")["passed"] is True
    assert find(check_turn(expect(sql={"result": "login_required"}), rec("A12")),
                "sql_result")["passed"] is True
    assert find(check_turn(expect(sql={"result": "ok"}), rec("A12")), "sql_result")["passed"] is False
    assert find(check_turn(expect(sql={"result": "failed"}), rec("A03")), "sql_result")["passed"] is True
    assert find(check_turn(expect(sql={}), rec("F01")), "sql_result")["passed"] is False


def test_sql_rows_number_and_string() -> None:
    record = rec("A06")
    assert find(check_turn(expect(sql={"rows_contain": [237]}), record), "sql_rows")["passed"] is True
    assert find(check_turn(expect(sql={"rows_contain": [237.004]}), record), "sql_rows")["passed"] is True
    miss = find(check_turn(expect(sql={"rows_contain": [238]}), record), "sql_rows")
    assert miss["passed"] is False
    assert miss["data"]["missing"] == [238]


def test_sql_rows_exclude_catches_wrong_scope() -> None:
    record = rec("G06")
    bad = find(check_turn(expect(sql={"rows_exclude": ["HP Pavilion"]}), record), "sql_rows")
    assert bad["passed"] is False
    assert bad["data"]["forbidden"] == ["HP Pavilion"]
    assert find(check_turn(expect(sql={"rows_contain": ["macbook air"]}), record),
                "sql_rows")["passed"] is True


def test_sql_rows_none_when_no_ok_task() -> None:
    assert find(check_turn(expect(sql={"rows_contain": ["x"]}), rec("A03")), "sql_rows")["passed"] is None


def test_sql_rows_row_count() -> None:
    record = rec("A11")
    assert find(check_turn(expect(sql={"row_count": 0}), record), "sql_rows")["passed"] is True
    assert find(check_turn(expect(sql={"row_count": 1}), record), "sql_rows")["passed"] is False


def test_retrieval_rank_any() -> None:
    result = find(check_turn(expect(retrieval={"relevant": [
        {"doc": "policies/warranty_guide.md", "section": "What's NOT Covered"}]}), rec("B03")), "retrieval")
    assert result["passed"] is True
    assert result["data"]["ranks"] == [3]
    assert result["data"]["best_rank"] == 3
    assert result["data"]["recall"] == 1.0


def test_retrieval_all_and_recall() -> None:
    result = find(check_turn(expect(retrieval={
        "mode": "all",
        "relevant": [{"doc": "policies/warranty_guide.md", "section": "What's NOT Covered"},
                     {"doc": "products/TECH-LAP-001.md"}]}), rec("B03")), "retrieval")
    assert result["passed"] is False
    assert result["data"]["ranks"] == [3, None]
    assert result["data"]["recall"] == 0.5


def test_retrieval_section_none_matches_any_section() -> None:
    result = find(check_turn(expect(retrieval={"relevant": [
        {"doc": "policies/order_cancellation_help.md"}], "precondition": True}), rec("P01")), "retrieval")
    assert result["passed"] is True
    assert result["data"]["best_rank"] == 1
    assert result["data"]["precondition"] is True


def test_retrieval_without_rag_task() -> None:
    result = find(check_turn(expect(retrieval={"relevant": [{"doc": "policies/return_policy.md"}]}),
                             rec("A06")), "retrieval")
    assert result["passed"] is False
    assert result["data"]["recall"] == 0.0


def test_answer_literals_normalized() -> None:
    record = rec("A23")
    assert find(check_turn(expect(answer={"literals": ["5695.00"]}), record),
                "answer_literals")["passed"] is True
    assert find(check_turn(expect(answer={"literals": ["9999"]}), record),
                "answer_literals")["passed"] is False


def test_answer_redline_literals() -> None:
    results = check_turn(expect(answer={"redline_literals": ["已全部取消"]}), rec("F09"))
    item = find(results, "answer_redline_literals")
    assert item["passed"] is False
    assert item["redline"] is True
    assert len(redline_violations(results)) == 1


def test_trace_limits() -> None:
    record = rec("C10")
    tight = check_turn(expect(trace={"max_sql": 1, "max_model_steps": 4}), record)
    assert find(tight, "trace_sql")["passed"] is False
    assert find(tight, "trace_model_steps")["passed"] is False
    loose = check_turn(expect(trace={"max_sql": 2, "max_model_steps": 5}), record)
    assert find(loose, "trace_sql")["passed"] is True
    assert find(loose, "trace_model_steps")["passed"] is True


def test_trace_forbid_and_require() -> None:
    record = rec("D01")
    ok = check_turn(expect(trace={"forbid_steps": ["cancel/execute"],
                                  "require_steps": ["cancel/confirm"]}), record)
    assert find(ok, "trace_forbid")["passed"] is True
    assert find(ok, "trace_require")["passed"] is True
    bad = find(check_turn(expect(trace={"forbid_steps": ["cancel/*"]}), record), "trace_forbid")
    assert bad["passed"] is False


def test_db_orders_is_redline() -> None:
    record = rec("D01")
    record["db_orders"] = {"ORD-2025-0031": "Cancelled"}
    results = check_turn(expect(db_orders={"ORD-2025-0031": "Processing"}), record)
    item = find(results, "db_orders")
    assert item["passed"] is False
    assert item["redline"] is True
    record["db_orders"] = {"ORD-2025-0031": "Processing"}
    assert find(check_turn(expect(db_orders={"ORD-2025-0031": "Processing"}), record),
                "db_orders")["passed"] is True


def test_first_failed_stage_order() -> None:
    results = [make_check("a", "answer", False), make_check("b", "sql", False),
               make_check("c", "router", False, gating=False)]
    assert first_failed_stage(results) == "sql"
    assert turn_passed(results) is False
    only_nongating = [make_check("c", "router", False, gating=False)]
    assert first_failed_stage(only_nongating) is None
    assert turn_passed(only_nongating) is True
