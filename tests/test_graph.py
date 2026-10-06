"""主图与运行入口的测试：模型全部是假的，数据库是真的，检索是假的。"""
from __future__ import annotations

import builtins
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

import app.answer as answer_module
import app.handoff as handoff_module
import app.rag.retrieval as retrieval_module
import app.router as router_module
import app.sql_agent.graph as sql_graph_module
import app.supervisor.nodes as supervisor_module
import cli
from app.answer import LOGIN_REPLY
from app.context import AppContext
from app.db import connect
from app.graph import build_graph
from app.models import ModelResult, ModelUnavailable
from app.router import RouteDecision
from app.runner import make_context, run_turn
from app.sql_agent.plan import QueryPlan
from app.supervisor.plan import ExecutionPlan, PlanTask
from cli import format_preview, format_step

# ---------------------------------------------------------------- 假模型、假检索

MODULES = (router_module, supervisor_module, sql_graph_module, answer_module, handoff_module)


class _FakeRole:
    """替某个角色的模型：invoke 时从队列里弹下一个结果，并记下收到的消息。"""

    def __init__(self, models: "FakeModels", role: str):
        self.models, self.role = models, role

    def invoke(self, messages: list) -> ModelResult:
        self.models.calls.append((self.role, list(messages)))
        queue = self.models.queues.setdefault(self.role, [])
        if not queue:
            raise AssertionError(f"没有为角色 {self.role} 预置结果")
        value = queue.pop(0)
        if isinstance(value, BaseException):
            raise value
        return ModelResult(value=value, notes=[])


class FakeModels:
    """五个模块共用的假模型工厂：每个角色一个结果队列，同时记录所有调用。"""

    def __init__(self, router=None, planner=None, sql_planner=None, answer=None):
        self.queues: dict[str, list] = {
            "router": list(router or []), "planner": list(planner or []),
            "sql_planner": list(sql_planner or []), "answer": list(answer or []),
        }
        self.calls: list[tuple[str, list]] = []
        self.queries: list[str] = []     # 假检索收到的检索词

    def factory(self, role: str, schema=None, fault: str = "off") -> _FakeRole:
        return _FakeRole(self, role)

    def install(self, monkeypatch) -> None:
        """把 5 个模块里的 get_model 换成自己的 factory，检索也换成假的。"""
        for module in MODULES:
            monkeypatch.setattr(module, "get_model", self.factory)
        monkeypatch.setattr(retrieval_module, "embed_query", self._embed)
        monkeypatch.setattr(retrieval_module, "IndexStore", _FakeStore)

    def _embed(self, text: str) -> list[float]:
        self.queries.append(text)
        return [0.1] * 8

    def roles(self) -> list[str]:
        """调用过的模型角色，按调用顺序。"""
        return [role for role, _ in self.calls]

    def times(self, role: str) -> int:
        return sum(1 for called, _ in self.calls if called == role)


class _FakeStore:
    """假的索引：配置检查什么都不做，检索固定返回一条退货政策。"""

    def __init__(self, *args, **kwargs):
        pass

    def check_runtime(self, version: str, cfg) -> None:
        return None

    def query(self, version: str, embedding: list, top_k: int) -> list[dict]:
        return [{"doc": "policies/return_policy.md", "section": "Return Windows",
                 "text": "30 days", "score": 0.9}]


# ---------------------------------------------------------------- 公共工具

GRAPH_SESSION = "s-graph"
CANCELLED_ORDER = "ORD-2025-0038"

LEGAL_PLAN = QueryPlan(reasoning="", answerable=True, unanswerable_reason="",
                       select=["orders.order_id", "orders.status"], metrics=[], group_by=[],
                       filters=[], order_by=[{"key": "orders.order_date", "direction": "desc"}],
                       limit=1)
BAD_PLAN = QueryPlan(reasoning="", answerable=True, unanswerable_reason="",
                     select=["orders.customer_id"], metrics=[], group_by=[], filters=[],
                     order_by=[], limit=1)

