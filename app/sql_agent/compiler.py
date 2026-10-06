"""编译器：把查询计划对照白名单目录编译成参数化的 MySQL 查询。纯函数，不调模型、不连数据库。"""
import re
from dataclasses import dataclass
from datetime import date, timedelta

from app import config
from app.sql_agent.catalog import CHAIN, JOIN_ON, CATALOG, Column
from app.sql_agent.plan import Filter, QueryPlan

# 每种列类型允许的操作符
OPS_BY_TYPE: dict[str, set[str]] = {
    "str": {"eq", "ne", "in"},
    "enum": {"eq", "ne", "in"},
    "int": {"eq", "ne", "gt", "gte", "lt", "lte", "between", "in"},
    "number": {"eq", "ne", "gt", "gte", "lt", "lte", "between", "in"},
    "date": {"eq", "ne", "gt", "gte", "lt", "lte", "between", "last_n_days"},
    "bool": {"eq", "ne"},
}
# 每种操作符需要的值的个数
VALUE_COUNTS: dict[str, tuple[int, int]] = {
    "between": (2, 2),
    "in": (1, 20),
}
COMPARISON = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
# 聚合函数允许的列类型；None 表示任意类型都行
METRIC_TYPES: dict[str, set[str] | None] = {
    "sum": {"int", "number"},
    "avg": {"int", "number"},
    "min": {"int", "number", "date"},
    "max": {"int", "number", "date"},
    "count": None,
    "count_distinct": None,
}
AGG_FUNCTION = {"sum": "SUM", "count": "COUNT", "avg": "AVG", "min": "MIN", "max": "MAX"}
ALIAS_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


@dataclass
class CompiledQuery:
    sql: str
    params: list


class PlanRejected(Exception):
    """计划有问题，模型可以自己改。kind: fixable / login_required / unanswerable。"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


class CompilerBug(Exception):
    """编译自己的问题：出口断言失败。"""


def _fixable(message: str) -> None:
    raise PlanRejected("fixable", message)


def resolve_column(ref: str) -> tuple[str, Column]:
    """把 "表.列" 解析成 (表名, 列)。不合法就拒绝，错误信息里给出可用的表和列。"""
    if ref.count(".") != 1:
        _fixable(f"列引用 {ref} 不合法，必须写成 表.列 的形式。"
                 f"可用的表: {', '.join(CATALOG)}")
    table_name, column_name = ref.split(".")
    table = CATALOG.get(table_name)
    if table is None:
        _fixable(f"未登记的表 {ref}。可用的表: {', '.join(CATALOG)}")
    column = table.columns.get(column_name)
    if column is None:
        available = ", ".join(f"{table_name}.{name}" for name in table.columns)
        _fixable(f"未登记的列 {ref}。{table_name} 表可用的列: {available}")
    return table_name, column


def _escape_like(value: str) -> str:
    """contains 的转义：先转义反斜杠，再转义 % 和 _。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _convert_value(ref: str, column: Column, value: str) -> object:
    """按列的类型把字符串转换成交给驱动的参数值。转换失败就拒绝。"""
    if column.type == "str":
        return value
    if column.type == "enum":
        if value not in column.enum_values:
            _fixable(f"{ref} 的取值 {value} 不合法，允许的取值: {', '.join(column.enum_values)}")
        return value
    if column.type == "int":
        try:
            return int(value)
        except ValueError:
            _fixable(f"{ref} 的取值 {value} 不是整数")
    if column.type == "number":
        try:
            return float(value)
        except ValueError:
            _fixable(f"{ref} 的取值 {value} 不是数字")
    if column.type == "date":
        try:
            date.fromisoformat(value)
        except ValueError:
            _fixable(f"{ref} 的取值 {value} 不是合法的日期（应当是 YYYY-MM-DD）")
        return value
    if column.type == "bool":
        lowered = value.lower()
        if lowered in ("true", "1"):
            return 1
        if lowered in ("false", "0"):
            return 0
        _fixable(f"{ref} 的取值 {value} 不是布尔值（应当是 true 或 false）")
    _fixable(f"{ref} 的类型 {column.type} 编译器还不认识")


def _today_to_date(today: str) -> date:
    try:
        return date.fromisoformat(today)
    except ValueError as exc:
        raise CompilerBug(f"today 不是合法的 ISO 日期: {today}") from exc


