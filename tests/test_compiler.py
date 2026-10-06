"""SQL 编译器的单元测试：不调模型、不连数据库。"""
import itertools

import pytest

from app.sql_agent.catalog import CATALOG
from app.sql_agent.compiler import CompiledQuery, PlanRejected, compile_plan
from app.sql_agent.plan import QueryPlan

CUSTOMER = "CUST-001"
TODAY = "2025-10-20"


def make_plan(**fields) -> QueryPlan:
    base = dict(reasoning="", answerable=True, unanswerable_reason="", select=[], metrics=[],
                group_by=[], filters=[], order_by=[], limit=0)
    base.update(fields)
    return QueryPlan.model_validate(base)


def norm(sql: str) -> str:
    return " ".join(sql.split())


def compile_ok(customer_id=CUSTOMER, **fields) -> CompiledQuery:
    return compile_plan(make_plan(**fields), customer_id, TODAY)


def rejected(customer_id=CUSTOMER, **fields) -> PlanRejected:
    with pytest.raises(PlanRejected) as info:
        compile_plan(make_plan(**fields), customer_id, TODAY)
    return info.value


def f(column, op, *values):
    return {"column": column, "op": op, "values": list(values)}


# ---------- 完整 SQL 的例子 ----------

def test_latest_order():
    q = compile_ok(select=["orders.order_id", "orders.status", "orders.tracking_number"],
                   order_by=[{"key": "orders.order_date", "direction": "desc"}], limit=1)
    assert norm(q.sql) == (
        "SELECT o.order_id AS `orders.order_id`, o.status AS `orders.status`, "
        "o.tracking_number AS `orders.tracking_number` "
        "FROM orders o WHERE o.customer_id = %s ORDER BY o.order_date DESC LIMIT 1")
    assert q.params == [CUSTOMER]


def test_keyboard_spend():
    q = compile_ok(metrics=[{"func": "sum", "column": "order_items.line_total", "alias": "total_spent"}],
                   filters=[f("products.category", "eq", "Keyboards")])
    assert norm(q.sql) == (
        "SELECT SUM((oi.quantity * oi.price_per_unit)) AS `total_spent` "
        "FROM orders o "
        "JOIN order_items oi ON oi.order_id = o.order_id "
        "JOIN products p ON p.product_id = oi.product_id "
        "WHERE o.customer_id = %s AND p.category = %s "
        "LIMIT 50")
    assert q.params == [CUSTOMER, "Keyboards"]


def test_public_products_no_scope():
    q = compile_ok(customer_id=None, select=["products.name", "products.price"],
                   filters=[f("products.category", "eq", "Laptops"), f("products.in_stock", "eq", "true")],
                   order_by=[{"key": "products.price", "direction": "asc"}], limit=10)
    assert norm(q.sql) == (
        "SELECT p.name AS `products.name`, p.price AS `products.price` "
        "FROM products p WHERE p.category = %s AND p.in_stock = %s ORDER BY p.price ASC LIMIT 10")
    assert q.params == ["Laptops", 1]


def test_group_by_month_and_category():
    q = compile_ok(
        select=["products.category"],
        metrics=[{"func": "sum", "column": "order_items.line_total", "alias": "spent"},
                 {"func": "count_distinct", "column": "orders.order_id", "alias": "order_count"}],
        group_by=[{"column": "products.category", "granularity": "none"},
                  {"column": "orders.order_date", "granularity": "month"}],
        filters=[f("orders.order_date", "between", "2024-01-01", "2024-12-31")],
        order_by=[{"key": "orders.order_date", "direction": "asc"}, {"key": "spent", "direction": "desc"}],
        limit=20)
    assert norm(q.sql) == (
        "SELECT p.category AS `products.category`, "
        "DATE_FORMAT(o.order_date, '%%Y-%%m') AS `orders.order_date_month`, "
        "SUM((oi.quantity * oi.price_per_unit)) AS `spent`, "
        "COUNT(DISTINCT o.order_id) AS `order_count` "
        "FROM orders o "
        "JOIN order_items oi ON oi.order_id = o.order_id "
        "JOIN products p ON p.product_id = oi.product_id "
        "WHERE o.customer_id = %s AND o.order_date BETWEEN %s AND %s "
        "GROUP BY p.category, DATE_FORMAT(o.order_date, '%%Y-%%m') "
        "ORDER BY `orders.order_date_month` ASC, `spent` DESC "
        "LIMIT 20")
    assert q.params == [CUSTOMER, "2024-01-01", "2024-12-31"]