# 合法的执行计划 A：t1 先查订单，t2 依赖 t1 查保修期，t3 独立查退货政策
PLAN_A = ExecutionPlan(reasoning="r", tasks=[
    PlanTask(id="t1", agent="sql", goal="查最近一个订单", depends_on=[]),
    PlanTask(id="t2", agent="rag", goal="查这个订单里商品的保修期", depends_on=["t1"]),
    PlanTask(id="t3", agent="rag", goal="退货政策", depends_on=[]),
])
# 有环的执行计划 B：t1 和 t2 互相依赖
PLAN_B = ExecutionPlan(reasoning="r", tasks=[
    PlanTask(id="t1", agent="sql", goal="查订单", depends_on=["t2"]),
    PlanTask(id="t2", agent="sql", goal="再查一个订单", depends_on=["t1"]),
])

ROUTE_SQL = RouteDecision(reason="查订单", route="sql")
ROUTE_RAG = RouteDecision(reason="查文档", route="rag")
ROUTE_SUPERVISOR = RouteDecision(reason="两边都要查", route="supervisor")
ROUTE_CANCEL = RouteDecision(reason="取消订单", route="cancel")
ROUTE_HANDOFF = RouteDecision(reason="要人工", route="handoff")
ROUTE_DIRECT = RouteDecision(reason="闲聊", route="direct")


@pytest.fixture(autouse=True)
def reset_databases():
    """每个测试跑完把动过的订单和工单恢复原样。"""
    yield
    conn = connect("root")
    try:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE orders SET status = 'Processing', cancelled_at = NULL "
                        f"WHERE order_id = %s", [CANCELLED_ORDER])
            cur.execute("DELETE FROM order_cancellations WHERE order_id = %s", [CANCELLED_ORDER])
            cur.execute("DELETE FROM handoff_tickets WHERE session_id = %s", [GRAPH_SESSION])
        conn.commit()
    finally:
        conn.close()


def context_of(customer_id: str | None = "CUST-002") -> AppContext:
    return AppContext(customer_id=customer_id, index_version="v1", session_id=GRAPH_SESSION)


def new_graph():
    return build_graph(checkpointer=InMemorySaver())


def run(graph, ctx, thread, text=None, resume=None) -> tuple[list[str], list[dict], dict]:
    """跑一轮，把事件收成：步骤名列表、中断的预览列表、结束时的状态。"""
    names: list[str] = []
    interrupts: list[dict] = []
    final: dict = {}
    for kind, payload in run_turn(graph, ctx, thread, text, resume):
        if kind == "step":
            names.append(str(payload["name"]))
        elif kind == "interrupt":
            interrupts.append(payload)
        else:
            final = payload
    assert final, "没有 final 事件"
    return names, interrupts, final


def run_events(graph, ctx, thread, text=None, resume=None) -> list[tuple[str, Any]]:
    return list(run_turn(graph, ctx, thread, text, resume))


def user_text(fake: FakeModels, role: str, index: int = 0) -> str:
    """某个角色第 index 次调用模型时收到的用户消息文本。"""
    sent = [messages for called, messages in fake.calls if called == role]
    assert len(sent) > index, f"角色 {role} 只被调用了 {len(sent)} 次"
    for item in sent[index]:
        if isinstance(item, tuple):
            if item[0] == "user":
                return str(item[1])
        elif isinstance(item, HumanMessage):
            return str(item.content)
    raise AssertionError(f"角色 {role} 的第 {index} 次调用里没有用户消息")


def order_status(order_id: str) -> str:
    conn = connect("root")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM orders WHERE order_id = %s", [order_id])
            row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None, f"数据库里没有订单 {order_id}"
    return str(row["status"])


def handoff_ticket_count(session_id: str) -> int:
    conn = connect("root")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM handoff_tickets WHERE session_id = %s", [session_id])
            row = cur.fetchone()
    finally:
        conn.close()
    return int(row["n"])


