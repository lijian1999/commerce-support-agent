"""SQL Agent 的白名单目录：允许查询的表、列，以及把它们渲染成提示词文本。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Column:
    type: str                          # "str" / "enum" / "int" / "number" / "date" / "bool"
    expr: str                          # 这一列在 SQL 里的表达式，含表别名
    description: str                   # 中文说明，给模型看
    searchable: bool = False           # 只有 True 的列能用 contains
    enum_values: tuple[str, ...] = ()  # type 为 enum 时的允许取值


@dataclass(frozen=True)
class Table:
    alias: str
    private: bool                      # True 表示客户私有，必须加客户范围
    description: str
    columns: dict[str, Column]


# 4 张表连成一条链，编译时按这条链决定 FROM 和 JOIN
CHAIN = ["customers", "orders", "order_items", "products"]

# 链上相邻两张表的连接条件，键是 (左表, 右表)
JOIN_ON = {
    ("customers", "orders"): "o.customer_id = c.customer_id",
    ("orders", "order_items"): "oi.order_id = o.order_id",
    ("order_items", "products"): "p.product_id = oi.product_id",
}

CATALOG: dict[str, Table] = {
    "customers": Table(
        alias="c",
        private=True,
        description="客户",
        columns={
            "name": Column("str", "c.name", "客户姓名"),
            "email": Column("str", "c.email", "邮箱地址"),
            "phone": Column("str", "c.phone", "联系电话"),
            "city": Column("str", "c.city", "所在城市"),
            "state": Column("str", "c.state", "所在州或地区"),
            "segment": Column("enum", "c.segment", "客户细分群体",
                              enum_values=("Consumer", "Corporate", "Home Office")),
        },
    ),
    "orders": Table(
        alias="o",
        private=True,
        description="订单",
        columns={
            "order_id": Column("str", "o.order_id", "订单号，格式 ORD-年份-四位数字"),
            "order_date": Column("date", "o.order_date", "下单日期"),
            "status": Column("enum", "o.status", "订单状态",
                             enum_values=("Processing", "Shipped", "Delivered", "Cancelled")),
            "shipped_date": Column("date", "o.shipped_date", "发货日期，还没发货时为空"),
            "tracking_number": Column("str", "o.tracking_number", "物流单号，未发货时为空"),
            "total_amount": Column("number", "o.total_amount", "订单总金额"),
            "cancelled_at": Column("date", "o.cancelled_at", "取消时间，没有取消时为空"),
        },
    ),
    "order_items": Table(
        alias="oi",
        private=True,
        description="订单明细",
        columns={
            "quantity": Column("int", "oi.quantity", "这一行买了几件"),
            "price_per_unit": Column("number", "oi.price_per_unit", "下单时的成交单价"),
            "line_total": Column("number", "(oi.quantity * oi.price_per_unit)",
                                 "这一行明细的金额，等于数量乘以成交单价"),
        },
    ),
    "products": Table(
        alias="p",
        private=False,
        description="商品目录",
        columns={
            "product_id": Column("str", "p.product_id", "商品编号"),
            "name": Column("str", "p.name", "商品名称", searchable=True),
            "category": Column("enum", "p.category", "商品品类",
                               enum_values=("Laptops", "Monitors", "Keyboards", "Audio", "Accessories")),
            "price": Column("number", "p.price", "当前标价，不是成交价"),
            "in_stock": Column("bool", "p.in_stock", "现在有没有货"),
        },
    ),
}


def catalog_prompt() -> str:
    """把目录渲染成一段文本，放进提示词给模型看。"""
    lines: list[str] = []
    for table_name in CHAIN:
        table = CATALOG[table_name]
        scope_label = "客户私有" if table.private else "公开"
        lines.append(f"表 {table_name}（{scope_label}）：{table.description}")
        for column_name, column in table.columns.items():
            if column.type == "enum":
                type_label = f"enum: {' / '.join(column.enum_values)}"
            else:
                type_label = column.type
            if column.searchable:
                type_label += "，可用 contains"
            lines.append(f"  - {table_name}.{column_name} ({type_label})：{column.description}")
        lines.append("")
    return "\n".join(lines)