def _render_filter(item: Filter, today: str) -> tuple[str, list]:
    """生成一个过滤条件，返回 (SQL 片段, 参数列表)。"""
    table_name, column = resolve_column(item.column)
    ref = item.column
    allowed = set(OPS_BY_TYPE[column.type])
    if column.searchable:
        allowed.add("contains")
    if item.op not in allowed:
        _fixable(f"{ref} 是 {column.type} 类型，不支持 {item.op} 操作。"
                 f"支持的操作: {', '.join(sorted(allowed))}")
    low, high = VALUE_COUNTS.get(item.op, (1, 1))
    if not (low <= len(item.values) <= high):
        needed = "两个" if low == 2 else ("一到二十个" if high == 20 else "一个")
        _fixable(f"{ref} 的 {item.op} 需要{needed}值，当前给了 {len(item.values)} 个")

    expr = column.expr
    if item.op == "last_n_days":
        try:
            days = int(item.values[0])
        except ValueError:
            _fixable(f"last_n_days 的天数必须是整数，当前是 {item.values[0]}")
        if not 1 <= days <= 3650:
            _fixable(f"last_n_days 的天数必须在 1 到 3650 之间，当前是 {days}")
        start = _today_to_date(today) - timedelta(days=days)
        return f"{expr} >= %s", [start.isoformat()]

    if item.op == "between":
        values = [_convert_value(ref, column, v) for v in item.values]
        return f"{expr} BETWEEN %s AND %s", values
    if item.op == "in":
        values = [_convert_value(ref, column, v) for v in item.values]
        return f"{expr} IN ({', '.join(['%s'] * len(values))})", values
    if item.op == "contains":
        return f"{expr} LIKE %s", ["%" + _escape_like(item.values[0]) + "%"]
    if item.op in COMPARISON:
        return f"{expr} {COMPARISON[item.op]} %s", [_convert_value(ref, column, item.values[0])]
    if item.op == "eq":
        return f"{expr} = %s", [_convert_value(ref, column, item.values[0])]
    return f"{expr} <> %s", [_convert_value(ref, column, item.values[0])]