# ---------------------------------------------------------------- 结构

def test_graph_has_exactly_thirteen_nodes():
    nodes = {name for name in build_graph().get_graph().nodes if not name.startswith("__")}
    assert nodes == {"router", "planner", "validate", "scheduler", "sql_agent", "rag_agent",
                     "answer", "handoff", "cancel_extract", "cancel_check", "cancel_confirm",
                     "cancel_execute", "cancel_reply"}


def test_cancel_execute_has_single_incoming_edge():
    edges = build_graph().get_graph().edges
    sources = sorted({edge.source for edge in edges if edge.target == "cancel_execute"})
    assert sources == ["cancel_confirm"]


def test_no_route_from_business_flow_to_cancel_nodes():
    adjacency: dict[str, list[str]] = {}
    for edge in build_graph().get_graph().edges:
        adjacency.setdefault(edge.source, []).append(edge.target)
    for start in ("sql_agent", "rag_agent", "planner", "validate", "scheduler", "answer", "handoff"):
        seen = {start}
        stack = [start]
        while stack:
            current = stack.pop()
            assert not current.startswith("cancel_"), f"{start} 能走到 {current}"
            for nxt in adjacency.get(current, []):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)


def test_no_bind_tools_anywhere_in_app():
    for path in Path("app").rglob("*.py"):
        assert "bind_tools" not in path.read_text(encoding="utf-8"), path


# ---------------------------------------------------------------- 流程

def test_single_domain_sql(monkeypatch):
    fake = FakeModels(router=[ROUTE_SQL], sql_planner=[LEGAL_PLAN], answer=["您的订单已发货。"])
    fake.install(monkeypatch)
    names, interrupts, final = run(new_graph(), context_of(), "t-sql", "我最近一个订单到哪了？")
    assert names == ["router", "t1/sql_plan", "t1/compile", "t1/execute", "answer"]
    assert interrupts == []
    assert final["answer"] == "您的订单已发货。"
    assert final["task_results"]["t1"]["data"]["row_count"] == 1
    assert fake.roles() == ["router", "sql_planner", "answer"]


def test_single_domain_rag(monkeypatch):
    fake = FakeModels(router=[ROUTE_RAG], answer=["支持 30 天退货。"])
    fake.install(monkeypatch)
    names, interrupts, final = run(new_graph(), context_of(), "t-rag", "退货政策是什么？")
    assert names == ["router", "t1/retrieve", "answer"]
    assert final["sources"] == ["policies/return_policy.md"]
    assert fake.roles() == ["router", "answer"]


def test_cross_domain(monkeypatch):
    fake = FakeModels(router=[ROUTE_SUPERVISOR], planner=[PLAN_A], sql_planner=[LEGAL_PLAN],
                      answer=["您的订单是 Processing，退货政策 30 天。"])
    fake.install(monkeypatch)
    names, interrupts, final = run(new_graph(), context_of(), "t-multi", "最近一个订单的情况，另外退货政策？")
    assert names[:4] == ["router", "planner", "validate", "scheduler"]
    assert names[-4:] == ["scheduler", "t2/retrieve", "scheduler", "answer"]
    assert sorted(names[4:-4]) == ["t1/compile", "t1/execute", "t1/sql_plan", "t3/retrieve"]
    assert fake.times("planner") == 1
    assert any(text.startswith("查这个订单里商品的保修期") and "ORD-2025-0031" in text
               for text in fake.queries), fake.queries
    assert all(result["status"] == "ok" for result in final["task_results"].values())


def test_two_invalid_plans_handoff(monkeypatch):
    fake = FakeModels(router=[ROUTE_SUPERVISOR], planner=[PLAN_B, PLAN_B], answer=["用不上"])
    fake.install(monkeypatch)
    names, interrupts, final = run(new_graph(), context_of(), "t-badplan", "两件事都查一下")
    assert names == ["router", "planner", "validate", "planner", "validate", "handoff"]
    assert final["handoff"]["reason"] == "system_failure"
    assert final["card"]["type"] == "ticket"
    assert fake.times("planner") == 2
    assert fake.times("answer") == 0


