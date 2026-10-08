"""Router、Supervisor 三个节点、回答节点的单元测试：模型全部是假的。"""
import types

from langchain_core.messages import AIMessage, HumanMessage

import app.answer as answer_mod
import app.router as router_mod
import app.supervisor.nodes as nodes_mod
from app.answer import ANSWER_REMINDER, ANSWER_SYSTEM, DIRECT_SYSTEM, LOGIN_REPLY, answer_node, collect_sources
from app.context import AppContext
from app.models import ModelError, ModelResult
from app.router import ROUTER_SYSTEM, RouteDecision, router_node
from app.supervisor.nodes import PLANNER_SYSTEM, planner_node, scheduler_node, validate_node


def make_runtime(fault: str = "off"):
    """假的 Runtime：只用到 context 字段。"""
    return types.SimpleNamespace(context=AppContext(customer_id="CUST-001", fault=fault))


def state_of(messages=None, **extra):
    state = {"messages": list(messages or [HumanMessage("我最近一个订单到哪了？")])}
    state.update(extra)
    return state


class _StubModel:
    """替换 get_model 后返回的对象：记录 invoke 收到的消息，返回预置结果或抛出预置异常。"""

    def __init__(self, factory, role, schema, fault):
        self.factory, self.role, self.schema, self.fault = factory, role, schema, fault

    def invoke(self, messages):
        f = self.factory
        f.calls.append({"role": self.role, "schema": self.schema, "fault": self.fault})
        f.sent.append(list(messages))
        if f.error is not None:
            raise f.error
        return ModelResult(value=f.value, notes=list(f.notes))


class FakeFactory:
    """记录每个模块对 get_model 的调用；value 是返回值，error 非空则抛异常。"""

    def __init__(self, value=None, error=None, notes=None):
        self.value = value
        self.error = error
        self.notes = notes or []
        self.calls: list[dict] = []     # 每次 get_model 的参数
        self.sent: list[list] = []      # 每次 invoke 收到的 messages

    @property
    def invoked(self) -> bool:
        return len(self.sent) > 0

    def install(self, monkeypatch, module):
        monkeypatch.setattr(module, "get_model",
                            lambda role, schema=None, fault="off": _StubModel(self, role, schema, fault))


def user_message(factory, index=0) -> str:
    """第 index 次 invoke 收到的 user 消息内容。"""
    for role, text in factory.sent[index]:
        if role == "user":
            return text
    raise AssertionError("没有 user 消息")


def system_message(factory, index=0) -> str:
    for role, text in factory.sent[index]:
        if role == "system":
            return text
    raise AssertionError("没有 system 消息")


def step_of(update, name) -> dict:
    for step in update["steps"]:
        if step["name"] == name:
            return step
    raise AssertionError(f"没有名为 {name} 的步骤")


def task_result(task_id, agent, status, data=None, error_kind=None, error=None):
    return {"task_id": task_id, "agent": agent, "goal": f"目标 {task_id}", "status": status,
            "error_kind": error_kind, "error": error, "data": data}


def plan_dict(*tasks):
    return {"reasoning": "", "tasks": list(tasks)}


def task(id, agent="sql", deps=()):
    return {"id": id, "agent": agent, "goal": f"目标 {id}", "depends_on": list(deps)}


# ---------- 1 数据结构 ----------

def test_route_decision_field_order():
    assert list(RouteDecision.model_fields) == ["reason", "route"]


# ---------- 2-4 router ----------

def test_router_success(monkeypatch):
    fake = FakeFactory(value=RouteDecision(reason="查订单", route="sql"))
    fake.install(monkeypatch, router_mod)
    update = router_node(state_of(), make_runtime())
    assert update["route"] == "sql" and update["route_reason"] == "查订单"
    step = step_of(update, "router")
    assert step["kind"] == "model"
    assert step["summary"] == "route=sql   理由: 查订单"
    assert fake.calls == [{"role": "router", "schema": RouteDecision, "fault": "off"}]


