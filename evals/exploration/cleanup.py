"""把探查改动过的数据库内容复原：两张会被取消的订单改回处理中，删掉探针产生的取消记录和工单。

做法和 tests/test_cancel.py、tests/test_handoff.py 里的复原 fixture 相同，只动会话编号以 probe- 开头的记录。
用法: uv run python evals/exploration/cleanup.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.db import connect

conn = connect("root")
try:
    with conn.cursor() as cur:
        cur.execute("UPDATE orders SET status = 'Processing', cancelled_at = NULL "
                    "WHERE order_id IN ('ORD-2025-0031', 'ORD-2025-0038')")
        cur.execute("DELETE FROM order_cancellations WHERE session_id LIKE %s", ["probe-%"])
        cancelled = cur.rowcount
        cur.execute("DELETE FROM handoff_tickets WHERE session_id LIKE %s", ["probe-%"])
        tickets = cur.rowcount
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT (SELECT COUNT(*) FROM orders WHERE status = 'Processing') AS processing, "
                    "(SELECT COUNT(*) FROM order_cancellations) AS cancellations, "
                    "(SELECT COUNT(*) FROM handoff_tickets) AS tickets")
        print(f"删掉取消记录 {cancelled} 条、工单 {tickets} 张；现在: {cur.fetchone()}")
finally:
    conn.close()