def test_second_plan_after_rejection(monkeypatch):
    fake = FakeModels(router=[ROUTE_SUPERVISOR], planner=[PLAN_B, PLAN_A], sql_planner=[LEGAL_PLAN],
                      answer=["都查到了。"])
    fake.install(monkeypatch)
    names, interrupts, final = run(new_graph(), context_of(), "t-fixplan", "两件事都查一下")
    assert names[:5] == ["router", "planner", "validate", "planner", "validate"]
    assert names[-1] == "answer"
    assert "上一次的执行计划没有通过校验" in user_text(fake, "planner", 1)


def test_all_tasks_failed_handoff(monkeypatch):
    fake = FakeModels(router=[ROUTE_SQL], sql_planner=[BAD_PLAN, BAD_PLAN])
    fake.install(monkeypatch)
    names, interrupts, final = run(new_graph(), context_of(), "t-fail", "查一下我的订单")
    assert names[-1] == "handoff"
    assert "t1/execute" not in names and "answer" not in names
    assert final["handoff"]["reason"] == "system_failure"


def test_anonymous_login_required(monkeypatch):
    fake = FakeModels(router=[ROUTE_SQL], sql_planner=[LEGAL_PLAN], answer=["用不上"])
    fake.install(monkeypatch)
    names, interrupts, final = run(new_graph(), context_of(None), "t-anon", "我最近一个订单到哪了？")
    assert names == ["router", "t1/sql_plan", "t1/compile", "answer"]
    assert final["answer"] == LOGIN_REPLY
    assert fake.times("answer") == 0
    assert handoff_ticket_count(GRAPH_SESSION) == 0


def test_direct_chat(monkeypatch):
    fake = FakeModels(router=[ROUTE_DIRECT], answer=["你好，有什么可以帮您。"])
    fake.install(monkeypatch)
    names, interrupts, final = run(new_graph(), context_of(), "t-direct", "你好")
    assert names == ["router", "answer"]


def test_router_handoff_request(monkeypatch):
    summary = "用户想查订单又想要人工客服。"
    fake = FakeModels(router=[ROUTE_HANDOFF], answer=[summary])
    fake.install(monkeypatch)
    names, interrupts, final = run(new_graph(), context_of(), "t-user-handoff", "帮我转人工")
    assert names == ["router", "handoff"]
    assert final["handoff"]["reason"] == "user_request"
    assert final["handoff"]["ticket_id"] in final["answer"]


def test_router_model_unavailable(monkeypatch):
    fake = FakeModels(router=[ModelUnavailable("x", ["n1"])], answer=["用不上"])
    fake.install(monkeypatch)
    names, interrupts, final = run(new_graph(), context_of(), "t-router-down", "我最近一个订单到哪了？")
    assert names == ["router", "handoff"]
    assert final["handoff"]["reason"] == "system_failure"
    assert fake.times("answer") == 0


def test_answer_model_unavailable(monkeypatch):
    fake = FakeModels(router=[ROUTE_SQL], sql_planner=[LEGAL_PLAN],
                      answer=[ModelUnavailable("x", [])])
    fake.install(monkeypatch)
    names, interrupts, final = run(new_graph(), context_of(), "t-answer-down", "我最近一个订单到哪了？")
    assert names == ["router", "t1/sql_plan", "t1/compile", "t1/execute", "answer", "handoff"]


def test_cancel_order_confirmed(monkeypatch):
    fake = FakeModels(router=[ROUTE_CANCEL])
    fake.install(monkeypatch)
    graph, thread = new_graph(), "t-cancel-yes"
    text = "帮我取消订单 ORD-2025-0038"
    names, interrupts, final = run(graph, context_of(), thread, text)
    assert names == ["router", "cancel/extract", "cancel/check"]
    assert len(interrupts) == 1
    assert interrupts[0]["order_id"] == CANCELLED_ORDER
    names, interrupts, final = run(graph, context_of(), thread,
                                   resume={"confirm": True, "order_id": CANCELLED_ORDER})
    assert names == ["cancel/confirm", "cancel/execute", "cancel/reply"]
    assert final["answer"] == "订单 ORD-2025-0038 已取消。"
    assert order_status(CANCELLED_ORDER) == "Cancelled"
    assert fake.roles() == ["router"]


