"""app/db.py 的测试：连真实数据库，覆盖连接、只读/写账号权限和值转换。"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pymysql
import pytest

from app.db import connect, execute_sql, to_jsonable


def test_execute_sql_count() -> None:
    assert execute_sql("SELECT COUNT(*) AS n FROM orders", []) == {
        "columns": ["n"],
        "rows": [{"n": 250}],
        "row_count": 1,
    }


def test_execute_sql_types_json_serializable() -> None:
    result = execute_sql(
        "SELECT order_id, order_date, total_amount FROM orders WHERE order_id = %s",
        ["ORD-2023-0001"],
    )
    assert result["row_count"] == 1
    row = result["rows"][0]
    assert row["order_id"] == "ORD-2023-0001"
    assert row["order_date"] == "2023-10-25"
    assert isinstance(row["total_amount"], float)
    assert row["total_amount"] == 199.0
    assert json.dumps(result)


def test_to_jsonable_pure() -> None:
    assert to_jsonable(Decimal("1.50")) == 1.5
    assert to_jsonable(date(2025, 1, 2)) == "2025-01-02"
    assert to_jsonable({"a": [Decimal("2")]}) == {"a": [2.0]}


def test_ro_cannot_update() -> None:
    conn = connect("ro")
    try:
        with pytest.raises(pymysql.err.OperationalError):
            with conn.cursor() as cur:
                cur.execute("UPDATE orders SET status = 'Cancelled' WHERE order_id = 'ORD-2023-0001'")
    finally:
        conn.close()


def test_ro_cannot_see_handoff_tickets() -> None:
    conn = connect("ro")
    try:
        with pytest.raises(pymysql.err.OperationalError):
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM handoff_tickets")
    finally:
        conn.close()


def test_rw_cannot_change_amount() -> None:
    conn = connect("rw")
    try:
        with pytest.raises(pymysql.err.OperationalError):
            with conn.cursor() as cur:
                cur.execute("UPDATE orders SET total_amount = 0 WHERE order_id = 'ORD-2023-0001'")
    finally:
        conn.close()


def test_rw_cannot_delete() -> None:
    conn = connect("rw")
    try:
        with pytest.raises(pymysql.err.OperationalError):
            with conn.cursor() as cur:
                cur.execute("DELETE FROM orders WHERE order_id = 'ORD-2023-0001'")
    finally:
        conn.close()


def test_rw_can_update_status_column() -> None:
    conn = connect("rw")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE orders SET status = 'Delivered' "
                "WHERE order_id = 'ORD-2023-0001' AND status = 'Delivered'"
            )
        conn.rollback()
    finally:
        conn.close()


def test_connect_unknown_account() -> None:
    with pytest.raises(ValueError):
        connect("nobody")
