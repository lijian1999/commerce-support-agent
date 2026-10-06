# 任务 02：SQL Agent 的查询计划结构、白名单目录、编译器

先读 `AGENTS.md`，再读 `docs/contracts.md` 的第 5.1、5.2 节。本任务新建这些文件：`app/sql_agent/plan.py`、`app/sql_agent/catalog.py`、`app/sql_agent/compiler.py`。测试文件 `tests/test_compiler.py` 已经写好，**只读**，不要改它。不要改别的文件。

## 背景

模型不写 SQL，只输出一份结构化的查询计划（`QueryPlan`）。编译器 `compile_plan` 是确定性代码：对照白名单目录检查计划，自己推导 JOIN，对客户私有的表强制加上 `customer_id = 当前客户`，生成参数化的 MySQL 查询（PyMySQL 的 `%s` 占位符）。本任务不涉及模型和数据库，全部是纯函数。

## 第一步：`app/sql_agent/plan.py`

把 `docs/contracts.md` 第 5.1 节的 5 个 pydantic 类原样写进去（`Metric`、`GroupBy`、`Filter`、`OrderBy`、`QueryPlan`），字段顺序不变，不加默认值。每个字段加 `Field(description="...")`，用一句中文说明含义，例如 `values` 的说明是「值一律写成字符串；between 写两个值，in 写一到二十个值，其余写一个值」。

```sh
uv run python -c "from app.sql_agent.plan import QueryPlan; print(list(QueryPlan.model_fields))"
```

应输出 `['reasoning', 'answerable', 'unanswerable_reason', 'select', 'metrics', 'group_by', 'filters', 'order_by', 'limit']`。

## 第二步：`app/sql_agent/catalog.py`

定义白名单目录和一个把它渲染成提示词文本的函数。

```python
@dataclass(frozen=True)
class Column:
    type: str                         # "str" / "enum" / "int" / "number" / "date" / "bool"
    expr: str                         # 这一列在 SQL 里的表达式，含表别名
    description: str                  # 中文说明，给模型看
    searchable: bool = False          # 只有 True 的列能用 contains
    enum_values: tuple[str, ...] = () # type 为 enum 时的允许取值

@dataclass(frozen=True)
class Table:
    alias: str
    private: bool                     # True 表示客户私有，必须加客户范围
    description: str
    columns: dict[str, Column]

CHAIN = ["customers", "orders", "order_items", "products"]   # 4 张表连成一条链
JOIN_ON = {                                                   # 链上相邻两张表的连接条件，键是 (左表, 右表)
    ("customers", "orders"): "o.customer_id = c.customer_id",
    ("orders", "order_items"): "oi.order_id = o.order_id",
    ("order_items", "products"): "p.product_id = oi.product_id",
}
CATALOG: dict[str, Table]
def catalog_prompt() -> str
```

`CATALOG` 的内容（表 → 别名、是否私有；列 → 类型、表达式）：

| 表 | 别名 | 私有 | 列（类型） |
|---|---|---|---|
| `customers` | `c` | 是 | `name`(str)、`email`(str)、`phone`(str)、`city`(str)、`state`(str)、`segment`(enum: `Consumer`、`Corporate`、`Home Office`) |
| `orders` | `o` | 是 | `order_id`(str)、`order_date`(date)、`status`(enum: `Processing`、`Shipped`、`Delivered`、`Cancelled`)、`shipped_date`(date)、`tracking_number`(str)、`total_amount`(number)、`cancelled_at`(date) |
| `order_items` | `oi` | 是 | `quantity`(int)、`price_per_unit`(number)、`line_total`(number) |
| `products` | `p` | 否 | `product_id`(str)、`name`(str，searchable)、`category`(enum: `Laptops`、`Monitors`、`Keyboards`、`Audio`、`Accessories`)、`price`(number)、`in_stock`(bool) |