def test_cancel_order_declined(monkeypatch):
    fake = FakeModels(router=[ROUTE_CANCEL])
    fake.install(monkeypatch)
    graph, thread = new_graph(), "t-cancel-no"
    text = "帮我取消订单 ORD-2025-0038"
    names, interrupts, final = run(graph, context_of(), thread, text)
    assert len(interrupts) == 1
    names, interrupts, final = run(graph, context_of(), thread,
                                   resume={"confirm": False, "order_id": CANCELLED_ORDER})
    assert names == ["cancel/confirm", "cancel/reply"]
    assert order_status(CANCELLED_ORDER) == "Processing"


def test_second_turn_remembers_history(monkeypatch):
    fake = FakeModels(router=[ROUTE_DIRECT, ROUTE_DIRECT], answer=["你好！", "有什么可以帮您。"])
    fake.install(monkeypatch)
    graph, thread, ctx = new_graph(), "t-history", context_of()
    run(graph, ctx, thread, "你好")
    names, interrupts, final = run(graph, ctx, thread, "那退货政策呢？")
    assert names == ["router", "answer"]
    assert len(final["messages"]) == 4
    assert "最近的对话：" in user_text(fake, "router", 1)
    assert "你好" in user_text(fake, "router", 1)


def test_second_turn_resets_per_turn_fields(monkeypatch):
    fake = FakeModels(router=[ROUTE_SQL, ROUTE_DIRECT], sql_planner=[LEGAL_PLAN],
                      answer=["您的订单处理中。", "好的。"])
    fake.install(monkeypatch)
    graph, thread, ctx = new_graph(), "t-reset", context_of()
    run(graph, ctx, thread, "我最近一个订单到哪了？")
    names, interrupts, final = run(graph, ctx, thread, "你好")
    assert final["task_results"] == {}
    assert final["plan"] is None
    assert final["route"] == "direct"


def test_run_turn_events(monkeypatch):
    fake = FakeModels(router=[ROUTE_SQL], sql_planner=[LEGAL_PLAN], answer=["您的订单处理中。"])
    fake.install(monkeypatch)
    events = run_events(new_graph(), context_of(), "t-events", "我最近一个订单到哪了？")
    assert events[-1][0] == "final"
    assert all(kind == "step" for kind, _ in events[:-1])
    for _, step in events[:-1]:
        assert set(step) == {"name", "kind", "summary", "detail", "notes"}


# ---------------------------------------------------------------- 命令行的格式函数

def test_format_step_single_line():
    step = {"name": "router", "kind": "model", "summary": "route=sql   理由: x",
            "detail": None, "notes": []}
    assert format_step(step) == "[router]          route=sql   理由: x"


def test_format_step_multiline_and_notes():
    step = {"name": "t1/compile", "kind": "code", "summary": "SELECT 1\nparams = []",
            "detail": None, "notes": ["主模型第 1 次调用失败"]}
    assert format_step(step) == (
        "[t1/compile]      SELECT 1\n"
        + " " * 18 + "params = []\n"
        + " " * 18 + "↻ 主模型第 1 次调用失败"
    )


def test_format_step_colors():
    step = {"name": "router", "kind": "model", "summary": "route=sql", "detail": None, "notes": []}
    assert format_step(step, color=True).startswith("\033[35m[router]\033[0m")
    step = {**step, "notes": ["主模型第 1 次调用失败"]}
    assert format_step(step, color=True).startswith("\033[33m[router]\033[0m")
    step = {**step, "kind": "code", "notes": []}
    assert format_step(step, color=True).startswith("\033[90m[router]\033[0m")