def test_group_by_year():
    q = compile_ok(metrics=[{"func": "count", "column": "orders.order_id", "alias": "n"}],
                   group_by=[{"column": "orders.order_date", "granularity": "year"}])
    assert norm(q.sql) == (
        "SELECT YEAR(o.order_date) AS `orders.order_date_year`, COUNT(o.order_id) AS `n` "
        "FROM orders o WHERE o.customer_id = %s GROUP BY YEAR(o.order_date) LIMIT 50")


def test_customers_only_scope():
    q = compile_ok(select=["customers.name", "customers.email"])
    assert norm(q.sql) == (
        "SELECT c.name AS `customers.name`, c.email AS `customers.email` "
        "FROM customers c WHERE c.customer_id = %s LIMIT 50")
    assert q.params == [CUSTOMER]


def test_full_chain_join():
    q = compile_ok(select=["customers.name", "products.name"])
    assert norm(q.sql) == (
        "SELECT c.name AS `customers.name`, p.name AS `products.name` "
        "FROM customers c "
        "JOIN orders o ON o.customer_id = c.customer_id "
        "JOIN order_items oi ON oi.order_id = o.order_id "
        "JOIN products p ON p.product_id = oi.product_id "
        "WHERE o.customer_id = %s LIMIT 50")
    assert q.params == [CUSTOMER]


def test_sql_is_one_clause_per_line():
    q = compile_ok(select=["orders.order_id"], filters=[f("products.category", "eq", "Audio")])
    lines = q.sql.split("\n")
    assert [line.split()[0] for line in lines] == ["SELECT", "FROM", "JOIN", "JOIN", "WHERE", "LIMIT"]


# ---------- 过滤条件 ----------

def test_filter_ops():
    q = compile_ok(select=["orders.order_id"], filters=[
        f("orders.status", "ne", "Cancelled"),
        f("orders.total_amount", "gt", "100"),
        f("orders.total_amount", "lte", "500.5"),
        f("orders.status", "in", "Shipped", "Delivered"),
        f("orders.order_date", "gte", "2025-01-01"),
        f("orders.order_date", "lt", "2025-06-01"),
    ])
    assert norm(q.sql) == (
        "SELECT o.order_id AS `orders.order_id` FROM orders o "
        "WHERE o.customer_id = %s AND o.status <> %s AND o.total_amount > %s AND o.total_amount <= %s "
        "AND o.status IN (%s, %s) AND o.order_date >= %s AND o.order_date < %s LIMIT 50")
    assert q.params == [CUSTOMER, "Cancelled", 100.0, 500.5, "Shipped", "Delivered", "2025-01-01", "2025-06-01"]


def test_int_and_bool_values():
    q = compile_ok(select=["orders.order_id"], filters=[f("order_items.quantity", "gte", "2"),
                                                       f("products.in_stock", "eq", "False")])
    assert q.params == [CUSTOMER, 2, 0]
    assert isinstance(q.params[1], int)


def test_last_n_days():
    q = compile_ok(select=["orders.order_id"], filters=[f("orders.order_date", "last_n_days", "30")])
    assert norm(q.sql).endswith("WHERE o.customer_id = %s AND o.order_date >= %s LIMIT 50")
    assert q.params == [CUSTOMER, "2025-09-20"]


def test_contains_escapes_wildcards():
    q = compile_ok(customer_id=None, select=["products.name"], filters=[f("products.name", "contains", "100%_a\\b")])
    assert "p.name LIKE %s" in q.sql
    assert q.params == ["%100\\%\\_a\\\\b%"]


def test_values_never_appear_in_sql_text():
    evil = "x' OR '1'='1"
    q = compile_ok(select=["orders.order_id"], filters=[f("orders.order_id", "eq", evil)])
    assert evil not in q.sql
    assert q.params == [CUSTOMER, evil]


# ---------- LIMIT ----------

@pytest.mark.parametrize("limit, expected", [(0, 50), (-5, 50), (1, 1), (50, 50), (51, 50), (1000, 50)])
def test_limit_is_always_enforced(limit, expected):
    q = compile_ok(select=["orders.order_id"], limit=limit)
    assert q.sql.split("\n")[-1] == f"LIMIT {expected}"