- 列的 `expr` 是 `别名.列名`，例如 `orders.status` 的 `expr` 是 `o.status`。唯一的例外是虚拟列 `order_items.line_total`，它的 `expr` 是 `(oi.quantity * oi.price_per_unit)`。
- 任何表的 `customer_id` 列都**不在**目录里。`order_items` 的 `order_id`、`product_id`、`order_item_id` 也不在目录里。
- 只有 `products.name` 的 `searchable` 是 `True`。
- `description` 自己写，每列一句中文（例如 `orders.tracking_number` →「物流单号，未发货时为空」；`order_items.line_total` →「这一行明细的金额，等于数量乘以成交单价」；`products.price` →「当前标价，不是成交价」；`order_items.price_per_unit` →「下单时的成交单价」）。

`catalog_prompt()` 返回一段文本，按 `CHAIN` 的顺序每张表一段，格式如下（枚举列在类型后面列出取值，searchable 的列标注「可用 contains」）：

```
表 orders（客户私有）：订单
  - orders.order_id (str)：订单号，格式 ORD-年份-四位数字
  - orders.status (enum: Processing / Shipped / Delivered / Cancelled)：订单状态
  ...
表 products（公开）：商品目录
  - products.name (str，可用 contains)：商品名称
  ...
```

```sh
uv run python -c "
from app.sql_agent.catalog import CATALOG, CHAIN, catalog_prompt
print(CHAIN, sorted(CATALOG), sum(len(t.columns) for t in CATALOG.values()))
print(CATALOG['order_items'].columns['line_total'].expr, CATALOG['orders'].columns['status'].expr)
print(any('customer_id' in t.columns for t in CATALOG.values()), 'orders.status (enum: Processing / Shipped / Delivered / Cancelled)' in catalog_prompt())"
```

应输出三行：

```
['customers', 'orders', 'order_items', 'products'] ['customers', 'order_items', 'orders', 'products'] 21
(oi.quantity * oi.price_per_unit) o.status
False True
```

## 第三步：`app/sql_agent/compiler.py` 的骨架、列解析、JOIN 和客户范围

```python
@dataclass
class CompiledQuery:
    sql: str
    params: list

class PlanRejected(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind          # "fixable" / "login_required" / "unanswerable"
        self.message = message

class CompilerBug(Exception): ...

def compile_plan(plan: QueryPlan, customer_id: str | None, today: str) -> CompiledQuery
```

`compile_plan` 按下面的顺序做。下文所有「拒绝」都是 `raise PlanRejected("fixable", 错误信息)`，除非写明了别的 kind。

**3.1 能否回答。** `plan.answerable` 为 `False` → `raise PlanRejected("unanswerable", plan.unanswerable_reason)`。

**3.2 解析列引用。** 写一个内部函数把 `"表.列"` 字符串解析成 `(表名, Column)`。字符串里没有恰好一个 `.`、表不在 `CATALOG`、或列不在该表的目录里，都拒绝。错误信息必须包含被引用的原字符串；表存在而列不存在时，错误信息里还要列出这张表所有可用的列（`表.列` 形式，逗号分隔）；表不存在时列出所有可用的表名。例如：

```
未登记的列 orders.customer_id。orders 表可用的列: orders.order_id, orders.order_date, orders.status, orders.shipped_date, orders.tracking_number, orders.total_amount, orders.cancelled_at
```

计划里所有的列引用都要解析：`select` 的每一项、每个 `metrics[i].column`、每个 `group_by[i].column`、每个 `filters[i].column`、以及 `order_by[i].key` 中含有 `.` 的。

**3.3 算出要连哪些表。**
- 「引用到的表」= 上一步解析出的所有表名的集合。集合为空（计划没有引用任何列）→ 拒绝（「select 和 metrics 不能同时为空」）。
- 如果集合里有 `order_items`，把 `orders` 也加进去（`order_items` 自己没有客户列，要靠 `orders` 加范围）。
- 「连接范围」= `CHAIN` 中从集合里下标最小的表到下标最大的表之间的所有表（含两端）。例如引用了 `orders` 和 `products`，连接范围是 `orders`、`order_items`、`products`。

**3.4 匿名用户。** 连接范围里有任何 `private` 为 `True` 的表，且 `customer_id` 是 `None` → `raise PlanRejected("login_required", "查询订单或账户信息需要先登录")`。

