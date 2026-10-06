"""SQL Agent 子图的单元测试：不调用真实模型和数据库，全靠 monkeypatch 替换。"""
import json
import types

import pymysql

from app.context import AppContext
from app.models import ModelResult, ModelUnavailable
from app.sql_agent import graph
from app.sql_agent.plan import QueryPlan

TASK: dict = {"id": "t1", "agent": "sql", "goal": "查最近一个订单", "depends_on": []}
DATA_OK = {"columns": ["orders.order_id"], "rows": [{"orders.order_id": "ORD-1"}], "row_count": 1}


def make_plan(select: list[str], *, answerable: bool = True, reason: str = "") -> QueryPlan:
    return QueryPlan(reasoning="测试计划", answerable=answerable, unanswerable_reason=reason,
                     select=select, metrics=[], group_by=[], filters=[], order_by=[], limit=1)


VALID = make_plan(["orders.order_id"])
INVALID = make_plan(["orders.customer_id"])


class _Recorder:
    """记录模型或数据库每次收到的输入。"""

    def __init__(self) -> None:
        self.calls: list = []


class _FakeModel:
    def __init__(self, outcomes: list, recorder: _Recorder) -> None:
        self.outcomes = outcomes
        self.recorder = recorder

    def invoke(self, messages: list) -> ModelResult:
        self.recorder.calls.append([tuple(message) for message in messages])
        if not self.outcomes:
            raise AssertionError("模型调用次数超过预期")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _FakeFactory:
    """假的 get_model：每次调用返回消费一个预置结果的模型。"""

    def __init__(self, outcomes: list, recorder: _Recorder) -> None:
        self.outcomes = outcomes
        self.recorder = recorder

    def __call__(self, role: str, schema=None, fault: str = "off") -> _FakeModel:
        assert role == "sql_planner" and fault == "off", (role, fault)
        return _FakeModel(self.outcomes, self.recorder)


def _fake_execute(result, recorder: _Recorder):
    def run(sql: str, params: list) -> dict:
        recorder.calls.append((sql, list(params)))
        if isinstance(result, BaseException):
            raise result
        return result
    return run


def run_subgraph(monkeypatch, model_results: list, exec_result=DATA_OK,
                 customer_id: str | None = "CUST-001", question: str = TASK["goal"],
                 history: str = "", upstream: dict | None = None):
    """用假模型和假数据库跑一遍子图，返回最终状态、模型调用记录、SQL 执行记录。"""
    model_recorder = _Recorder()
    exec_recorder = _Recorder()
    monkeypatch.setattr(graph, "get_model", _FakeFactory(model_results, model_recorder))
    monkeypatch.setattr(graph, "execute_sql", _fake_execute(exec_result, exec_recorder))
    out = graph.build_sql_graph().invoke(
        {"task": TASK, "question": question, "history": history,
         "upstream": upstream or {}, "query_plan": None, "plan_error": None, "attempts": 0,
         "compiled": None, "result": None, "steps": None},
        context=AppContext(customer_id=customer_id),
    )
    return out, model_recorder, exec_recorder


def test_normal_path(monkeypatch):
    out, model_recorder, exec_recorder = run_subgraph(monkeypatch, [ModelResult(value=VALID, notes=[])])
    assert out["result"]["status"] == "ok"
    names = [step["name"] for step in out["steps"]]
    kinds = [step["kind"] for step in out["steps"]]
    assert names == ["t1/sql_plan", "t1/compile", "t1/execute"]
    assert kinds == ["model", "code", "code"]
    assert len(exec_recorder.calls) == 1
    assert exec_recorder.calls[0][1][0] == "CUST-001"
    assert len(model_recorder.calls) == 1


def test_fix_once_then_ok(monkeypatch):
    out, model_recorder, exec_recorder = run_subgraph(
        monkeypatch, [ModelResult(value=INVALID, notes=[]), ModelResult(value=VALID, notes=[])])
    assert out["result"]["status"] == "ok"
    names = [step["name"] for step in out["steps"]]
    assert names == ["t1/sql_plan", "t1/compile", "t1/sql_plan", "t1/compile", "t1/execute"]
    assert len(model_recorder.calls) == 2
    user_message = model_recorder.calls[1][1][1]
    assert "上一次的查询计划被拒绝了" in user_message
    assert "orders.customer_id" in user_message
    assert len(exec_recorder.calls) == 1


def test_invalid_twice(monkeypatch):
    out, model_recorder, exec_recorder = run_subgraph(
        monkeypatch, [ModelResult(value=INVALID, notes=[]), ModelResult(value=INVALID, notes=[])])
    assert out["result"]["status"] == "failed"
    assert out["result"]["error_kind"] == "invalid_plan"
    assert len(model_recorder.calls) == 2
    assert exec_recorder.calls == []


