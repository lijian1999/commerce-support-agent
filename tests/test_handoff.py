"""app/handoff.py 的测试：纯函数逻辑、假模型写摘要、真实数据库写工单。"""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pymysql
import pytest
from langchain_core.messages import AIMessage, HumanMessage

import app.handoff
from app.context import AppContext
from app.db import connect
from app.handoff import (
    current_trace_id,
    extract_order_ids,
    handoff_node,
    new_ticket_id,
    raw_transcript,
    resolve_trigger,
)
from app.models import ModelError, ModelResult, ModelUnavailable
from app.state import State, make_task_result


# ---------------------------------------------------------------- 公共工具

def hm(text: str) -> HumanMessage:
    return HumanMessage(content=text)


def am(text: str) -> AIMessage:
    return AIMessage(content=text)


def state_of(messages: list, **fields) -> State:
    state: State = {"messages": messages}  # type: ignore[typeddict-item]
    state.update(fields)  # type: ignore[arg-type]
    return state


def runtime_of(customer_id: str | None = "CUST-001") -> SimpleNamespace:
    return SimpleNamespace(context=AppContext(customer_id=customer_id, session_id="s-test"))


def read_ticket(ticket_id: str) -> dict:
    """用 root 账号读一张工单。"""
    conn = connect("root")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM handoff_tickets WHERE ticket_id = %s", (ticket_id,))
            return cur.fetchone()
    finally:
        conn.close()


class FakeModel:
    """假的 RoleModel：invoke 返回预设结果或抛出预设异常。"""

    def __init__(self, result: ModelResult | None, error: ModelError | None) -> None:
        self._result = result
        self._error = error

    def invoke(self, messages: list) -> ModelResult:
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


def install_model(monkeypatch: pytest.MonkeyPatch, *, text: str = "摘要文本",
                  error: ModelError | None = None) -> list[dict]:
    """把 app.handoff.get_model 换成假的，记录每次调用的参数。"""
    calls: list[dict] = []

    def fake_get_model(role: str, schema=None, fault: str = "off"):
        calls.append({"role": role, "schema": schema, "fault": fault})
        return FakeModel(None if error else ModelResult(text, []), error)

    monkeypatch.setattr(app.handoff, "get_model", fake_get_model)
    return calls


@pytest.fixture(autouse=True)
def clean_tickets():
    """每个测试之后清掉测试造的工单，不给数据库留数据。"""
    yield
    conn = connect("root")
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM handoff_tickets WHERE session_id = %s", ("s-test",))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- 第一步的纯函数

def test_resolve_trigger_keeps_existing_handoff() -> None:
    state = state_of([hm("随便")], handoff={"reason": "system_failure", "detail": "回答模型不可用"},
                     route="sql")
    assert resolve_trigger(state) == {"reason": "system_failure", "detail": "回答模型不可用"}


def test_resolve_trigger_from_router_handoff() -> None:
    asked = state_of([hm("帮我转人工")], route="handoff", route_reason="用户明确要求人工")
    assert resolve_trigger(asked) == {"reason": "user_request", "detail": "用户明确要求人工"}

    complained = state_of([hm("我要投诉，你们发错货了")], route="handoff", route_reason="投诉纠纷")
    assert resolve_trigger(complained) == {"reason": "out_of_scope", "detail": "投诉纠纷"}


def test_resolve_trigger_plan_errors() -> None:
    state = state_of([hm("查一下")], route="supervisor",
                     plan_errors=["存在循环依赖: t1 -> t2 -> t1"])
    got = resolve_trigger(state)
    assert got["reason"] == "system_failure"
    assert got["detail"] == "执行计划没有通过校验: 存在循环依赖: t1 -> t2 -> t1"


def test_resolve_trigger_failed_tasks() -> None:
    task = {"id": "t1", "agent": "sql", "goal": "查订单", "depends_on": []}
    state = state_of([hm("查一下")], route="sql",
                     task_results={"t1": make_task_result(task, "failed", error="数据库查询失败")})
    assert resolve_trigger(state)["detail"] == "任务全部失败: t1（数据库查询失败）"


def test_raw_transcript() -> None:
    state = state_of([hm("我的订单到哪了"), am("已发货"), hm("帮我转人工")])
    assert raw_transcript(state) == "用户: 我的订单到哪了\n客服: 已发货\n用户: 帮我转人工"

    single = state_of([hm("帮我转人工")])
    assert raw_transcript(single) == "用户: 帮我转人工"


