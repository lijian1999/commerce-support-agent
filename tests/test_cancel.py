"""取消订单五个节点的测试：连真实数据库，覆盖提取、校验、确认、执行和回复。"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app import cancel as cancel_module
from app.cancel import (cancel_check_node, cancel_confirm_node, cancel_execute_node,
                        cancel_extract_node, cancel_reply_node)
from app.context import AppContext
from app.db import connect

# 复原这个测试用到的两张单，保证每个测试跑完数据库回到初始状态
RESTORE_ORDERS = ("ORD-2025-0031", "ORD-2025-0038")


@pytest.fixture(autouse=True)
def restore_orders() -> None:
    yield
    conn = connect("root")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE orders SET status = 'Processing', cancelled_at = NULL "
                "WHERE order_id IN ('ORD-2025-0031', 'ORD-2025-0038')"
            )
            cur.execute("DELETE FROM order_cancellations WHERE order_id IN ('ORD-2025-0031', 'ORD-2025-0038')")
        conn.commit()
    finally:
        conn.close()


def make_runtime(customer_id: str | None = "CUST-002"):
    return types.SimpleNamespace(context=AppContext(customer_id=customer_id, session_id="s-test"))


def make_state(text: str, cancel: dict | None = None) -> dict:
    return {"messages": [HumanMessage(content=text)], "cancel": cancel}


def set_status(order_id: str, status: str) -> None:
    """用 root 账号直接改状态，模拟用户在确认期间订单状态变了。"""
    conn = connect("root")
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE orders SET status = %s WHERE order_id = %s", [status, order_id])
        conn.commit()
    finally:
        conn.close()


def fetch_order(order_id: str) -> dict:
    conn = connect("root")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status, cancelled_at, total_amount FROM orders WHERE order_id = %s", [order_id])
            return cur.fetchone()
    finally:
        conn.close()


def fetch_order_items(order_id: str) -> list[dict]:
    conn = connect("root")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT quantity FROM order_items WHERE order_id = %s", [order_id])
            return list(cur.fetchall())
    finally:
        conn.close()


def fetch_cancellations(order_id: str) -> list[dict]:
    conn = connect("root")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT order_id, customer_id, session_id FROM order_cancellations WHERE order_id = %s",
                [order_id],
            )
            return list(cur.fetchall())
    finally:
        conn.close()


def checked_cancel(text: str = "取消 ORD-2025-0031", order_id: str = "ORD-2025-0031",
                  customer_id: str = "CUST-002") -> dict:
    """走一遍校验，拿到可以取消的 cancel 字典。"""
    state = make_state(text, {"order_id": order_id})
    result = cancel_check_node(state, make_runtime(customer_id))
    return result["cancel"]


def test_extract_single_order_id() -> None:
    result = cancel_extract_node(make_state("帮我取消订单 ord-2025-0031"), make_runtime())
    assert result["cancel"]["order_id"] == "ORD-2025-0031"
    assert result["steps"][0]["name"] == "cancel/extract"


def test_extract_without_order_id_lists_candidates() -> None:
    result = cancel_extract_node(make_state("我想取消订单"), make_runtime())
    cancel = result["cancel"]
    assert cancel["outcome"] == "need_order_id"
    assert [row["order_id"] for row in cancel["candidates"]] == ["ORD-2025-0031", "ORD-2025-0038"]
    assert "- ORD-2025-0031（下单日期 2025-10-17，金额 $1399.00）" in cancel["message"]
    assert "order_id" not in cancel


def test_extract_multiple_order_ids() -> None:
    result = cancel_extract_node(make_state("取消 ORD-2025-0031 和 ORD-2025-0038"), make_runtime())
    cancel = result["cancel"]
    assert cancel["outcome"] == "need_order_id"
    assert cancel["message"].startswith("一次只能取消一个订单。")


def test_extract_anonymous_requires_login() -> None:
    result = cancel_extract_node(make_state("我想取消订单"), make_runtime(None))
    assert result["cancel"]["outcome"] == "login_required"
    assert result["cancel"]["message"] == "取消订单需要先登录。"


def test_extract_no_cancellable_order() -> None:
    result = cancel_extract_node(make_state("我想取消订单"), make_runtime("CUST-001"))
    cancel = result["cancel"]
    assert cancel["candidates"] == []
    assert "您当前没有可以取消的订单" in cancel["message"]


def test_check_other_customers_order_is_not_found() -> None:
    state = make_state("取消 ORD-2025-0031", {"order_id": "ORD-2025-0031"})
    result = cancel_check_node(state, make_runtime("CUST-001"))
    assert result["cancel"]["outcome"] == "not_found"
    # 别人的订单和不存在的订单，除了订单号以外说明文字完全相同
    other = cancel_check_node(make_state("取消 ORD-2099-9999", {"order_id": "ORD-2099-9999"}),
                              make_runtime())
    mine = result["cancel"]["message"].replace("ORD-2025-0031", "X")
    theirs = other["cancel"]["message"].replace("ORD-2099-9999", "X")
    assert mine == theirs


def test_check_delivered_order_not_cancellable() -> None:
    result = cancel_check_node(make_state("取消 ORD-2025-0009", {"order_id": "ORD-2025-0009"}),
                               make_runtime())
    cancel = result["cancel"]
    assert cancel["outcome"] == "not_cancellable"
    assert "已经送达" in cancel["message"]
    assert "preview" not in cancel


def test_check_already_cancelled() -> None:
    state = make_state("取消 ORD-2025-0028", {"order_id": "ORD-2025-0028"})
    result = cancel_check_node(state, make_runtime("CUST-019"))
    assert result["cancel"]["outcome"] == "already_cancelled"


def test_check_cancellable_generates_preview() -> None:
    result = cancel_check_node(make_state("取消 ORD-2025-0031", {"order_id": "ORD-2025-0031"}),
                               make_runtime())
    cancel = result["cancel"]
    assert "outcome" not in cancel
    preview = cancel["preview"]
    assert preview["order_id"] == "ORD-2025-0031"
    assert preview["status"] == "Processing"
    assert preview["total_amount"] == 1399.0
    assert preview["order_date"] == "2025-10-17"
    assert len(preview["items"]) == 1
    assert preview["items"][0]["quantity"] == 1
    assert preview["effect"] == "订单状态将变为已取消；此操作不可撤销"
    json.dumps(result["steps"][0]["detail"], ensure_ascii=False)


def test_confirm_accepted(monkeypatch) -> None:
    monkeypatch.setattr("app.cancel.interrupt", lambda preview: {"confirm": True, "order_id": "ORD-2025-0031"})
    cancel = checked_cancel()
    result = cancel_confirm_node(make_state("取消 ORD-2025-0031", cancel), make_runtime())
    assert result["cancel"]["decision"] == {"confirm": True, "order_id": "ORD-2025-0031"}
    assert "outcome" not in result["cancel"]
    assert result["steps"][0]["name"] == "cancel/confirm"


def test_confirm_rejects_other_answers(monkeypatch) -> None:
    cancel = checked_cancel()
    for answer in ({"confirm": False, "order_id": "ORD-2025-0031"}, "好吧", {"confirm": "yes"}):
        monkeypatch.setattr("app.cancel.interrupt", lambda preview, a=answer: a)
        result = cancel_confirm_node(make_state("取消 ORD-2025-0031", dict(cancel)), make_runtime())
        assert result["cancel"]["decision"]["confirm"] is False
        assert result["cancel"]["outcome"] == "declined"
        assert "没有取消" in result["cancel"]["message"]


def test_execute_success() -> None:
    cancel = checked_cancel()
    cancel["decision"] = {"confirm": True, "order_id": "ORD-2025-0031"}
    result = cancel_execute_node(make_state("取消 ORD-2025-0031", cancel), make_runtime())
    assert result["cancel"]["outcome"] == "cancelled"
    order = fetch_order("ORD-2025-0031")
    assert order["status"] == "Cancelled"
    assert order["cancelled_at"] is not None
    assert float(order["total_amount"]) == 1399.0
    assert len(fetch_order_items("ORD-2025-0031")) == 1
    rows = fetch_cancellations("ORD-2025-0031")
    assert len(rows) == 1
    assert rows[0]["customer_id"] == "CUST-002"
    assert rows[0]["session_id"] == "s-test"


def test_execute_is_idempotent() -> None:
    cancel = checked_cancel()
    cancel["decision"] = {"confirm": True, "order_id": "ORD-2025-0031"}
    first = cancel_execute_node(make_state("取消 ORD-2025-0031", dict(cancel)), make_runtime())
    assert first["cancel"]["outcome"] == "cancelled"
    second = cancel_execute_node(make_state("取消 ORD-2025-0031", dict(first["cancel"])), make_runtime())
    assert second["cancel"]["outcome"] == "already_cancelled"
    assert len(fetch_cancellations("ORD-2025-0031")) == 1


def test_execute_when_status_changed_during_confirmation() -> None:
    cancel = checked_cancel()
    cancel["decision"] = {"confirm": True, "order_id": "ORD-2025-0031"}
    set_status("ORD-2025-0031", "Shipped")
    result = cancel_execute_node(make_state("取消 ORD-2025-0031", cancel), make_runtime())
    assert result["cancel"]["outcome"] == "not_cancellable"
    assert "Shipped" in result["cancel"]["message"]
    assert fetch_cancellations("ORD-2025-0031") == []
    assert fetch_order("ORD-2025-0031")["status"] == "Shipped"


def test_execute_order_id_mismatch() -> None:
    cancel = checked_cancel()
    cancel["decision"] = {"confirm": True, "order_id": "ORD-2025-0038"}
    result = cancel_execute_node(make_state("取消 ORD-2025-0031", cancel), make_runtime())
    assert result["cancel"]["outcome"] == "mismatch"
    assert fetch_order("ORD-2025-0031")["status"] == "Processing"
    assert fetch_order("ORD-2025-0038")["status"] == "Processing"


def test_execute_other_customer_cannot() -> None:
    cancel = checked_cancel()
    cancel["decision"] = {"confirm": True, "order_id": "ORD-2025-0031"}
    result = cancel_execute_node(make_state("取消 ORD-2025-0031", cancel), make_runtime("CUST-001"))
    assert result["cancel"]["outcome"] == "not_found"
    assert fetch_order("ORD-2025-0031")["status"] == "Processing"


def test_reply_is_template() -> None:
    cancel = {"order_id": "ORD-2025-0031", "outcome": "cancelled", "message": "订单 ORD-2025-0031 已取消。"}
    result = cancel_reply_node(make_state("帮我取消订单 ORD-2025-0031", cancel), make_runtime())
    assert result["answer"] == "订单 ORD-2025-0031 已取消。"
    assert len(result["messages"]) == 1
    assert isinstance(result["messages"][0], AIMessage)
    assert result["messages"][0].content == "订单 ORD-2025-0031 已取消。"
    assert result["card"] == {"type": "cancel_result", "outcome": "cancelled",
                              "order_id": "ORD-2025-0031", "message": "订单 ORD-2025-0031 已取消。"}


def test_reply_mentions_other_questions() -> None:
    cancel = {"order_id": "ORD-2025-0031", "outcome": "cancelled", "message": "订单 ORD-2025-0031 已取消。"}
    result = cancel_reply_node(make_state("帮我取消订单 ORD-2025-0031，另外退货政策是什么？", cancel),
                              make_runtime())
    assert result["answer"].startswith("订单 ORD-2025-0031 已取消。")
    assert result["answer"].endswith("这一轮只处理了取消订单，其他问题请再问一次。")


def test_no_model_in_source() -> None:
    source = Path(cancel_module.__file__).read_text(encoding="utf-8")
    assert "get_model" not in source
    assert "app.models" not in source
    assert "execute_sql" not in source