**3.5 FROM 和 JOIN。** 连接范围按 `CHAIN` 的顺序排列，第一张表放在 `FROM`，其余的依次 `JOIN`，条件取自 `JOIN_ON`：

```
FROM orders o
JOIN order_items oi ON oi.order_id = o.order_id
JOIN products p ON p.product_id = oi.product_id
```

**3.6 客户范围。** 这是 `WHERE` 的第一个条件，它的参数是参数列表的第一个：
- 连接范围里有 `orders` → 条件 `o.customer_id = %s`，参数 `customer_id`。
- 否则连接范围里有 `customers` → 条件 `c.customer_id = %s`，参数 `customer_id`。
- 连接范围只有 `products` → 不加。

## 第四步：SELECT、聚合、分组

**4.1 SELECT 列表。** 每一项的写法是 `` 表达式 AS `标签` ``。

没有 `metrics` 时：
- `select` 为空 → 拒绝（「select 和 metrics 不能同时为空」）。
- `group_by` 非空 → 拒绝（「没有聚合时不能使用 group_by」）。
- 每个 `select` 项输出 `` <列的 expr> AS `<表.列>` ``，例如 `` o.order_id AS `orders.order_id` ``。

有 `metrics` 时，SELECT 列表 = 所有 `group_by` 项（按顺序）+ 所有 `metrics` 项（按顺序）：
- `select` 里的项如果同时出现在 `group_by` 里且该 `group_by` 的 `granularity` 是 `none`，忽略它；否则拒绝（「有聚合时，select 里的列必须同时出现在 group_by 里」）。
- `group_by` 项：
  - `granularity` 为 `none` → `` <expr> AS `<表.列>` ``
  - `month`（只允许 `date` 类型的列，否则拒绝）→ `` DATE_FORMAT(<expr>, '%%Y-%%m') AS `<表.列>_month` ``（注意 `%%`：PyMySQL 会把 SQL 文本里的 `%%` 还原成 `%`）
  - `year`（只允许 `date` 类型的列，否则拒绝）→ `` YEAR(<expr>) AS `<表.列>_year` ``
- `metrics` 项：
  - `alias` 必须匹配正则 `^[a-z][a-z0-9_]{0,31}$`，且在 `metrics` 之间不重复，否则拒绝。
  - `sum`、`avg` 只允许 `int`、`number` 类型的列；`min`、`max` 只允许 `int`、`number`、`date`；`count`、`count_distinct` 任意列。不符合就拒绝。
  - 写法：`` SUM(<expr>) AS `<alias>` ``、`COUNT(<expr>)`、`COUNT(DISTINCT <expr>)`、`AVG(<expr>)`、`MIN(<expr>)`、`MAX(<expr>)`。

**4.2 GROUP BY。** 有 `group_by` 时输出 `GROUP BY`，每一项是 SELECT 里对应那一项 `AS` 之前的表达式，逗号加空格分隔。例如 `GROUP BY p.category, DATE_FORMAT(o.order_date, '%%Y-%%m')`。

## 第五步：过滤条件

`WHERE` 的条件之间一律用 ` AND ` 连接，顺序是：客户范围（如果有）、然后 `filters` 按原顺序。没有任何条件时不输出 `WHERE`。

每个 `filters` 项先检查，再生成。

**5.1 操作符是否适用于这一列的类型**（不适用就拒绝，错误信息里写出列和操作符）：

| 列类型 | 允许的 op |
|---|---|
| `str` | `eq`、`ne`、`in`；`searchable` 为 `True` 的列另外允许 `contains` |
| `enum` | `eq`、`ne`、`in` |
| `int`、`number` | `eq`、`ne`、`gt`、`gte`、`lt`、`lte`、`between`、`in` |
| `date` | `eq`、`ne`、`gt`、`gte`、`lt`、`lte`、`between`、`last_n_days` |
| `bool` | `eq`、`ne` |

**5.2 值的个数**：`between` 恰好 2 个；`in` 1 到 20 个；其余恰好 1 个。不符合就拒绝。

**5.3 值的类型转换**（`values` 里都是字符串，逐个转换，转换失败就拒绝；`last_n_days` 的值不按列类型转换，见 5.4）：