def test_extract_order_ids_dedup_keeps_first_order() -> None:
    state = state_of([hm("ord-2025-0031 和 ORD-2024-0089 一起看"), hm("再看一下 ORD-2025-0031")])
    assert extract_order_ids(state) == ["ORD-2025-0031", "ORD-2024-0089"]


def test_new_ticket_id_format_and_unique() -> None:
    first = new_ticket_id("2025-10-20")
    assert re.match(r"^TKT-20251020-[0-9A-F]{6}$", first)
    assert new_ticket_id("2025-10-20") != first


# ---------------------------------------------------------------- handoff_node

def test_user_request_writes_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install_model(monkeypatch, text="用户反馈订单有问题，要求人工处理。")
    state = state_of([hm("订单 ORD-2025-0031 有问题，帮我转人工")],
                     route="handoff", route_reason="用户明确要求人工")
    update = handoff_node(state, runtime_of())

    assert len(calls) == 1
    assert calls[0]["role"] == "answer"
    assert calls[0]["schema"] is None

    step = update["steps"][0]
    assert step["name"] == "handoff"
    assert step["kind"] == "model"
    assert "摘要来源: 模型" in step["summary"]

    ticket = update["handoff"]
    row = read_ticket(ticket["ticket_id"])
    assert row["reason"] == "user_request"
    assert row["summary"] == "用户反馈订单有问题，要求人工处理。"
    assert row["order_ids"] == "ORD-2025-0031"
    assert row["customer_id"] == "CUST-001"
    assert row["session_id"] == "s-test"

    assert update["card"]["type"] == "ticket"
    assert update["card"]["reason_label"] == "用户要求人工"
    assert ticket["ticket_id"] in update["answer"]

    assert len(update["messages"]) == 1
    assert isinstance(update["messages"][0], AIMessage)
    assert update["messages"][0].content == update["answer"]


def test_system_failure_does_not_call_model(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install_model(monkeypatch, text="不该被调用")
    state = state_of([hm("帮我查订单"), am("好的"), hm("再查一次")],
                     handoff={"reason": "system_failure", "detail": "回答模型不可用"})
    update = handoff_node(state, runtime_of())

    assert calls == []
    step = update["steps"][0]
    assert step["kind"] == "code"
    assert "摘要来源: 原始对话" in step["summary"]
    assert read_ticket(update["handoff"]["ticket_id"])["summary"] == raw_transcript(state)


def test_summary_model_unavailable_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    install_model(monkeypatch, error=ModelUnavailable("x", ["主模型第 1 次调用失败"]))
    state = state_of([hm("帮我转人工")], route="handoff", route_reason="用户明确要求人工")
    update = handoff_node(state, runtime_of())

    ticket = update["handoff"]
    assert read_ticket(ticket["ticket_id"])["summary"] == raw_transcript(state)
    step = update["steps"][0]
    assert step["kind"] == "model"
    assert step["notes"] == ["主模型第 1 次调用失败"]


def test_anonymous_customer(monkeypatch: pytest.MonkeyPatch) -> None:
    install_model(monkeypatch)
    state = state_of([hm("帮我转人工")], route="handoff", route_reason="用户明确要求人工")
    update = handoff_node(state, runtime_of(customer_id=None))

    assert read_ticket(update["handoff"]["ticket_id"])["customer_id"] is None


def test_db_error_is_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_connect(account: str):
        raise pymysql.err.OperationalError(2003, "down")

    install_model(monkeypatch)
    monkeypatch.setattr(app.handoff, "connect", broken_connect)
    state = state_of([hm("帮我转人工")], route="handoff", route_reason="用户明确要求人工")
    update = handoff_node(state, runtime_of())

    assert update["card"] is None
    assert update["answer"].startswith("系统繁忙")
    assert update["steps"][0]["summary"].startswith("写工单失败")


def test_step_detail_is_json_serializable(monkeypatch: pytest.MonkeyPatch) -> None:
    install_model(monkeypatch, text="用户反馈订单有问题，要求人工处理。")
    state = state_of([hm("订单 ORD-2025-0031 有问题，帮我转人工")],
                     route="handoff", route_reason="用户明确要求人工")
    update = handoff_node(state, runtime_of())

    detail = update["steps"][0]["detail"]
    assert json.dumps(detail, ensure_ascii=False)


def test_no_tools_bound() -> None:
    source = (Path(app.handoff.__file__)).read_text(encoding="utf-8")
    assert "bind_tools" not in source
    assert "execute_sql" not in source
