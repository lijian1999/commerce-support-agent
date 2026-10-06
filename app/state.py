"""图的状态、步骤记录、任务结果的数据结构。所有节点共用，字段名以这里为准。"""
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.graph.message import add_messages

from app.config import HISTORY_MESSAGES


class Step(TypedDict):
    """一个节点跑完后的可打印记录。终端、界面、Trace 的步骤一一对应。"""
    name: str         # 如 router、t1/sql_plan、cancel/check
    kind: str         # model（有模型参与）或 code（确定性代码）
    summary: str      # 一行摘要
    detail: Any       # 完整输出，必须能被 json.dumps 序列化
    notes: list[str]  # 模型调用的重试、降级记录；没有发生时是空列表


def make_step(name: str, kind: str, summary: str, detail: Any = None, notes: list[str] | None = None) -> Step:
    assert kind in ("model", "code"), kind
    return {"name": name, "kind": kind, "summary": summary, "detail": detail, "notes": list(notes or [])}


class Task(TypedDict):
    id: str                # 如 t1
    agent: str             # sql 或 rag
    goal: str              # 这个任务要拿到什么
    depends_on: list[str]  # 依赖的任务编号


class TaskResult(TypedDict):
    task_id: str
    agent: str
    goal: str
    status: str             # ok / failed / skipped
    error_kind: str | None  # login_required / unanswerable / invalid_plan / model_error / db_error / index_error / dependency_failed
    error: str | None       # 给用户看的失败说明
    data: Any               # sql: {"columns", "rows", "row_count"}；rag: {"index_version", "chunks"}；失败时为 None


def make_task_result(task: Task, status: str, data: Any = None,
                     error_kind: str | None = None, error: str | None = None) -> TaskResult:
    assert status in ("ok", "failed", "skipped"), status
    return {"task_id": task["id"], "agent": task["agent"], "goal": task["goal"],
            "status": status, "error_kind": error_kind, "error": error, "data": data}


class TaskPayload(TypedDict):
    """分发给 sql_agent / rag_agent 节点的输入。"""
    task: Task
    question: str                    # 用户本轮的原始问题
    history: str                     # 最近几轮对话的文本，没有则为空串
    upstream: dict[str, TaskResult]  # 它依赖的任务的结果


def reset_or_add(old: list | None, new: list | None) -> list:
    """列表累加；写入 None 表示清空（每轮开始时用）。"""
    if new is None:
        return []
    return (old or []) + new


def reset_or_merge(old: dict | None, new: dict | None) -> dict:
    """字典合并；写入 None 表示清空（每轮开始时用）。"""
    if new is None:
        return {}
    return {**(old or {}), **new}


class State(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]   # 整个会话的对话记录，跨轮保留

    # ---- 以下字段每轮开始时由 new_turn_input 重置 ----
    route: str | None             # router 的结果：sql / rag / supervisor / cancel / handoff / direct
    route_reason: str | None
    plan: dict | None             # ExecutionPlan.model_dump()，只有 supervisor 路径有
    plan_errors: list[str]        # 最近一次计划校验的错误
    plan_attempts: int            # 已经生成过几次计划
    batches: list[list[str]] | None   # 校验通过后算出的并行批次
    batch_index: int              # 下一个要调度的批次下标
    current_batch: list[str]      # scheduler 本次要分发的任务编号
    task_results: Annotated[dict[str, TaskResult], reset_or_merge]
    cancel: dict | None           # 取消订单流程的工作数据
    handoff: dict | None          # 转人工的触发信息 {"reason", "detail"}；写完工单后是工单内容
    answer: str | None            # 本轮最终回复的文本
    sources: list[str]            # 回答引用的文档来源
    card: dict | None             # 给界面渲染的结构化卡片（工单、取消结果）
    steps: Annotated[list[Step], reset_or_add]   # 主图节点的步骤记录（子图节点的步骤只出现在流里）


def new_turn_input(text: str) -> dict:
    """一轮对话的图输入：追加用户消息，并把所有按轮的字段清空。"""
    return {
        "messages": [HumanMessage(content=text)],
        "route": None, "route_reason": None,
        "plan": None, "plan_errors": [], "plan_attempts": 0,
        "batches": None, "batch_index": 0, "current_batch": [],
        "task_results": None,
        "cancel": None, "handoff": None,
        "answer": None, "sources": [], "card": None,
        "steps": None,
    }


def last_user_text(messages: list[AnyMessage]) -> str:
    """最后一条用户消息的文本。"""
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return str(message.content)
    return ""


def history_text(messages: list[AnyMessage], limit: int = HISTORY_MESSAGES) -> str:
    """当前这条用户消息之前的最近几条对话，格式为「用户: …」「客服: …」，一行一条。没有则返回空串。"""
    earlier = list(messages)
    for i in range(len(earlier) - 1, -1, -1):
        if isinstance(earlier[i], HumanMessage):
            earlier = earlier[:i]
            break
    lines = []
    for message in earlier[-limit:]:
        if isinstance(message, HumanMessage):
            lines.append(f"用户: {message.content}")
        elif isinstance(message, AIMessage):
            lines.append(f"客服: {message.content}")
    return "\n".join(lines)