def test_anonymous_needs_login(monkeypatch):
    out, model_recorder, exec_recorder = run_subgraph(
        monkeypatch, [ModelResult(value=VALID, notes=[])], customer_id=None)
    assert out["result"]["status"] == "failed"
    assert out["result"]["error_kind"] == "login_required"
    assert len(model_recorder.calls) == 1
    assert exec_recorder.calls == []


def test_unanswerable(monkeypatch):
    plan = make_plan(["orders.order_id"], answerable=False, reason="需要子查询")
    out, model_recorder, exec_recorder = run_subgraph(monkeypatch, [ModelResult(value=plan, notes=[])])
    assert out["result"]["error_kind"] == "unanswerable"
    assert "需要子查询" in out["result"]["error"]
    assert len(model_recorder.calls) == 1
    assert exec_recorder.calls == []


def test_model_unavailable(monkeypatch):
    out, _, exec_recorder = run_subgraph(
        monkeypatch, [ModelUnavailable("x", ["主模型第 1 次调用失败"])])
    assert out["result"]["error_kind"] == "model_error"
    assert len(out["steps"]) == 1
    assert out["steps"][0]["name"] == "t1/sql_plan"
    assert out["steps"][0]["notes"] == ["主模型第 1 次调用失败"]
    assert exec_recorder.calls == []


def test_db_error(monkeypatch):
    boom = pymysql.err.OperationalError(1205, "timeout")
    out, _, exec_recorder = run_subgraph(monkeypatch, [ModelResult(value=VALID, notes=[])],
                                         exec_result=boom)
    assert out["result"]["status"] == "failed"
    assert out["result"]["error_kind"] == "db_error"
    assert len(exec_recorder.calls) == 1


def test_zero_rows(monkeypatch):
    empty = {"columns": [], "rows": [], "row_count": 0}
    out, _, _ = run_subgraph(monkeypatch, [ModelResult(value=VALID, notes=[])], exec_result=empty)
    assert out["result"]["status"] == "ok"
    assert out["steps"][-1]["summary"] == "0 行"


def test_model_retry_notes_in_steps(monkeypatch):
    notes = ["主模型第 1 次调用失败", "主模型调用成功"]
    out, _, _ = run_subgraph(monkeypatch, [ModelResult(value=VALID, notes=notes)])
    plan_step = [step for step in out["steps"] if step["name"] == "t1/sql_plan"][0]
    assert plan_step["notes"] == notes


def test_sql_agent_node(monkeypatch):
    exec_recorder = _Recorder()
    model_recorder = _Recorder()
    monkeypatch.setattr(graph, "get_model", _FakeFactory([ModelResult(value=VALID, notes=[])],
                                                         model_recorder))
    monkeypatch.setattr(graph, "execute_sql", _fake_execute(DATA_OK, exec_recorder))
    payload = {"task": TASK, "question": TASK["goal"], "history": "", "upstream": {}}
    runtime = types.SimpleNamespace(context=AppContext(customer_id="CUST-001"))
    out = graph.sql_agent_node(payload, runtime)
    assert set(out.keys()) == {"task_results"}
    assert out["task_results"]["t1"]["status"] == "ok"


def test_build_user_message():
    only_question = graph.build_user_message(
        {"task": TASK, "question": TASK["goal"], "history": "", "upstream": {}, "plan_error": None})
    assert only_question == f"用户的问题：{TASK['goal']}"
    full = graph.build_user_message({
        "task": {"id": "t1", "agent": "sql", "goal": "查键盘消费", "depends_on": []},
        "question": "我买过什么？", "history": "用户: 你好",
        "upstream": {"t0": {"task_id": "t0", "agent": "sql", "goal": "g", "status": "ok",
                            "error_kind": None, "error": None, "data": {}}},
        "plan_error": "不存在的列", "query_plan": {"select": ["orders.order_id"]},
    })
    positions = [full.index(marker) for marker in
                 ("最近的对话：", "用户的问题：", "本次任务：", "前置任务的结果：",
                  "上一次的查询计划被拒绝了：")]
    assert positions == sorted(positions)


def test_step_detail_is_json_serializable(monkeypatch):
    out, _, _ = run_subgraph(monkeypatch, [ModelResult(value=VALID, notes=[])])
    assert out["result"]["status"] == "ok"
    for step in out["steps"]:
        json.dumps(step["detail"], ensure_ascii=False)
