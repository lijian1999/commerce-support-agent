"""evals/run.py 的测试：选 Case、拼报告、命令行主流程。不调模型、不连库。"""
import copy
import json
import sys
import types

import evals.run
from evals.manifest import case_hash, eval_set_version
from evals.schema import Case, make_check

# 假的 current_state 返回值
STATE = {
    "eval_set_version": "ds-all",
    "cases": {},
    "agent": {"git": "g", "models": {}, "models_hash": "m", "index_version": "v1", "index_hash": "i"},
    "env": {"db_hash": "d1", "today": "2025-10-20"},
    "evaluator": {"version": "ev-1", "judge_model": "j"},
}


def make_case(case_id: str, **kw) -> Case:
    return Case.model_validate({"id": case_id, "business": "闲聊", "situation": "x",
                                "turns": [{"user": "你好"}], **kw})


def passing_result(case_id: str, run_index: int) -> dict:
    """一个通过的 RunResult。"""
    return {"case_id": case_id, "run_index": run_index, "passed": True, "invalid": False,
            "first_failed_stage": None, "redlines": [],
            "turns": [{"question": "你好", "outcome": "answered", "passed": True,
                       "first_failed_stage": None,
                       "meta": {"planner_steps": 0, "sql_tasks": 0, "sql_tasks_first_try": 0},
                       "checks": []}]}


def fake_evaluate(case: Case, case_run: dict, judge: bool = True, votes: int = 3) -> dict:
    return passing_result(case.id, case_run["run_index"])


def fake_run_cases(cases: list[Case], repeats: int, workers: int, hashes: dict,
                   progress=None) -> list[dict]:
    """每条 Case 的每次重复一个 CaseRun，并调用 progress。"""
    runs: list[dict] = []
    total = len(cases) * repeats
    for case in cases:
        for run_index in range(repeats):
            case_run = {"case_id": case.id, "case_hash": (hashes or {}).get(case.id, ""),
                        "run_index": run_index, "turns": [{"seconds": 1.5}]}
            runs.append(case_run)
            if progress:
                progress(len(runs), total, case_run)
    return runs