| 列类型 | 转换 |
|---|---|
| `str` | 原样 |
| `enum` | 必须是 `enum_values` 之一（区分大小写），否则拒绝，错误信息里列出允许的取值，例如「products.category 的取值 键盘 不合法，允许的取值: Laptops, Monitors, Keyboards, Audio, Accessories」 |
| `int` | `int(...)` |
| `number` | `float(...)` |
| `date` | 用 `datetime.date.fromisoformat` 校验，参数仍然传原字符串 |
| `bool` | `"true"`、`"1"` → `1`；`"false"`、`"0"` → `0`（不区分大小写）；其他拒绝 |

**5.4 生成**（`E` 是列的 `expr`）：

| op | SQL | 参数 |
|---|---|---|
| `eq` | `E = %s` | 转换后的值 |
| `ne` | `E <> %s` | 转换后的值 |
| `gt` / `gte` / `lt` / `lte` | `E > %s` / `E >= %s` / `E < %s` / `E <= %s` | 转换后的值 |
| `between` | `E BETWEEN %s AND %s` | 两个值 |
| `in` | `E IN (%s, %s, ...)` | 每个值一个占位符 |
| `contains` | `E LIKE %s` | `"%" + 转义后的值 + "%"`。转义：先把值里的 `\` 换成 `\\`，再把 `%` 换成 `\%`，再把 `_` 换成 `\_` |
| `last_n_days` | `E >= %s` | 值必须是 1 到 3650 的整数 N，否则拒绝；参数是 `today` 减 N 天的 ISO 日期字符串。例如 `today="2025-10-20"`、N=30 → `"2025-09-20"` |

## 第六步：排序、行数、组装、出口断言

**6.1 ORDER BY。** 每个 `order_by` 项：
- `key` 等于某个 `metrics` 的 `alias` → `` `<alias>` ASC `` 或 `DESC`。
- 否则 `key` 必须是一个列引用（按 3.2 解析）。
  - 没有 `metrics` 时 → `<expr> ASC|DESC`。
  - 有 `metrics` 时，这一列必须出现在 `group_by` 里，否则拒绝；`granularity` 为 `none` → `<expr> ASC|DESC`，为 `month` 或 `year` → 用 SELECT 里那一项的标签：`` `<表.列>_month` ASC|DESC ``。
- 多项用逗号加空格分隔。

**6.2 LIMIT。** `plan.limit` 小于 1 或大于 `config.SQL_ROW_LIMIT` 时用 `SQL_ROW_LIMIT`，否则用 `plan.limit`。总是输出 `LIMIT <整数>`。

**6.3 组装。** 各子句按 `SELECT`、`FROM`、`JOIN`…、`WHERE`、`GROUP BY`、`ORDER BY`、`LIMIT` 的顺序，每个子句（每个 `JOIN` 也算一个）占一行，用 `\n` 连接。SELECT 列表的各项用逗号加空格分隔。参数列表的顺序和 `%s` 在 SQL 里出现的顺序一致。

**6.4 出口断言。** 返回之前检查：如果连接范围里有私有表，那么 SQL 文本必须包含 `o.customer_id = %s` 或 `c.customer_id = %s`，并且 `params[0] == customer_id`、`customer_id` 不是 `None`。不满足就 `raise CompilerBug(...)`，不返回任何 SQL。

**例子**（`customer_id="CUST-001"`）：计划 `metrics=[sum(order_items.line_total) 取名 total_spent]`、`filters=[products.category eq Keyboards]`、`limit=0`，其余为空，编译结果：

```
SELECT SUM((oi.quantity * oi.price_per_unit)) AS `total_spent`
FROM orders o
JOIN order_items oi ON oi.order_id = o.order_id
JOIN products p ON p.product_id = oi.product_id
WHERE o.customer_id = %s AND p.category = %s
LIMIT 50
params = ['CUST-001', 'Keyboards']
```

## 最后一步：收尾检查

```sh
uv run pytest tests/test_compiler.py -q
```

通过的标准：输出里没有 `failed`、没有 `error`，全部测试 `passed`。全部通过后最后一行输出 `DONE`。