# ---------- 身份：客户范围一定来自 Runtime ----------

ONE_COLUMN = {"customers": "customers.city", "orders": "orders.order_id",
              "order_items": "order_items.quantity", "products": "products.name"}
TABLE_SUBSETS = [list(c) for n in range(1, 5) for c in itertools.combinations(ONE_COLUMN, n)]
PRIVATE = {"customers", "orders", "order_items"}


@pytest.mark.parametrize("tables", TABLE_SUBSETS)
def test_every_private_combination_is_scoped(tables):
    """枚举 4 张表的全部 15 种组合：只要涉及私有表，SQL 就带客户范围，且参数来自 Runtime。"""
    q = compile_ok(customer_id="CUST-777", select=[ONE_COLUMN[t] for t in tables])
    if PRIVATE & set(tables):
        assert "o.customer_id = %s" in q.sql or "c.customer_id = %s" in q.sql
        assert q.params[0] == "CUST-777"
        assert q.sql.count("customer_id = %s") == 1
    else:
        assert "customer_id" not in q.sql
        assert q.params == []


@pytest.mark.parametrize("tables", TABLE_SUBSETS)
def test_anonymous_cannot_touch_private_tables(tables):
    fields = dict(select=[ONE_COLUMN[t] for t in tables])
    if PRIVATE & set(tables):
        assert rejected(customer_id=None, **fields).kind == "login_required"
    else:
        assert compile_ok(customer_id=None, **fields).params == []


@pytest.mark.parametrize("via", ["select", "filter", "metric", "group_by", "order_by"])
def test_scope_added_when_private_table_only_appears_in(via):
    """私有表不管出现在计划的哪个位置，都会触发客户范围。"""
    fields = {
        "select": dict(select=["products.name", "orders.status"]),
        "filter": dict(select=["products.name"], filters=[f("orders.status", "eq", "Delivered")]),
        "metric": dict(metrics=[{"func": "sum", "column": "order_items.quantity", "alias": "units"}],
                       group_by=[{"column": "products.name", "granularity": "none"}]),
        "group_by": dict(metrics=[{"func": "count", "column": "products.product_id", "alias": "n"}],
                         group_by=[{"column": "orders.status", "granularity": "none"}]),
        "order_by": dict(select=["products.name"], order_by=[{"key": "orders.order_date", "direction": "desc"}]),
    }[via]
    q = compile_ok(customer_id="CUST-042", **fields)
    assert "WHERE o.customer_id = %s" in q.sql
    assert q.params[0] == "CUST-042"
    assert rejected(customer_id=None, **fields).kind == "login_required"


def test_customer_id_column_is_not_referencable():
    for column in ["orders.customer_id", "customers.customer_id"]:
        err = rejected(select=["orders.order_id"], filters=[f(column, "eq", "CUST-002")])
        assert err.kind == "fixable"
        assert column in err.message


def test_filter_cannot_widen_scope():
    """模型给的条件只能和客户范围并列（AND），SQL 里不会出现 OR。"""
    q = compile_ok(select=["orders.order_id"], filters=[f("orders.status", "in", "Shipped", "Delivered"),
                                                       f("orders.order_id", "eq", "ORD-2024-0001")])
    assert " OR " not in q.sql.upper().replace("ORDER", "")
    assert norm(q.sql).split("WHERE ")[1].startswith("o.customer_id = %s AND ")


# ---------- 被拒绝的计划 ----------

def test_unanswerable():
    err = rejected(answerable=False, unanswerable_reason="需要子查询")
    assert err.kind == "unanswerable"
    assert err.message == "需要子查询"


def test_unknown_column_lists_available_columns():
    err = rejected(select=["orders.price"])
    assert err.kind == "fixable"
    assert "orders.price" in err.message
    for name in CATALOG["orders"].columns:
        assert f"orders.{name}" in err.message


def test_unknown_table_lists_tables():
    err = rejected(select=["payments.amount"])
    assert err.kind == "fixable"
    assert "payments.amount" in err.message
    for table in CATALOG:
        assert table in err.message


def test_enum_value_error_lists_allowed_values():
    err = rejected(select=["products.name"], filters=[f("products.category", "eq", "键盘")])
    assert err.kind == "fixable"
    for value in ["Laptops", "Monitors", "Keyboards", "Audio", "Accessories"]:
        assert value in err.message


