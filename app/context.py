"""Runtime Context：由调用方在发起请求时设定，图内的节点只能读。"""
from dataclasses import dataclass

from app.config import DEFAULT_TODAY


@dataclass(frozen=True)
class AppContext:
    customer_id: str | None = None   # None 表示匿名用户。只有 SQL 编译器和取消订单的节点读它
    today: str = DEFAULT_TODAY       # 今天的日期，ISO 格式
    fault: str = "off"               # 模拟故障：off / primary_down / all_down
    index_version: str | None = None # 请求开始时读到的知识库线上版本，本次请求的所有检索都用它
    session_id: str = ""             # 会话编号
