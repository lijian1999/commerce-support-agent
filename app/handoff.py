"""转人工节点：判断触发原因，生成对话摘要，往 handoff_tickets 写一条工单。"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from typing import Any

import pymysql
from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime
from langsmith.run_helpers import get_current_run_tree

from app.context import AppContext
from app.db import connect
from app.models import ModelError, get_model
from app.state import State, history_text, last_user_text, make_step

# 转人工原因的中文说明，写进工单的 reason_label，也用于步骤摘要
REASON_LABELS = {
    "user_request": "用户要求人工",
    "out_of_scope": "超出机器人处理范围",
    "system_failure": "系统处理失败",
}

# 用户这句话里出现这些词就认为是「明确要求人工」
_USER_REQUEST_PATTERN = re.compile(r"人工|真人|客服人员|human|agent", re.IGNORECASE)

# 提取订单号
_ORDER_ID_PATTERN = re.compile(r"ORD-\d{4}-\d{4}")

# 写交接摘要的提示词
SUMMARY_SYSTEM = (
    "你在为人工客服写一段交接摘要。根据下面的对话，用两三句简体中文写清楚："
    "用户遇到了什么问题、想要什么、提到了哪些订单或商品。"
    "只写对话里有的内容，不要编造，不要给出处理建议。"
)

_INSERT_SQL = (
    "INSERT INTO handoff_tickets "
    "(ticket_id, customer_id, reason, summary, order_ids, session_id, trace_id, created_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
)


def resolve_trigger(state: State) -> dict:
    """按「已有触发信息 → 路由 → 故障」的顺序判断这次转人工的原因。"""
    existing = state.get("handoff")
    if isinstance(existing, dict) and "reason" in existing:
        return {"reason": existing["reason"], "detail": existing.get("detail", "")}

    if state.get("route") == "handoff":
        text = last_user_text(state["messages"])
        reason = "user_request" if _USER_REQUEST_PATTERN.search(text) else "out_of_scope"
        return {"reason": reason, "detail": state.get("route_reason") or ""}

    detail = "系统无法处理这个请求"
    plan_errors = state.get("plan_errors")
    if plan_errors:
        detail = "执行计划没有通过校验: " + "；".join(plan_errors)
    else:
        bad = [result for result in (state.get("task_results") or {}).values()
               if result.get("status") in ("failed", "skipped")]
        if bad:
            bad.sort(key=lambda item: item["task_id"])
            detail = "任务全部失败: " + "；".join(f"{item['task_id']}（{item['error']}）" for item in bad)
    return {"reason": "system_failure", "detail": detail}


def raw_transcript(state: State) -> str:
    """最近几轮对话的原始文本，加上新这句话。系统故障转人工时直接当摘要用。"""
    history = history_text(state["messages"])
    current = last_user_text(state["messages"])
    if history:
        return history + "\n用户: " + current
    return "用户: " + current


def extract_order_ids(state: State) -> list[str]:
    """对话里提到的所有订单号，去重后按首次出现的顺序。"""
    ids: list[str] = []
    for message in state["messages"]:
        for found in _ORDER_ID_PATTERN.findall(str(message.content).upper()):
            if found not in ids:
                ids.append(found)
    return ids


def new_ticket_id(today: str) -> str:
    """工单号：日期加随机串，例如 TKT-20251020-3FA9C1。"""
    return "TKT-" + today.replace("-", "") + "-" + uuid.uuid4().hex[:6].upper()


def current_trace_id() -> str:
    """当前 Trace 的编号；不在 Trace 里时是空串。"""
    run = get_current_run_tree()
    return str(run.trace_id) if run is not None else ""


def handoff_node(state: State, runtime: Runtime[AppContext]) -> dict:
    """写一张转人工工单，回复工单号。"""
    trigger = resolve_trigger(state)

    notes: list[str] = []
    kind = "code"
    source = "原始对话"
    if trigger["reason"] == "system_failure":
        # 系统故障时不能再依赖模型，直接存原始对话
        summary = raw_transcript(state)
    else:
        kind = "model"
        try:
            result = get_model("answer", fault=runtime.context.fault).invoke(
                [("system", SUMMARY_SYSTEM), ("user", raw_transcript(state))]
            )
        except ModelError as e:
            summary = raw_transcript(state)
            notes = list(e.notes)
        else:
            summary = str(result.value)
            notes = list(result.notes)
            source = "模型"

    created_at = datetime.combine(
        date.fromisoformat(runtime.context.today), datetime.now().time()
    ).replace(microsecond=0)
    ticket = {
        "ticket_id": new_ticket_id(runtime.context.today),
        "customer_id": runtime.context.customer_id,
        "reason": trigger["reason"],
        "reason_label": REASON_LABELS[trigger["reason"]],
        "detail": trigger["detail"],
        "summary": summary,
        "order_ids": extract_order_ids(state),
        "session_id": runtime.context.session_id,
        "trace_id": current_trace_id(),
        "created_at": created_at.isoformat(),
    }

    answer_text = (
        f"已为您转接人工客服，工单号 {ticket['ticket_id']}。"
        "人工客服会根据已有的记录继续处理，您不需要重复说明。"
    )
    try:
        conn = connect("rw")
        try:
            with conn.cursor() as cur:
                cur.execute(_INSERT_SQL, [
                    ticket["ticket_id"], ticket["customer_id"], ticket["reason"],
                    ticket["summary"], ",".join(ticket["order_ids"]),
                    ticket["session_id"], ticket["trace_id"], ticket["created_at"],
                ])
            conn.commit()
        finally:
            conn.close()
    except pymysql.MySQLError as e:
        return {
            "handoff": ticket,
            "card": None,
            "answer": "系统繁忙，暂时无法创建工单，请稍后再试或直接联系人工客服。",
            "messages": [AIMessage(content="系统繁忙，暂时无法创建工单，请稍后再试或直接联系人工客服。")],
            "steps": [make_step("handoff", kind, f"写工单失败: {e}", ticket, notes)],
        }

    return {
        "handoff": ticket,
        "card": {"type": "ticket", **ticket},
        "answer": answer_text,
        "messages": [AIMessage(content=answer_text)],
        "steps": [make_step(
            "handoff", kind,
            f"工单 {ticket['ticket_id']}   原因: {ticket['reason_label']}   摘要来源: {source}",
            ticket, notes,
        )],
    }
