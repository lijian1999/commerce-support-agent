"""evals/manifest.py 的单元测试：不连数据库、不调模型。"""
import json
import re
from pathlib import Path

from evals.manifest import (case_hash, check_release, describe_diff, diff_cases, eval_set_version,
                            evaluator_version, load_manifest, write_manifest)
from evals.schema import Case


def make_case(case_id: str, user: str = "你好") -> Case:
    return Case.model_validate({"id": case_id, "business": "闲聊", "situation": "问候",
                                "turns": [{"user": user}]})


def make_state(cases: list[Case], git: str = "abc1234", db: str = "dbhash000000") -> dict:
    """手工拼一个和 current_state 同结构的字典。"""
    hashes = {case.id: case_hash(case) for case in cases}
    return {
        "eval_set_version": eval_set_version(hashes),
        "cases": hashes,
        "agent": {"git": git, "models": {"router": "m1", "answer": "m1"}, "models_hash": "mh1",
                  "index_version": "v1", "index_hash": "ih1"},
        "env": {"db_hash": db, "today": "2025-10-20"},
        "evaluator": {"version": "ev-1", "judge_model": "j"},
    }


def make_report(state: dict) -> dict:
    return {"report_id": "r1", "repeats": 3, **state}


def make_manifest(state: dict) -> dict:
    return {"eval_set_version": state["eval_set_version"], "cases": state["cases"], "env": state["env"],
            "evaluator": state["evaluator"], "generated_at": "t"}


def test_case_hash_is_stable_and_sensitive() -> None:
    case = make_case("a")
    assert case_hash(case) == case_hash(case)
    assert len(case_hash(case)) == 64
    assert case_hash(make_case("a")) != case_hash(make_case("a", user="你好吗"))


def test_case_hash_ignores_yaml_key_order() -> None:
    one = {"id": "a", "business": "闲聊", "situation": "问候", "turns": [{"user": "你好"}]}
    two = {"turns": [{"user": "你好"}], "situation": "问候", "id": "a", "business": "闲聊"}
    assert case_hash(Case.model_validate(one)) == case_hash(Case.model_validate(two))


def test_eval_set_version_format_and_order() -> None:
    hashes = {"b": "2", "a": "1"}
    assert re.fullmatch(r"ds-[0-9a-f]{8}", eval_set_version(hashes))
    assert eval_set_version(hashes) == eval_set_version({"a": "1", "b": "2"})
    assert eval_set_version(hashes) != eval_set_version({"b": "3", "a": "1"})


def test_diff_and_describe() -> None:
    diff = diff_cases({"a": "1", "b": "2", "x": "9"}, {"a": "1", "b": "3", "c": "4", "d": "5"})
    assert diff == {"added": ["c", "d"], "removed": ["x"], "modified": ["b"]}
    assert describe_diff(diff) == "修改 1 个（b），新增 2 个（c、d），删除 1 个（x）"
    same = {"a": "1"}
    assert describe_diff(diff_cases(same, dict(same))) == "没有变化"
    ids = [f"c{i}" for i in range(7)]
    assert describe_diff(diff_cases({}, {i: "1" for i in ids})) == f"新增 7 个（{'、'.join(ids[:5])} 等）"


def test_evaluator_version_follows_file_content(tmp_path: Path) -> None:
    one = tmp_path / "one.py"
    two = tmp_path / "two.yaml"
    one.write_text("甲", encoding="utf-8")
    two.write_text("乙", encoding="utf-8")
    got = evaluator_version(files=[one, two], judge_model="j")
    assert re.fullmatch(r"ev-[0-9a-f]{8}", got["version"])
    assert got["judge_model"] == "j"
    assert got == evaluator_version(files=[one, two], judge_model="j")
    one.write_text("改了", encoding="utf-8")
    assert evaluator_version(files=[one, two], judge_model="j")["version"] != got["version"]
    assert evaluator_version(files=[one, two], judge_model="别的模型")["version"] != got["version"]
    missing = tmp_path / "missing.py"
    assert re.fullmatch(r"ev-[0-9a-f]{8}", evaluator_version(files=[missing, two], judge_model="j")["version"])


def test_manifest_roundtrip(tmp_path: Path) -> None:
    manifest = make_manifest(make_state([make_case("a"), make_case("b")]))
    path = tmp_path / "manifest.json"
    write_manifest(manifest, path)
    assert load_manifest(path) == manifest
    assert json.loads(path.read_text(encoding="utf-8")) == manifest
    assert load_manifest(tmp_path / "does-not-exist.json") is None


def test_release_all_consistent() -> None:
    state = make_state([make_case("a"), make_case("b")])
    items = check_release(make_report(state), make_manifest(state), state)
    assert [item["name"] for item in items] == ["评测集版本", "Agent 版本", "环境"]
    assert [item["ok"] for item in items] == [True, True, True]
    assert [item["problems"] for item in items] == [[], [], []]


def test_release_case_modified_after_report() -> None:
    report_state = make_state([make_case("a"), make_case("b")])
    current = make_state([make_case("a"), make_case("b", user="改了")])
    items = check_release(make_report(report_state), make_manifest(report_state), current)
    assert not items[0]["ok"]
    assert len(items[0]["problems"]) == 2
    assert items[0]["problems"][0] == "Case 改了但没有重新生成 Manifest：修改 1 个（b）"
    assert "报告是在" in items[0]["problems"][1] and "修改 1 个（b）" in items[0]["problems"][1]


def test_release_missing_manifest() -> None:
    state = make_state([make_case("a")])
    items = check_release(make_report(state), None, state)
    assert items[0]["problems"] == ["没有 Manifest，先运行 scripts/eval_manifest.py"]
    assert "Manifest 无" in items[0]["line"]
    assert items[2]["problems"] == []


def test_release_dirty_worktree() -> None:
    report_state = make_state([make_case("a")])
    current = make_state([make_case("a")], git="abc1234-dirty")
    items = check_release(make_report(report_state), make_manifest(report_state), current)
    assert items[1]["problems"][0] == "工作区有未提交的修改，不能用于发布"
    assert items[1]["problems"][1].startswith("报告是在代码 abc1234 上跑的")


def test_release_model_changed() -> None:
    report_state = make_state([make_case("a")])
    current = make_state([make_case("a")])
    current["agent"]["models"] = {"router": "m2", "answer": "m1"}
    current["agent"]["models_hash"] = "mh2"
    items = check_release(make_report(report_state), make_manifest(report_state), current)
    assert "模型配置不同，不一样的角色：router" in items[1]["problems"]


def test_release_index_changed() -> None:
    report_state = make_state([make_case("a")])
    current = make_state([make_case("a")])
    current["agent"]["index_hash"] = "ih9"
    items = check_release(make_report(report_state), make_manifest(report_state), current)
    assert any(p.startswith("知识库索引不同") for p in items[1]["problems"])


def test_release_env_changed() -> None:
    state = make_state([make_case("a")])
    report_state = json.loads(json.dumps(make_state([make_case("a")])))
    report_state["env"]["db_hash"] = "other00000"
    report_state["evaluator"]["version"] = "ev-0"
    items = check_release(make_report(report_state), make_manifest(state), state)
    assert len(items[2]["problems"]) == 2
    assert items[2]["problems"][0].startswith("数据库内容不同")
    assert items[2]["problems"][1].startswith("判分方式变了")