FIXABLE_PLANS = {
    "malformed_column": dict(select=["order_id"]),
    "empty_select_and_metrics": dict(),
    "group_by_without_metrics": dict(select=["orders.status"],
                                     group_by=[{"column": "orders.status", "granularity": "none"}]),
    "select_not_in_group_by": dict(select=["orders.order_id"],
                                   metrics=[{"func": "count", "column": "orders.order_id", "alias": "n"}]),
    "month_on_non_date": dict(metrics=[{"func": "count", "column": "orders.order_id", "alias": "n"}],
                              group_by=[{"column": "orders.status", "granularity": "month"}]),
    "bad_alias": dict(metrics=[{"func": "count", "column": "orders.order_id", "alias": "n; DROP TABLE orders"}]),
    "uppercase_alias": dict(metrics=[{"func": "count", "column": "orders.order_id", "alias": "Total"}]),
    "duplicate_alias": dict(metrics=[{"func": "count", "column": "orders.order_id", "alias": "n"},
                                     {"func": "sum", "column": "orders.total_amount", "alias": "n"}]),
    "sum_on_text": dict(metrics=[{"func": "sum", "column": "orders.status", "alias": "s"}]),
    "max_on_text": dict(metrics=[{"func": "max", "column": "products.name", "alias": "m"}]),
    "contains_on_non_searchable": dict(select=["orders.order_id"], filters=[f("orders.tracking_number", "contains", "1Z")]),
    "gt_on_enum": dict(select=["orders.order_id"], filters=[f("orders.status", "gt", "Shipped")]),
    "last_n_days_on_number": dict(select=["orders.order_id"], filters=[f("orders.total_amount", "last_n_days", "7")]),
    "between_needs_two": dict(select=["orders.order_id"], filters=[f("orders.total_amount", "between", "1")]),
    "eq_needs_one": dict(select=["orders.order_id"], filters=[f("orders.status", "eq", "Shipped", "Delivered")]),
    "in_needs_values": dict(select=["orders.order_id"], filters=[f("orders.status", "in")]),
    "bad_number": dict(select=["orders.order_id"], filters=[f("orders.total_amount", "gt", "很多")]),
    "bad_date": dict(select=["orders.order_id"], filters=[f("orders.order_date", "gte", "去年")]),
    "bad_bool": dict(select=["products.name"], filters=[f("products.in_stock", "eq", "maybe")]),
    "bad_n_days": dict(select=["orders.order_id"], filters=[f("orders.order_date", "last_n_days", "0")]),
    "order_by_unknown": dict(select=["orders.order_id"], order_by=[{"key": "total", "direction": "desc"}]),
    "order_by_not_grouped": dict(metrics=[{"func": "count", "column": "orders.order_id", "alias": "n"}],
                                 order_by=[{"key": "orders.order_date", "direction": "desc"}]),
}


@pytest.mark.parametrize("name", sorted(FIXABLE_PLANS))
def test_fixable_rejections(name):
    err = rejected(**FIXABLE_PLANS[name])
    assert err.kind == "fixable"
    assert err.message


def test_select_in_group_by_is_tolerated():
    q = compile_ok(select=["orders.status"],
                   metrics=[{"func": "count", "column": "orders.order_id", "alias": "n"}],
                   group_by=[{"column": "orders.status", "granularity": "none"}],
                   order_by=[{"key": "orders.status", "direction": "asc"}])
    assert norm(q.sql) == (
        "SELECT o.status AS `orders.status`, COUNT(o.order_id) AS `n` FROM orders o "
        "WHERE o.customer_id = %s GROUP BY o.status ORDER BY o.status ASC LIMIT 50")


def test_min_max_on_date_and_avg():
    q = compile_ok(metrics=[{"func": "min", "column": "orders.order_date", "alias": "first_order"},
                            {"func": "max", "column": "orders.order_date", "alias": "last_order"},
                            {"func": "avg", "column": "orders.total_amount", "alias": "avg_amount"}])
    assert norm(q.sql).startswith(
        "SELECT MIN(o.order_date) AS `first_order`, MAX(o.order_date) AS `last_order`, "
        "AVG(o.total_amount) AS `avg_amount` FROM orders o WHERE")