def install(monkeypatch, tmp_path, cases: list[Case], *, evaluate=fake_evaluate,
            manifest=None) -> dict:
    """把 evals.run 里导入的函数换成假的，报告目录指到 tmp_path，返回调用次数记录。"""
    calls = {"run_cases": 0, "poisoned": 0}

    def run_cases(*args, **kwargs):
        calls["run_cases"] += 1
        return fake_run_cases(*args, **kwargs)

    def build_poisoned_index():
        calls["poisoned"] += 1
        return {"version": "v1", "index_hash": "h"}

    monkeypatch.setattr(evals.run, "load_cases", lambda: list(cases))
    monkeypatch.setattr(evals.run, "current_state", lambda _cases: copy.deepcopy(STATE))
    monkeypatch.setattr(evals.run, "load_manifest", lambda: copy.deepcopy(manifest) if manifest else None)
    monkeypatch.setattr(evals.run, "run_cases", run_cases)
    monkeypatch.setattr(evals.run, "evaluate_run", evaluate)
    monkeypatch.setattr(evals.run, "build_poisoned_index", build_poisoned_index)
    monkeypatch.setattr(evals.run, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(evals.run, "RUNS_DIR", tmp_path / "runs")
    return calls


def test_select_cases():
    cases = [make_case("ord-a", **{"set": "gate"}),
             make_case("ord-b", split="holdout"),
             make_case("kb-c")]
    assert [c.id for c in evals.run.select_cases(cases, patterns=["ord-*"])] == ["ord-a", "ord-b"]
    assert [c.id for c in evals.run.select_cases(cases, set_name="gate")] == ["ord-a"]
    assert [c.id for c in evals.run.select_cases(cases, split="holdout")] == ["ord-b"]
    assert [c.id for c in evals.run.select_cases(cases, patterns=["kb-*", "ord-a"])] == ["ord-a", "kb-c"]
    assert [c.id for c in evals.run.select_cases(cases)] == ["ord-a", "ord-b", "kb-c"]


def test_build_report_versions():
    cases = [make_case("a"), make_case("b")]
    results = [passing_result("a", 0)]
    options = {"repeats": 2}
    report = evals.run.build_report("r-1", cases, results, copy.deepcopy(STATE), options)
    assert report["report_id"] == "r-1"
    assert isinstance(report["created_at"], str)
    assert report["cases"] == {"a": case_hash(cases[0]), "b": case_hash(cases[1])}
    assert report["eval_set_version"] == eval_set_version(report["cases"])
    assert report["eval_set_version"] != "ds-all"
    assert report["agent"] == STATE["agent"]
    assert report["env"] == STATE["env"]
    assert report["evaluator"] == STATE["evaluator"]
    assert report["repeats"] == 2
    assert report["summary"]["runs"]["total"] == 1
    assert report["results"] == results


def test_main_writes_report_and_runs(monkeypatch, tmp_path, capsys):
    install(monkeypatch, tmp_path, [make_case("alpha"), make_case("beta")])
    rc = evals.run.main(["--repeats", "2", "--no-judge"])
    assert rc == 0
    reports = list((tmp_path / "reports").glob("*.json"))
    runs = list((tmp_path / "runs").glob("*.json"))
    assert len(reports) == 1 and len(runs) == 1
    assert reports[0].name == runs[0].name
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["summary"]["runs"] == {"total": 4, "passed": 4, "rate": 1.0}
    assert report["options"]["judge"] is False
    out = capsys.readouterr().out
    assert "[4/4]" in out
    assert "运行 4/4 通过（100.0%）" in out
    assert any(line.startswith("报告: evals/reports/") for line in out.splitlines())


def test_main_no_case_selected(monkeypatch, tmp_path, capsys):
    calls = install(monkeypatch, tmp_path, [make_case("alpha")])
    rc = evals.run.main(["--case", "nothing-*"])
    assert rc == 2
    assert "没有选中任何 Case" in capsys.readouterr().out
    assert calls["run_cases"] == 0


def test_main_db_mismatch(monkeypatch, tmp_path, capsys):
    calls = install(monkeypatch, tmp_path, [make_case("alpha")],
                    manifest={"env": {"db_hash": "other"}})
    assert evals.run.main([]) == 2
    assert calls["run_cases"] == 0
    assert "数据库不是 Manifest 记录的初始状态" in capsys.readouterr().out
    assert evals.run.main(["--force"]) == 0
    assert calls["run_cases"] == 1


def test_main_exit_code_on_gate_failure(monkeypatch, tmp_path, capsys):
    def failing_evaluate(case: Case, case_run: dict, judge: bool = True, votes: int = 3) -> dict:
        result = passing_result(case.id, case_run["run_index"])
        result["passed"] = False
        result["turns"][0]["passed"] = False
        result["turns"][0]["first_failed_stage"] = "router"
        result["turns"][0]["checks"] = [make_check("route", "router", False, "x")]
        return result

    install(monkeypatch, tmp_path, [make_case("gate-1", **{"set": "gate"})],
            evaluate=failing_evaluate)
    assert evals.run.main([]) == 1


def test_main_builds_poisoned_index_only_when_needed(monkeypatch, tmp_path, capsys):
    calls = install(monkeypatch, tmp_path, [make_case("alpha")])
    assert evals.run.main([]) == 0
    assert calls["poisoned"] == 0
    capsys.readouterr()

    calls = install(monkeypatch, tmp_path, [make_case("p-1", env={"index": "poisoned"})])
    assert evals.run.main([]) == 0
    assert calls["poisoned"] == 1
    assert "带诱导文档的索引 v1，指纹 h" in capsys.readouterr().out


def test_main_langsmith_path(monkeypatch, tmp_path, capsys):
    calls = install(monkeypatch, tmp_path, [make_case("x-1")])
    recorded: dict = {}

    def execute_langsmith(cases, repeats, workers, judge, votes, report_id, state):
        recorded["args"] = (cases, repeats, workers, judge, votes, report_id, state)
        return [], [passing_result("x-1", 0)], {"experiment": "e1", "url": "http://x"}

    module = types.ModuleType("evals.langsmith_run")
    module.execute_langsmith = execute_langsmith
    sys.modules["evals.langsmith_run"] = module
    try:
        rc = evals.run.main(["--langsmith"])
    finally:
        sys.modules.pop("evals.langsmith_run", None)
    assert rc == 0
    assert "LangSmith 实验: e1   http://x" in capsys.readouterr().out
    assert calls["run_cases"] == 0
    args = recorded["args"]
    assert [c.id for c in args[0]] == ["x-1"]
    assert args[1:5] == (3, 4, True, 3)
    assert args[6] == STATE