def test_router_uses_history_without_customer_id(monkeypatch):
    fake = FakeFactory(value=RouteDecision(reason="承接上文", route="sql"))
    fake.install(monkeypatch, router_mod)
    messages = [HumanMessage("我的订单到哪了"), AIMessage("已发货"), HumanMessage("那上一个呢")]
    router_node(state_of(messages), make_runtime())
    user = user_message(fake)
    assert "最近的对话：" in user
    assert "用户: 我的订单到哪了" in user and "客服: 已发货" in user
    assert "用户的问题：那上一个呢" in user
    assert "CUST-001" not in user


def test_router_model_unavailable(monkeypatch):
    notes = ["主模型超时，重试 1 次", "备用模型也失败"]
    fake = FakeFactory(error=ModelError("模型不可用", notes))
    fake.install(monkeypatch, router_mod)
    update = router_node(state_of(), make_runtime())
    assert update["route"] == "handoff"
    assert update["handoff"] == {"reason": "system_failure", "detail": "路由模型不可用"}
    assert step_of(update, "router")["notes"] == notes


# ---------- 5-7 planner ----------

def _plan_value():
    from app.supervisor.plan import ExecutionPlan
    return ExecutionPlan(reasoning="先查订单再查政策", tasks=[
        {"id": "t1", "agent": "sql", "goal": "查最近的订单里的商品", "depends_on": []},
        {"id": "t2", "agent": "rag", "goal": "查这个商品的保修政策", "depends_on": ["t1"]},
    ])


def test_planner_success(monkeypatch):
    fake = FakeFactory(value=_plan_value())
    fake.install(monkeypatch, nodes_mod)
    update = planner_node(state_of(), make_runtime())
    assert update["plan"] == _plan_value().model_dump()
    assert update["plan_attempts"] == 1
    lines = step_of(update, "planner")["summary"].splitlines()
    assert lines[0] == "执行计划（第 1 次生成，1 次模型调用）"
    assert any("t2  rag" in line and "依赖: t1" in line for line in lines)
    assert system_message(fake) == PLANNER_SYSTEM


def test_planner_regenerates_with_errors(monkeypatch):
    fake = FakeFactory(value=_plan_value())
    fake.install(monkeypatch, nodes_mod)
    state = state_of(plan=plan_dict(task("t1", deps=["t2"]), task("t2", "rag", ["t1"])),
                     plan_errors=["存在循环依赖: t1 -> t2 -> t1"], plan_attempts=1)
    update = planner_node(state, make_runtime())
    user = user_message(fake)
    assert "上一次的执行计划没有通过校验：" in user
    assert "- 存在循环依赖: t1 -> t2 -> t1" in user
    assert update["plan_attempts"] == 2


def test_planner_model_unavailable(monkeypatch):
    fake = FakeFactory(error=ModelError("模型不可用", ["降级后仍失败"]))
    fake.install(monkeypatch, nodes_mod)
    update = planner_node(state_of(), make_runtime())
    assert update["plan"] is None
    assert update["handoff"]["reason"] == "system_failure"


# ---------- 8-10 validate ----------

def test_validate_pass_design_example():
    state = state_of(plan=plan_dict(task("t1", "sql"), task("t2", "rag", ["t1"]), task("t3", "rag")),
                   plan_attempts=1)
    update = validate_node(state, make_runtime())
    assert update["batches"] == [["t1", "t3"], ["t2"]]
    assert update["batch_index"] == 0 and update["plan_errors"] == []
    assert "handoff" not in update


def test_validate_first_failure():
    state = state_of(plan=plan_dict(task("t1", "sql", ["t2"]), task("t2", "rag", ["t1"])),
                   plan_attempts=1)
    update = validate_node(state, make_runtime())
    assert update["batches"] is None
    assert update["plan_errors"]
    assert "handoff" not in update


def test_validate_second_failure_goes_to_handoff():
    state = state_of(plan=plan_dict(task("t1", "sql", ["t2"]), task("t2", "rag", ["t1"])),
                   plan_attempts=2)
    update = validate_node(state, make_runtime())
    assert update["handoff"]["reason"] == "system_failure"


