"""evals/langsmith_run.py 的单元测试：用内存假客户端，不连 LangSmith、不调模型。"""
import types
import uuid
from typing import Any, Iterator

import pytest

from evals import langsmith_run
from evals.manifest import case_hash
from evals.schema import Case, make_check


def make_case(case_id: str, **kw) -> Case:
    return Case.model_validate(
        {"id": case_id, "business": "闲聊", "situation": "x", "turns": [{"user": "你好"}], **kw}
    )


class FakeExperiment:
    """能被 list() 遍历的实验结果：每次运行是一行。"""

    def __init__(self, rows: list[dict]) -> None:
        self.experiment_name = "exp-1"
        self.url = "http://x"
        self._rows = rows

    def __iter__(self) -> Iterator[dict]:
        return iter(self._rows)


class FakeClient:
    """内存里存样例的假 LangSmith 客户端，记录每个方法被调用的次数。"""

    def __init__(self) -> None:
        self.examples: dict[str, types.SimpleNamespace] = {}
        self.datasets: dict[str, str] = {}
        self.calls: dict[str, int] = {}

    def _count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def has_dataset(self, *, dataset_name: str | None = None, dataset_id: Any = None) -> bool:
        self._count("has_dataset")
        return dataset_name in self.datasets

    def create_dataset(self, dataset_name: str, *, description: str | None = None, **kw) -> Any:
        self._count("create_dataset")
        self.datasets[dataset_name] = "ds1"
        return types.SimpleNamespace(id="ds1")

    def read_dataset(self, *, dataset_name: str | None = None, dataset_id: Any = None) -> Any:
        self._count("read_dataset")
        return types.SimpleNamespace(id="ds1")

    def list_examples(self, dataset_id: Any = None, example_ids: Any = None, **kw) -> Iterator:
        self._count("list_examples")
        if example_ids is None:
            return iter(list(self.examples.values()))
        return iter([self.examples[str(i)] for i in example_ids if str(i) in self.examples])

    def create_examples(self, *, dataset_id: Any = None, examples: Any = None, **kw) -> dict:
        self._count("create_examples")
        for item in examples or []:
            self.examples[str(item["id"])] = types.SimpleNamespace(
                id=item["id"], inputs=item["inputs"], outputs=item["outputs"],
                metadata=item["metadata"])
        return {}

    def update_example(self, example_id: Any, *, inputs: Any = None, outputs: Any = None,
                       metadata: Any = None, **kw) -> dict:
        self._count("update_example")
        example = self.examples[str(example_id)]
        example.inputs, example.outputs, example.metadata = inputs, outputs, metadata
        return {}

    def evaluate(self, target, *, data=None, evaluators=None, num_repetitions: int = 1,
                 **kw) -> FakeExperiment:
        self._count("evaluate")
        rows: list[dict] = []
        for example in data or []:
            for _ in range(num_repetitions):
                outputs = target(example.inputs)
                run = types.SimpleNamespace(outputs=outputs, child_runs=None)
                scores = [ev(run, example) for ev in evaluators or []]
                rows.append({"outputs": outputs, "results": scores})
        return FakeExperiment(rows)


def node(name: str, run_type: str, children: Any = None) -> types.SimpleNamespace:
    return types.SimpleNamespace(name=name, run_type=run_type, child_runs=children)


STATE: dict[str, Any] = {"eval_set_version": "ds-1",
                         "agent": {"git": "abc", "models_hash": "m", "index_version": "v1",
                                   "index_hash": "h"},
                         "env": {"db_hash": "d"}, "evaluator": {"version": "ev-1"}}


# ---- 第一步：样例 ----

def test_example_id_is_stable() -> None:
    first = langsmith_run.example_id("a")
    assert isinstance(first, uuid.UUID)
    assert first == langsmith_run.example_id("a")
    assert first != langsmith_run.example_id("b")


def test_example_payload() -> None:
    case = make_case("a", turns=[{"user": "你好"}, {"user": "查订单", "expect": {"route": {"prefer": "sql"}}}])
    payload = langsmith_run.example_payload(case)
    assert payload["id"] == langsmith_run.example_id("a")
    assert payload["inputs"]["case_id"] == "a"
    assert payload["inputs"]["turns"] == ["你好", "查订单"]
    assert payload["inputs"]["env"] == case.env.model_dump(mode="json")
    assert len(payload["outputs"]["expect"]) == 2
    assert payload["outputs"]["expect"][1] == {"route": {"prefer": "sql"}}
    assert payload["metadata"]["case_hash"] == case_hash(case)


# ---- 第一步：同步 ----

