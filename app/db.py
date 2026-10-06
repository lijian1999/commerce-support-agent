"""数据库访问的唯一入口：连接、值转换、执行只读 SQL。"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pymysql
import pymysql.cursors
from langsmith import traceable

from app import config

# account -> (用户名字段, 密码字段)
_ACCOUNTS = {
    "ro": (config.MYSQL_RO_USER, config.MYSQL_RO_PASSWORD),
    "rw": (config.MYSQL_RW_USER, config.MYSQL_RW_PASSWORD),
    "root": ("root", config.MYSQL_ROOT_PASSWORD),
}


def connect(account: str) -> pymysql.connections.Connection:
    """按账号建立连接。account 是 "ro" / "rw" / "root"。"""
    if account not in _ACCOUNTS:
        raise ValueError(f"未知的数据库账号: {account}")
    user, password = _ACCOUNTS[account]
    conn = pymysql.connect(
        host=config.MYSQL_HOST,
        port=config.MYSQL_PORT,
        user=user,
        password=password,
        database=config.MYSQL_DATABASE,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
        connect_timeout=5,
    )
    # 只读账号加语句超时，避免慢查询拖垮整个图
    if account == "ro":
        with conn.cursor() as cur:
            cur.execute("SET SESSION MAX_EXECUTION_TIME = %s", (config.SQL_TIMEOUT_MS,))
    return conn


def to_jsonable(value: Any) -> Any:
    """把数据库返回值转成能 json.dumps 的形式。"""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_jsonable(v) for v in value]
    return value


@traceable(run_type="tool", name="execute_sql")
def execute_sql(sql: str, params: list) -> dict:
    """用只读账号执行一条 SQL，返回列名、行、行数。数据库错误直接抛出。"""
    conn = connect("ro")
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            columns = [desc[0] for desc in cur.description] if cur.description else []
            rows = [
                {col: to_jsonable(val) for col, val in row.items()}
                for row in cur.fetchall()
            ]
        return {"columns": columns, "rows": rows, "row_count": len(rows)}
    finally:
        conn.close()