# ---------- 11-12 scheduler ----------

_SCHED_PLAN = plan_dict(task("t1", "sql"), task("t2", "rag", ["t1"]), task("t3", "rag"))


def test_scheduler_first_batch():
    state = state_of(plan=_SCHED_PLAN, batches=[["t1", "t3"], ["t2"]], batch_index=0,
                   task_results={})
    update = scheduler_node(state, make_runtime())
    assert update["current_batch"] == ["t1", "t3"]
    assert update["batch_index"] == 1
    assert step_of(update, "scheduler")["summary"] == "第 1 批并行: t1, t3"


def test_scheduler_skips_dependents_of_failed_task():
    state = state_of(plan=_SCHED_PLAN, batches=[["t1", "t3"], ["t2"]], batch_index=1,
                   task_results={"t1": task_result("t1", "sql", "failed", error="查询超时"),
                                 "t3": task_result("t3", "rag", "ok", data={"chunks": []})})
    update = scheduler_node(state, make_runtime())
    assert update["current_batch"] == []
    assert update["batch_index"] == 2
    assert update["task_results"]["t2"]["status"] == "skipped"
    assert step_of(update, "scheduler")["summary"] == "全部任务已结束   跳过: t2"


# ---------- 13-17 answer ----------

_SQL_OK = task_result("t1", "sql", "ok", data={
    "columns": ["order_id", "status"],
    "rows": [{"order_id": "ORD-2025-0031", "status": "shipped"}], "row_count": 1})

_RAG_OK = task_result("t2", "rag", "ok", data={"index_version": "v1", "chunks": [
    {"doc": "policies/return_policy.md", "section": "Return Windows", "text": "30 天内可退。"},
    {"doc": "policies/return_policy.md", "section": "Exceptions", "text": "定制品除外。"},
]})


def test_answer_direct(monkeypatch):
    fake = FakeFactory(value="您好，很高兴为您服务。")
    fake.install(monkeypatch, answer_mod)
    update = answer_node(state_of(route="direct"), make_runtime())
    assert system_message(fake) == DIRECT_SYSTEM
    assert update["sources"] == []
    assert update["messages"] == [AIMessage(content="您好，很高兴为您服务。")]
    assert fake.calls[0]["schema"] is None
    assert fake.calls[0]["role"] == "answer"


def test_answer_login_template(monkeypatch):
    fake = FakeFactory(value="不该被调用")
    fake.install(monkeypatch, answer_mod)
    results = {"t1": task_result("t1", "sql", "failed", error_kind="login_required",
                                 error="需要先登录")}
    update = answer_node(state_of(route="sql", task_results=results), make_runtime())
    assert update["answer"] == LOGIN_REPLY
    assert step_of(update, "answer")["kind"] == "code"
    assert not fake.invoked


def test_answer_normal(monkeypatch):
    fake = FakeFactory(value="您的订单已发货。")
    fake.install(monkeypatch, answer_mod)
    results = {"t2": _RAG_OK, "t1": _SQL_OK}
    update = answer_node(state_of(route="supervisor", task_results=results), make_runtime())
    user = user_message(fake)
    assert "[任务 t1 · 数据库查询 · 成功]" in user
    assert "查询结果（1 行）:" in user
    assert '<参考资料 来源="policies/return_policy.md" 小节="Return Windows">' in user
    assert "</参考资料>" in user
    assert system_message(fake) == ANSWER_SYSTEM.replace("{today}",
                                                         AppContext(customer_id="CUST-001").today)
    assert update["sources"] == ["policies/return_policy.md"]