def test_sync_creates_then_unchanged() -> None:
    client = FakeClient()
    cases = [make_case("a"), make_case("b")]
    first = langsmith_run.sync_dataset(client, cases)
    assert first["created"] == 2 and first["updated"] == 0
    second = langsmith_run.sync_dataset(client, cases)
    assert second == {"dataset_id": "ds1", "created": 0, "updated": 0, "unchanged": 2}
    assert client.calls["create_examples"] == 1


def test_sync_updates_changed_case() -> None:
    client = FakeClient()
    langsmith_run.sync_dataset(client, [make_case("a")])
    changed = make_case("a", note="改了")
    result = langsmith_run.sync_dataset(client, [changed])
    assert result["updated"] == 1
    stored = client.examples[str(langsmith_run.example_id("a"))]
    assert stored.metadata["case_hash"] == case_hash(changed)


# ---- 第二步：Trace 计数 ----

def test_count_trace() -> None:
    tree = node("agent", "tool", [
        node("execute_sql", "tool", [node("execute_sql", "tool", None)]),
        node("llm-1", "llm", [node("chat", "llm", None), node("chat", "llm", None)]),
    ])
    assert langsmith_run.count_trace(tree) == {"execute_sql": 2, "llm": 3}


# ---- 第三步：评分 ----

def test_feedback_pass() -> None:
    result = {"case_id": "a", "run_index": 0, "passed": True, "invalid": False,
              "first_failed_stage": None, "redlines": [],
              "turns": [{"question": "你好", "outcome": "answered", "passed": True,
                         "first_failed_stage": None, "meta": {},
                         "checks": [make_check("route", "router", True),
                                    make_check("cost", "turn", None, gating=False)]}]}
    scores = langsmith_run.feedback(result, {"execute_sql": 1, "llm": 3})
    assert [item["key"] for item in scores] == ["passed", "redline_violations", "route",
                                                "trace_execute_sql", "trace_llm_calls"]
    by_key = {item["key"]: item for item in scores}
    assert by_key["passed"]["score"] == 1
    assert by_key["trace_llm_calls"]["score"] == 3


def test_feedback_fail_and_redline() -> None:
    result = {"case_id": "a", "run_index": 0, "passed": False, "invalid": False,
              "first_failed_stage": "sql", "redlines": [{"turn": 0, "check": "db_orders", "detail": "d"}],
              "turns": [{"question": "第一轮", "checks": [make_check("sql_rows", "sql", True)]},
                        {"question": "第二轮", "checks": [make_check("sql_rows", "sql", False, "缺 x")]}]}
    by_key = {item["key"]: item for item in langsmith_run.feedback(result, {"execute_sql": 0, "llm": 1})}
    assert by_key["passed"]["score"] == 0
    assert by_key["passed"]["comment"] == "第一处出错的环节: sql"
    assert by_key["redline_violations"]["score"] == 1
    assert by_key["redline_violations"]["comment"] == "db_orders: d"
    assert by_key["sql_rows"]["score"] == 0
    assert by_key["sql_rows"]["comment"] == "缺 x"


# ---- 第三步：实验 ----

def test_execute_langsmith_assigns_run_index(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run_case(case: Case, run_index: int, hash_value: str) -> dict:
        return {"case_id": case.id, "case_hash": hash_value, "run_index": run_index, "turns": []}

    def fake_evaluate_run(case: Case, case_run: dict, judge: bool = True, votes: int = 3) -> dict:
        return {"case_id": case.id, "run_index": case_run["run_index"], "passed": True, "invalid": False,
                "first_failed_stage": None, "redlines": [], "turns": []}

    monkeypatch.setattr(langsmith_run, "run_case", fake_run_case)
    monkeypatch.setattr(langsmith_run, "evaluate_run", fake_evaluate_run)
    monkeypatch.setattr(langsmith_run, "IndexStore", lambda *a, **kw: None)
    monkeypatch.setattr(langsmith_run, "cleanup_sessions", lambda: {})

    client = FakeClient()
    runs, results, info = langsmith_run.execute_langsmith(
        [make_case("a"), make_case("b")], 2, 1, False, 1, "r1", STATE, client=client)

    pairs = [(run["case_id"], run["run_index"]) for run in runs]
    assert len(runs) == 4
    assert pairs == [("a", 0), ("a", 1), ("b", 0), ("b", 1)]
    assert len(results) == 4
    assert [(item["case_id"], item["run_index"]) for item in results] == pairs
    assert info["experiment"] == "exp-1"
    assert info["url"] == "http://x"
    assert info["rows"] == 4
    assert info["created"] == 2