def compile_plan(plan: QueryPlan, customer_id: str | None, today: str) -> CompiledQuery:
    """把查询计划编译成参数化的 SQL。计划有问题时抛 PlanRejected。"""
    # 3.1 能否回答
    if not plan.answerable:
        raise PlanRejected("unanswerable", plan.unanswerable_reason)

    # 3.2 解析所有列引用
    refs: list[str] = []
    refs += list(plan.select)
    refs += [metric.column for metric in plan.metrics]
    refs += [item.column for item in plan.group_by]
    refs += [item.column for item in plan.filters]
    refs += [item.key for item in plan.order_by if "." in item.key]
    for ref in refs:
        resolve_column(ref)
    table_names = {resolve_column(ref)[0] for ref in refs}

    # 3.3 连接范围
    if not table_names:
        _fixable("select 和 metrics 不能同时为空")
    if "order_items" in table_names:
        table_names.add("orders")   # order_items 自己没有客户列，要靠 orders 加范围
    positions = [CHAIN.index(name) for name in table_names]
    scope = CHAIN[min(positions): max(positions) + 1]

    # 3.4 匿名用户不能碰私有的表
    if any(CATALOG[name].private for name in scope) and customer_id is None:
        raise PlanRejected("login_required", "查询订单或账户信息需要先登录")

    # 3.5 FROM 和 JOIN
    body: list[str] = [f"FROM {scope[0]} {CATALOG[scope[0]].alias}"]
    for left, right in zip(scope, scope[1:]):
        body.append(f"JOIN {right} {CATALOG[right].alias} ON {JOIN_ON[(left, right)]}")

    # 3.6 客户范围：WHERE 的第一个条件，参数也是参数列表的第一个
    params: list = []
    where: list[str] = []
    if "orders" in scope:
        where.append("o.customer_id = %s")
        params.append(customer_id)
    elif "customers" in scope:
        where.append("c.customer_id = %s")
        params.append(customer_id)

    # SELECT 和 GROUP BY
    select: list[str] = []
    group_by: list[str] = []
    grouped: dict[str, tuple[str, str]] = {}   # 列引用 -> (SELECT 里的标签, granularity)
    aliases: set[str] = set()                  # 聚合的名字，order_by 可以用它们排序
    if not plan.metrics:
        if not plan.select:
            _fixable("select 和 metrics 不能同时为空")
        if plan.group_by:
            _fixable("没有聚合时不能使用 group_by")
        for ref in plan.select:
            select.append(f"{resolve_column(ref)[1].expr} AS `{ref}`")
    else:
        for item in plan.group_by:
            column = resolve_column(item.column)[1]
            expr = column.expr
            if item.granularity == "none":
                piece = expr
                label = item.column
            elif item.granularity == "month":
                if column.type != "date":
                    _fixable(f"{item.column} 是 {column.type} 类型，granularity month 只适用于 date 类型的列")
                piece = f"DATE_FORMAT({expr}, '%%Y-%%m')"
                label = f"{item.column}_month"
            else:
                if column.type != "date":
                    _fixable(f"{item.column} 是 {column.type} 类型，granularity year 只适用于 date 类型的列")
                piece = f"YEAR({expr})"
                label = f"{item.column}_year"
            select.append(f"{piece} AS `{label}`")
            group_by.append(piece)
            grouped[item.column] = (label, item.granularity)
        for ref in plan.select:
            if grouped.get(ref, ("", ""))[1] != "none":
                _fixable("有聚合时，select 里的列必须同时出现在 group_by 里")
        for metric in plan.metrics:
            if not ALIAS_RE.match(metric.alias):
                _fixable(f"alias {metric.alias} 不合法：必须是小写字母开头，"
                         f"只含字母、数字和下划线，最长 32 个字符")
            if metric.alias in aliases:
                _fixable(f"alias {metric.alias} 重复了，每个聚合的名字都要独一无二")
            aliases.add(metric.alias)
            table_name, column = resolve_column(metric.column)
            permitted = METRIC_TYPES[metric.func]
            if permitted is not None and column.type not in permitted:
                _fixable(f"{table_name}.{metric.column.split('.')[1]} 是 {column.type} 类型，"
                         f"{metric.func} 不能用在它上面")
            if metric.func == "count_distinct":
                call = f"COUNT(DISTINCT {column.expr})"
            else:
                call = f"{AGG_FUNCTION[metric.func]}({column.expr})"
            select.append(f"{call} AS `{metric.alias}`")

    # 第五步：过滤条件，接在客户范围后面
    for item in plan.filters:
        condition, values = _render_filter(item, today)
        where.append(condition)
        params.extend(values)

    # 6.1 ORDER BY
    order_by: list[str] = []
    for item in plan.order_by:
        direction = "ASC" if item.direction == "asc" else "DESC"
        if item.key in aliases:
            order_by.append(f"`{item.key}` {direction}")
        elif "." not in item.key:
            _fixable(f"order_by 里的 {item.key} 既不是某个聚合的 alias，也不是 表.列 形式的列引用")
        elif not plan.metrics:
            order_by.append(f"{resolve_column(item.key)[1].expr} {direction}")
        elif item.key not in grouped:
            _fixable(f"有聚合时，order_by 里的 {item.key} 必须同时出现在 group_by 里")
        elif grouped[item.key][1] == "none":
            order_by.append(f"{resolve_column(item.key)[1].expr} {direction}")
        else:
            order_by.append(f"`{grouped[item.key][0]}` {direction}")

    # 6.2 LIMIT
    limit = plan.limit if 1 <= plan.limit <= config.SQL_ROW_LIMIT else config.SQL_ROW_LIMIT

    # 6.3 组装
    lines = [f"SELECT {', '.join(select)}", *body]
    if where:
        lines.append(f"WHERE {' AND '.join(where)}")
    if group_by:
        lines.append(f"GROUP BY {', '.join(group_by)}")
    if order_by:
        lines.append(f"ORDER BY {', '.join(order_by)}")
    lines.append(f"LIMIT {limit}")
    sql = "\n".join(lines)

    # 6.4 出口断言
    if any(CATALOG[name].private for name in scope):
        scoped = "o.customer_id = %s" in sql or "c.customer_id = %s" in sql
        if not scoped or customer_id is None or params[:1] != [customer_id]:
            raise CompilerBug("私有表没有加上客户范围，拒绝返回这条 SQL")

    return CompiledQuery(sql=sql, params=params)