def test_answer_partial_failure_still_calls_model(monkeypatch):
    fake = FakeFactory(value="查到一部分。")
    fake.install(monkeypatch, answer_mod)
    results = {"t1": _SQL_OK,
               "t2": task_result("t2", "rag", "skipped", error_kind="dependency_failed",
                                 error="依赖的任务 t1 没有成功，已跳过")}
    update = answer_node(state_of(route="supervisor", task_results=results), make_runtime())
    assert fake.invoked
    user = user_message(fake)
    assert "[任务 t2 · 知识库检索 · 跳过]" in user
    assert "原因: " in user


def test_answer_model_unavailable(monkeypatch):
    fake = FakeFactory(error=ModelError("模型不可用", ["降级后仍失败"]))
    fake.install(monkeypatch, answer_mod)
    update = answer_node(state_of(route="sql", task_results={"t1": _SQL_OK}), make_runtime())
    assert update["handoff"]["reason"] == "system_failure"
    assert "answer" not in update and "messages" not in update


# ---------- 18 fault 透传 ----------

def test_fault_is_passed_through(monkeypatch):
    f_router = FakeFactory(value=RouteDecision(reason="查订单", route="sql"))
    f_router.install(monkeypatch, router_mod)
    f_planner = FakeFactory(value=_plan_value())
    f_planner.install(monkeypatch, nodes_mod)
    f_answer = FakeFactory(value="好的。")
    f_answer.install(monkeypatch, answer_mod)
    rt = make_runtime(fault="primary_down")
    router_node(state_of(), rt)
    planner_node(state_of(), rt)
    answer_node(state_of(route="direct"), rt)
    assert f_router.calls[0]["fault"] == "primary_down"
    assert f_planner.calls[0]["fault"] == "primary_down"
    assert f_answer.calls[0]["fault"] == "primary_down"


# ---------- 19 不绑定工具 ----------

def test_no_bind_tools_anywhere():
    for module in (router_mod, nodes_mod, answer_mod):
        with open(module.__file__, encoding="utf-8") as fh:
            assert "bind_tools" not in fh.read()


# ---------- 20 collect_sources ----------

def test_collect_sources_dedups_in_order():
    results = {
        "t1": task_result("t1", "sql", "ok", data={"rows": [], "row_count": 0}),
        "t2": task_result("t2", "rag", "ok", data={"chunks": [
            {"doc": "a.md", "section": "A", "text": "a"},
            {"doc": "b.md", "section": "B", "text": "b"}]}),
        "t3": task_result("t3", "rag", "failed", error_kind="index_error", error="索引坏了"),
        "t4": task_result("t4", "rag", "ok", data={"chunks": [
            {"doc": "b.md", "section": "B", "text": "b"},
            {"doc": "c.md", "section": "C", "text": "c"}]}),
    }
    assert collect_sources(results) == ["a.md", "b.md", "c.md"]


# ---------- 21 回答提示词的今天日期与提醒 ----------

def _runtime_with_today(today: str = "2025-10-20"):
    """Runtime 的 Context 里指定今天的日期。"""
    return types.SimpleNamespace(context=AppContext(customer_id="CUST-001", today=today))


def test_answer_system_has_today(monkeypatch):
    fake = FakeFactory(value="已过保。")
    fake.install(monkeypatch, answer_mod)
    answer_node(state_of(route="sql", task_results={"t1": _SQL_OK}), _runtime_with_today())
    system = system_message(fake)
    assert "今天是 2025-10-20。" in system
    assert "{today}" not in system


def test_answer_user_message_ends_with_reminder(monkeypatch):
    fake = FakeFactory(value="已过保。")
    fake.install(monkeypatch, answer_mod)
    answer_node(state_of(route="sql", task_results={"t1": _SQL_OK}), _runtime_with_today())
    user = user_message(fake)
    assert user.endswith(ANSWER_REMINDER)
    assert "查询结果（" in user


def test_direct_answer_has_no_reminder(monkeypatch):
    fake = FakeFactory(value="您好。")
    fake.install(monkeypatch, answer_mod)
    answer_node(state_of(route="direct"), _runtime_with_today())
    assert "提醒：" not in user_message(fake)
    assert system_message(fake) == DIRECT_SYSTEM
