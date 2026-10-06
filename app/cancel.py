"""取消订单的五个节点：提取订单号 → 校验 → 确认 → 执行 → 回复。全程没有模型。"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

import pymysql
from langchain_core.messages import AIMessage
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from langsmith.run_helpers import get_current_run_tree

from app.context import AppContext
from app.db import connect, to_jsonable
from app.state import State, last_user_text, make_step

# 订单号格式固定：ORD-年份-四位数字
ORDER_ID_RE = re.compile(r"ORD-\d{4}-\d{4}")

# 真正防止误取消的保证在这条 UPDATE 的 WHERE 条件里
UPDATE_CANCEL_SQL = ("UPDATE orders SET status = 'Cancelled', cancelled_at = %s "
                     "WHERE order_id = %s AND customer_id = %s AND status = 'Processing'")

DB_ERROR_MESSAGE = "系统暂时无法处理取消，请稍后再试。"


def current_trace_id() -> str:
    """当前 LangSmith run 的 trace 编号，不在 trace 上下文里时是空串。"""
    run = get_current_run_tree()
    return str(run.trace_id) if run is not None else ""


def _fetch_one(conn, sql: str, params: list) -> dict | None:
    """执行一条查询，返回第一行（经过 to_jsonable）或 None。"""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return to_jsonable(row) if row is not None else None


def _error_update(cancel: dict, step_name: str, exc: Exception) -> dict:
    """数据库出错时统一的失败结果。"""
    cancel = {**cancel, "outcome": "error", "message": DB_ERROR_MESSAGE}
    return {"cancel": cancel,
            "steps": [make_step(step_name, "code", f"数据库出错: {exc}", {"outcome": "error"})]}


def cancel_extract_node(state: State, runtime: Runtime[AppContext]) -> dict:
    """从用户这句话里提取订单号。提取不到就不猜，列出可取消的订单让用户指定。"""
    cancel = dict(state.get("cancel") or {})
    customer_id = runtime.context.customer_id
    if customer_id is None:
        cancel.update({"outcome": "login_required", "message": "取消订单需要先登录。"})
        return {"cancel": cancel, "steps": [make_step("cancel/extract", "code", "匿名用户，提示登录", None)]}

    text = last_user_text(state["messages"]).upper()
    found = list(dict.fromkeys(ORDER_ID_RE.findall(text)))
    if len(found) == 1:
        order_id = found[0]
        cancel["order_id"] = order_id
        return {"cancel": cancel,
                "steps": [make_step("cancel/extract", "code", f"订单号 {order_id}（正则提取）", {"order_id": order_id})]}

    try:
        conn = connect("ro")
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT order_id, order_date, total_amount FROM orders "
                    "WHERE customer_id = %s AND status = 'Processing' "
                    "ORDER BY order_date DESC, order_id",
                    [customer_id],
                )
                candidates = [to_jsonable(row) for row in cur.fetchall()]
        finally:
            conn.close()
    except pymysql.MySQLError as e:
        return _error_update(cancel, "cancel/extract", e)

    message = "一次只能取消一个订单。" if len(found) > 1 else ""
    if candidates:
        message += "请告诉我要取消的订单号。您当前可以取消的订单："
        message += "".join(
            f"\n- {row['order_id']}（下单日期 {row['order_date']}，金额 ${float(row['total_amount']):.2f}）"
            for row in candidates
        )
    else:
        message += "您当前没有可以取消的订单（只有处理中的订单可以取消）。"

    cancel.update({"outcome": "need_order_id", "candidates": candidates, "message": message})
    summary = f"提取到 {len(found)} 个订单号，列出 {len(candidates)} 个可取消的订单让用户指定"
    return {"cancel": cancel,
            "steps": [make_step("cancel/extract", "code", summary,
                                {"found": found, "candidates": candidates})]}


def cancel_check_node(state: State, runtime: Runtime[AppContext]) -> dict:
    """查这个订单能不能取消。可以的话生成结构化预览，给用户确认用。"""
    cancel = dict(state.get("cancel") or {})
    order_id = cancel.get("order_id", "")
    try:
        conn = connect("ro")
        try:
            row = _fetch_one(
                conn,
                "SELECT order_id, order_date, status, total_amount FROM orders "
                "WHERE order_id = %s AND customer_id = %s",
                [order_id, runtime.context.customer_id],
            )
        finally:
            conn.close()
    except pymysql.MySQLError as e:
        return _error_update(cancel, "cancel/check", e)

    status = row["status"] if row is not None else None
    if row is None:
        cancel.update({"outcome": "not_found", "message": f"没有找到订单 {order_id}。"})
        summary = "没有找到这个订单（不存在或不属于当前客户）"
    elif status in ("Shipped", "Delivered"):
        done = "发货" if status == "Shipped" else "送达"
        cancel.update({"outcome": "not_cancellable",
                       "message": f"订单 {order_id} 已经{done}，不能取消。如需退货请走退货流程。"})
        summary = f"状态 {status}，不能取消"
    elif status == "Cancelled":
        cancel.update({"outcome": "already_cancelled", "message": f"订单 {order_id} 之前已经取消过了。"})
        summary = "状态 Cancelled，已经取消过"
    else:  # Processing：可以取消
        try:
            conn = connect("ro")
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT p.name, oi.quantity, oi.price_per_unit FROM order_items oi "
                        "JOIN products p ON p.product_id = oi.product_id "
                        "WHERE oi.order_id = %s ORDER BY oi.order_item_id",
                        [order_id],
                    )
                    items = to_jsonable(cur.fetchall())
            finally:
                conn.close()
        except pymysql.MySQLError as e:
            return _error_update(cancel, "cancel/check", e)

        preview = {
            "action": "cancel_order",
            "action_label": "取消订单",
            "order_id": row["order_id"],
            "order_date": row["order_date"],          # ISO 字符串
            "status": "Processing",
            "items": [{"name": it["name"], "quantity": int(it["quantity"]),
                       "price_per_unit": float(it["price_per_unit"])} for it in items],
            "total_amount": float(row["total_amount"]),
            "effect": "订单状态将变为已取消；此操作不可撤销",
        }
        cancel["preview"] = preview
        summary = "属于当前客户 ✓   状态 Processing ✓   可以取消"

    detail = {"order_id": order_id, "status": status, "preview": cancel.get("preview")}
    return {"cancel": cancel, "steps": [make_step("cancel/check", "code", summary, detail)]}


def cancel_confirm_node(state: State, runtime: Runtime[AppContext]) -> dict:
    """中断，把预览交给调用方等用户选择。只认真正表示确认的值。"""
    cancel = state["cancel"]
    answer = interrupt(cancel["preview"])
    confirmed = isinstance(answer, dict) and answer.get("confirm") is True
    decision = {"confirm": confirmed,
                "order_id": answer.get("order_id") if isinstance(answer, dict) else None}
    if confirmed:
        return {"cancel": {**cancel, "decision": decision},
                "steps": [make_step("cancel/confirm", "code", "用户: 确认",
                                    {"decision": decision, "preview": cancel["preview"]})]}

    preview = cancel.get("preview") or {}
    order_id = preview.get("order_id") or cancel.get("order_id", "")
    cancel = {**cancel, "decision": decision, "outcome": "declined",
              "message": f"好的，订单 {order_id} 没有取消。"}
    return {"cancel": cancel, "steps": [make_step("cancel/confirm", "code", "用户: 不取消",
                                                  {"decision": decision, "preview": cancel["preview"]})]}


def cancel_execute_node(state: State, runtime: Runtime[AppContext]) -> dict:
    """带条件的 UPDATE 执行取消。真正的安全保证在 WHERE 条件里。"""
    cancel = {**state["cancel"]}
    decision = cancel["decision"]
    preview = cancel["preview"]
    order_id = preview["order_id"]
    if decision["order_id"] != order_id:
        cancel.update({"outcome": "mismatch", "message": "确认的订单和预览的不一致，没有执行取消。"})
        return {"cancel": cancel,
                "steps": [make_step("cancel/execute", "code", "确认的订单号和预览不一致，拒绝执行",
                                    {"outcome": "mismatch"})]}

    # 取消时间用 Runtime 里的「今天」加上当前时刻，避免跨天的会话拿到过去的日期
    cancelled_at = datetime.combine(date.fromisoformat(runtime.context.today),
                                    datetime.now().time()).replace(microsecond=0)
    customer_id = runtime.context.customer_id
    params = [cancelled_at, order_id, customer_id]
    try:
        conn = connect("rw")
        try:
            with conn.cursor() as cur:
                cur.execute(UPDATE_CANCEL_SQL, params)
                affected = cur.rowcount
            if affected == 1:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO order_cancellations "
                        "(order_id, customer_id, session_id, trace_id, cancelled_at) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        [order_id, customer_id, runtime.context.session_id,
                         current_trace_id(), cancelled_at],
                    )
                conn.commit()
                cancel["outcome"] = "cancelled"
                cancel["message"] = f"订单 {order_id} 已取消。"
                summary = "影响 1 行，已写入取消记录，事务提交"
            else:
                # 状态在等确认的这段时间里变了，回滚后如实说明
                conn.rollback()
                conn.close()  # 先释放写连接，再用只读账号重新查状态
                conn = connect("ro")
                row = _fetch_one(
                    conn,
                    "SELECT order_id, order_date, status, total_amount FROM orders "
                    "WHERE order_id = %s AND customer_id = %s",
                    [order_id, customer_id],
                )
                status = row["status"] if row is not None else None
                if status == "Cancelled":
                    outcome, message = "already_cancelled", f"订单 {order_id} 已经是取消状态，没有重复取消。"
                elif status in ("Shipped", "Delivered"):
                    outcome, message = "not_cancellable", (
                        f"订单 {order_id} 的状态已经变为 {status}，不能取消了。如需退货请走退货流程。")
                else:
                    outcome, message = "not_found", f"没有找到订单 {order_id}。"
                cancel["outcome"] = outcome
                cancel["message"] = message
                summary = f"影响 0 行（当前状态 {status}），没有取消"
        finally:
            conn.close()
    except pymysql.MySQLError as e:
        return _error_update(cancel, "cancel/execute", e)

    detail = {"sql": UPDATE_CANCEL_SQL,
              "params": [str(cancelled_at), order_id, customer_id],
              "affected_rows": affected,
              "outcome": cancel.get("outcome")}
    return {"cancel": cancel, "steps": [make_step("cancel/execute", "code", summary, detail)]}


def cancel_reply_node(state: State, runtime: Runtime[AppContext]) -> dict:
    """用模板拼出回复。这一轮只处理取消，别的问题提醒用户再问一次。"""
    cancel = state.get("cancel") or {}
    message = cancel.get("message", "")
    outcome = cancel.get("outcome")
    order_id = cancel.get("order_id")

    # 用户这句话去掉订单号之后还有问句或「另外/顺便/还有」，说明还问了别的
    rest = ORDER_ID_RE.sub("", last_user_text(state["messages"]))
    answer = message
    if any(token in rest for token in ("？", "?", "另外", "顺便", "还有")):
        answer += "\n这一轮只处理了取消订单，其他问题请再问一次。"

    return {"answer": answer,
            "messages": [AIMessage(content=answer)],
            "card": {"type": "cancel_result", "outcome": outcome, "order_id": order_id, "message": message},
            "steps": [make_step("cancel/reply", "code", answer, {"outcome": outcome})]}