def test_format_preview():
    preview = {"action_label": "取消订单", "order_id": "ORD-2025-0031", "order_date": "2025-10-17",
               "status": "Processing",
               "items": [{"name": "Dell XPS 15", "quantity": 1, "price_per_unit": 1399.0}],
               "total_amount": 1399.0, "effect": "订单状态将变为已取消；此操作不可撤销"}
    lines = format_preview(preview).split("\n")
    assert [line.lstrip(" ") for line in lines] == [
        "操作: 取消订单   订单: ORD-2025-0031   下单: 2025-10-17   状态: Processing",
        "明细: Dell XPS 15 × 1   $1399.00",
        "合计: $1399.00   执行后: 订单状态将变为已取消；此操作不可撤销",
    ]


# ---------------------------------------------------------------- 命令行的打印行为

def test_cli_prints_reply_for_handoff(monkeypatch, capsys):
    fake = FakeModels(router=[ROUTE_HANDOFF], answer=["用户要求人工。"])
    fake.install(monkeypatch)
    cli.run_and_print(new_graph(), context_of(), "t-cli-handoff", "帮我转人工")
    lines = capsys.readouterr().out.splitlines()
    reply = [i for i, line in enumerate(lines) if line.startswith("客服> 已为您转接人工客服，工单号 TKT-")]
    stats = [i for i, line in enumerate(lines) if line.startswith("—— 有模型参与的步骤")]
    assert len(reply) == 1, lines
    assert stats and reply[0] < stats[0], lines


def test_cli_does_not_repeat_answer(monkeypatch, capsys):
    fake = FakeModels(router=[ROUTE_DIRECT], answer=["您好！"])
    fake.install(monkeypatch)
    cli.run_and_print(new_graph(), context_of(), "t-cli-direct", "你好")
    out = capsys.readouterr().out
    assert "客服>" not in out
    assert "[answer]".ljust(18) + "您好！" in out


def test_cli_interactive_rebuilds_context_each_turn(monkeypatch):
    fake = FakeModels(router=[ROUTE_DIRECT, ROUTE_DIRECT], answer=["你好！", "不客气。"])
    fake.install(monkeypatch)
    calls = {"count": 0}

    def fake_make_context(customer_id, today=None, fault="off", session_id="", index_version=None):
        calls["count"] += 1
        return AppContext(customer_id="CUST-002", index_version="v1", session_id="s-graph")

    inputs = iter(["你好", "", "谢谢", "exit"])
    monkeypatch.setattr(cli, "make_context", fake_make_context)
    monkeypatch.setattr(builtins, "input", lambda *args, **kwargs: next(inputs))
    assert cli.main([]) == 0
    assert calls["count"] == 3


def test_make_context_uses_given_index_version(monkeypatch):
    """给了 index_version 就不读指针，没给才读。"""
    calls = {"count": 0}

    class FakePointerStore:
        def read_pointer(self):
            calls["count"] += 1
            return {"current": "v7", "previous": None}

    monkeypatch.setattr("app.runner.IndexStore", FakePointerStore)
    assert make_context("CUST-001").index_version == "v7"
    assert calls["count"] == 1
    assert make_context("CUST-001", index_version="v9").index_version == "v9"
    assert calls["count"] == 1


def test_cli_passes_index_version(monkeypatch):
    """命令行的 --index-version 原样传到 make_context。"""
    fake = FakeModels(router=[ROUTE_DIRECT], answer=["你好！"])
    fake.install(monkeypatch)
    seen: dict = {}

    def fake_make_context(*args, **kwargs):
        seen.update(kwargs)
        return AppContext(customer_id="CUST-002", index_version="v9", session_id="s-graph")

    monkeypatch.setattr(cli, "make_context", fake_make_context)
    assert cli.main(["--index-version", "v9", "你好"]) == 0
    assert seen["index_version"] == "v9"
