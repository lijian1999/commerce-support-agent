"""evals/compare.py 的单元测试：纯函数，报告字典手工拼，不跑评测也不读索引。"""
import json
from pathlib import Path
import pytest
from typing import Any

from evals import compare
from evals.compare import compare_reports, render_compare


# 第 1、第 7 条测试用的旧报告，六条 Case 六种分类走向
OLD_ROWS = {"a": (3, 3), "b": (3, 3), "c": (0, 3), "d": (3, 3), "e": (1, 3), "f": (2, 3)}
NEW_ROWS = {"a": (3, 3), "b": (1, 3), "c": (3, 3), "d": (2, 3), "e": (3, 3), "f": (0, 3)}


def make_report(report_id: str, rows: dict[str, tuple[int, int]], hashes: dict[str, str] | None = None,
                redlines: int = 0, gate_failed: list[str] | None = None,
                stages: dict[str, Any] | None = None) -> dict[str, Any]:
    """拼一份报告：rows 是 {id: (passes, runs)}，没全过的行带上出错环节和原因。"""
    return {
        "report_id": report_id,
        "eval_set_version": "ds-" + report_id,
        "agent": {"git": "g-" + report_id},
        "cases": hashes if hashes is not None else {case_id: "h" for case_id in rows},
        "summary": {
            "case_table": [{"id": case_id, "passes": passes, "runs": runs,
                            "first_failed_stage": None if passes == runs else "sql",
                            "detail": "" if passes == runs else "sql_rows: x"}
                           for case_id, (passes, runs) in rows.items()],
            "redlines": [{}] * redlines,
            "gate": {"failed": gate_failed if gate_failed is not None else []},
            "stages": stages or {},
        },
    }


def test_classify() -> None:
    old, new = make_report("o", OLD_ROWS), make_report("n", NEW_ROWS)
    result = compare_reports(old, new)
    assert [item["id"] for item in result["regressed"]] == ["b"]
    assert result["regressed"][0]["old"] == "3/3"
    assert result["regressed"][0]["new"] == "1/3"
    assert result["regressed"][0]["first_failed_stage"] == "sql"
    assert result["regressed"][0]["detail"] == "sql_rows: x"
    assert [item["id"] for item in result["improved"]] == ["c", "e"]
    assert [item["id"] for item in result["wobble"]] == ["d", "f"]
    assert result["same"] == 1


def test_only_same_hash_compared() -> None:
    old = make_report("o", {"a": (3, 3), "b": (3, 3)}, hashes={"a": "h1", "b": "h1", "x": "h"})
    new = make_report("n", {"a": (3, 3), "b": (3, 3)}, hashes={"a": "h1", "b": "h2", "y": "h"})
    result = compare_reports(old, new)
    assert result["common"] == ["a"]
    assert result["changed"] == ["b"]
    assert result["only_old"] == ["x"]
    assert result["only_new"] == ["y"]
    for key in ("regressed", "improved", "wobble"):
        assert "b" not in [item["id"] for item in result[key]]


def test_rate_and_noise() -> None:
    old = make_report("o", {"a": (3, 3), "b": (3, 3)})
    new = make_report("n", {"a": (3, 3), "b": (2, 3)})
    result = compare_reports(old, new)
    assert result["rate_old"] == 1.0
    assert result["rate_new"] == 0.8333
    assert result["rate_delta"] == -0.1667
    assert result["rate_conclusive"] is True
    assert compare_reports(old, new, noise=0.2)["rate_conclusive"] is False


def test_no_common_cases() -> None:
    result = compare_reports(make_report("o", {"a": (3, 3)}), make_report("n", {"b": (3, 3)}))
    assert result["rate_old"] is None
    assert result["rate_delta"] is None
    assert result["rate_conclusive"] is False
    assert result["release_ok"] is True


def test_release_problems_order() -> None:
    old = make_report("o", {"a": (3, 3), "b": (3, 3)})
    new = make_report("n", {"a": (3, 3), "b": (0, 3)}, redlines=2, gate_failed=["g1"])
    result = compare_reports(old, new)
    assert result["release_ok"] is False
    problems = result["release_problems"]
    assert problems[0].startswith("红线违反 2 次")
    assert problems[1].startswith("必过集有 1 条没有稳定通过")
    assert problems[2].startswith("有 1 条 Case 回退")
    assert problems[3].startswith("通过率下降")


def test_stages_fixed_order() -> None:
    old = make_report("o", {"a": (3, 3)}, stages={"router": {"accept_rate": 0.9}})
    new = make_report("n", {"a": (3, 3)}, stages={"router": {"accept_rate": 1.0},
                                                 "retrieval": {"mrr": 0.8}})
    stages = compare_reports(old, new)["stages"]
    assert len(stages) == 11
    assert stages[0] == {"stage": "router", "metric": "accept_rate", "old": 0.9, "new": 1.0}
    mrr = next(item for item in stages if item["metric"] == "mrr")
    assert mrr["old"] is None
    assert mrr["new"] == 0.8


def test_render() -> None:
    old, new = make_report("o", OLD_ROWS), make_report("n", NEW_ROWS)
    text = render_compare(compare_reports(old, new))
    assert "回退 1 条   变好 2 条   小幅波动 2 条   没变 1 条" in text
    assert "  b  3/3 → 1/3  [sql]  sql_rows: x" in text
    assert "小幅波动（不下结论）:" in text
    assert text.splitlines()[-1].startswith("发布门槛: 不通过（有 1 条 Case 回退")

    same = render_compare(compare_reports(old, make_report("n", dict(OLD_ROWS))))
    assert same.splitlines()[-1] == "发布门槛: 通过"
    assert "回退:" not in same


def test_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    old = make_report("o", {"a": (3, 3), "b": (3, 3)})
    new = make_report("n", {"a": (3, 3), "b": (3, 3)})
    path_old, path_new = tmp_path / "old.json", tmp_path / "new.json"
    path_old.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    path_new.write_text(json.dumps(new, ensure_ascii=False), encoding="utf-8")
    assert compare.main([str(path_old), str(path_new)]) == 0

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(make_report("b", {"a": (0, 3), "b": (3, 3)})), encoding="utf-8")
    assert compare.main([str(path_old), str(bad)]) == 1

    missing = tmp_path / "nope.json"
    assert compare.main([str(missing), str(path_new)]) == 2
    assert "找不到报告: " in capsys.readouterr().out
