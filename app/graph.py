"""主图：把路由器、Supervisor、两个 Agent、回答、取消订单和转人工的节点连成一张图。

路由决定这一轮走哪条路；supervisor 路径由 planner → validate → scheduler 反复调度，
每个批次把任务用 Send 并行分发给 sql_agent / rag_agent。
"""
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app.answer import answer_node
from app.cancel import (cancel_check_node, cancel_confirm_node, cancel_execute_node,
                        cancel_extract_node, cancel_reply_node)
from app.context import AppContext
from app.handoff import handoff_node
from app.rag.retrieval import rag_agent_node
from app.router import router_node
from app.sql_agent.graph import sql_agent_node
from app.state import State, Task, TaskPayload, history_text, last_user_text
from app.supervisor.nodes import planner_node, scheduler_node, validate_node


def build_payload(state: State, task: Task) -> TaskPayload:
    """拼 Agent 节点的输入：任务本身、本轮问题、历史对话、它依赖的任务结果。"""
    results = state.get("task_results") or {}
    return {
        "task": task,
        "question": last_user_text(state["messages"]),
        "history": history_text(state["messages"]),
        "upstream": {dep: results[dep] for dep in task["depends_on"] if dep in results},
    }


def finish_route(state: State) -> str:
    """所有任务都结束之后去哪：有成功的结果就写回答；只是需要登录也交给回答节点；否则转人工。"""
    results = list((state.get("task_results") or {}).values())
    if any(r["status"] == "ok" for r in results):
        return "answer"
    if any(r["error_kind"] == "login_required" for r in results):
        return "answer"
    return "handoff"


def after_router(state: State):
    """路由之后：单领域直接把一个任务发给对应 Agent，其余走各自的分支。"""
    route = state["route"]
    if route in ("sql", "rag"):
        task: Task = {"id": "t1", "agent": route, "goal": last_user_text(state["messages"]), "depends_on": []}
        return [Send(f"{route}_agent", build_payload(state, task))]
    return {"supervisor": "planner", "cancel": "cancel_extract", "handoff": "handoff", "direct": "answer"}[route]


def after_planner(state: State) -> str:
    return "handoff" if state.get("handoff") else "validate"


def after_validate(state: State) -> str:
    if state.get("batches") is not None:
        return "scheduler"
    return "handoff" if state.get("handoff") else "planner"


def after_scheduler(state: State):
    """有要跑的任务就并行分发；没有了就决定去写回答还是转人工。"""
    if state["current_batch"]:
        tasks = {t["id"]: t for t in state["plan"]["tasks"]}
        return [Send(f"{tasks[tid]['agent']}_agent", build_payload(state, tasks[tid])) for tid in state["current_batch"]]
    return finish_route(state)


def after_task(state: State) -> str:
    """Agent 跑完：在 supervisor 路径里回到调度台，单领域直接收尾。"""
    return "scheduler" if state.get("plan") else finish_route(state)


def after_answer(state: State) -> str:
    return "handoff" if state.get("handoff") else END


def after_cancel_extract(state: State) -> str:
    return "cancel_check" if state["cancel"].get("order_id") else "cancel_reply"


def after_cancel_check(state: State) -> str:
    return "cancel_confirm" if state["cancel"].get("preview") else "cancel_reply"


def after_cancel_confirm(state: State) -> str:
    return "cancel_execute" if state["cancel"]["decision"]["confirm"] else "cancel_reply"


def build_graph(checkpointer=None):
    """编译主图。checkpointer 传入时按 thread_id 记住会话，支持多轮和中断恢复。"""
    g = StateGraph(State, context_schema=AppContext)
    for name, node in [
        ("router", router_node), ("planner", planner_node), ("validate", validate_node),
        ("scheduler", scheduler_node), ("sql_agent", sql_agent_node), ("rag_agent", rag_agent_node),
        ("answer", answer_node), ("handoff", handoff_node),
        ("cancel_extract", cancel_extract_node), ("cancel_check", cancel_check_node),
        ("cancel_confirm", cancel_confirm_node), ("cancel_execute", cancel_execute_node),
        ("cancel_reply", cancel_reply_node),
    ]:
        g.add_node(name, node)

    g.add_edge(START, "router")
    g.add_conditional_edges("router", after_router,
                            ["sql_agent", "rag_agent", "planner", "cancel_extract", "handoff", "answer"])
    g.add_conditional_edges("planner", after_planner, ["validate", "handoff"])
    g.add_conditional_edges("validate", after_validate, ["scheduler", "planner", "handoff"])
    g.add_conditional_edges("scheduler", after_scheduler, ["sql_agent", "rag_agent", "answer", "handoff"])
    g.add_conditional_edges("sql_agent", after_task, ["scheduler", "answer", "handoff"])
    g.add_conditional_edges("rag_agent", after_task, ["scheduler", "answer", "handoff"])
    g.add_conditional_edges("answer", after_answer, ["handoff", END])
    g.add_edge("handoff", END)

    g.add_conditional_edges("cancel_extract", after_cancel_extract, ["cancel_check", "cancel_reply"])
    g.add_conditional_edges("cancel_check", after_cancel_check, ["cancel_confirm", "cancel_reply"])
    g.add_conditional_edges("cancel_confirm", after_cancel_confirm, ["cancel_execute", "cancel_reply"])
    g.add_edge("cancel_execute", "cancel_reply")
    g.add_edge("cancel_reply", END)
    return g.compile(checkpointer=checkpointer)
