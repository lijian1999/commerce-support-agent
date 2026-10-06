"""把 data/structured 下的 4 个 JSON 导入 MySQL，建表、建账号并授权。可重复运行。"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402

# 删表顺序：先子表后父表，配合 FOREIGN_KEY_CHECKS = 0
DROP_ORDER = ["handoff_tickets", "order_cancellations", "order_items", "orders", "products", "customers"]

DDL = {
    "customers": """
CREATE TABLE customers (
  customer_id VARCHAR(16) PRIMARY KEY,
  email VARCHAR(255) NOT NULL UNIQUE,
  name VARCHAR(100) NOT NULL,
  phone VARCHAR(20),
  city VARCHAR(100) NOT NULL,
  state CHAR(2) NOT NULL,
  segment VARCHAR(20) NOT NULL,
  CONSTRAINT chk_customers_segment CHECK (segment IN ('Consumer', 'Corporate', 'Home Office'))
) ENGINE=InnoDB
""",
    "products": """
CREATE TABLE products (
  product_id VARCHAR(16) PRIMARY KEY,
  name VARCHAR(255) NOT NULL,
  category VARCHAR(20) NOT NULL,
  price DECIMAL(10,2) NOT NULL,
  in_stock TINYINT NOT NULL,
  CONSTRAINT chk_products_category CHECK (category IN ('Laptops', 'Monitors', 'Keyboards', 'Audio', 'Accessories')),
  CONSTRAINT chk_products_price CHECK (price > 0),
  CONSTRAINT chk_products_in_stock CHECK (in_stock IN (0, 1)),
  INDEX idx_products_category (category)
) ENGINE=InnoDB
""",
    "orders": """
CREATE TABLE orders (
  order_id VARCHAR(16) PRIMARY KEY,
  customer_id VARCHAR(16) NOT NULL,
  order_date DATE NOT NULL,
  status VARCHAR(20) NOT NULL,
  shipped_date DATE NULL,
  tracking_number VARCHAR(32) NULL,
  total_amount DECIMAL(10,2) NOT NULL,
  cancelled_at DATETIME NULL,
  CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers (customer_id),
  CONSTRAINT chk_orders_status CHECK (status IN ('Processing', 'Shipped', 'Delivered', 'Cancelled')),
  CONSTRAINT chk_orders_total CHECK (total_amount >= 0),
  INDEX idx_orders_customer (customer_id),
  INDEX idx_orders_date (order_date),
  INDEX idx_orders_status (status)
) ENGINE=InnoDB
""",
    "order_items": """
CREATE TABLE order_items (
  order_item_id INT AUTO_INCREMENT PRIMARY KEY,
  order_id VARCHAR(16) NOT NULL,
  product_id VARCHAR(16) NOT NULL,
  quantity INT NOT NULL,
  price_per_unit DECIMAL(10,2) NOT NULL,
  CONSTRAINT fk_items_order FOREIGN KEY (order_id) REFERENCES orders (order_id),
  CONSTRAINT fk_items_product FOREIGN KEY (product_id) REFERENCES products (product_id),
  CONSTRAINT chk_items_quantity CHECK (quantity > 0),
  CONSTRAINT chk_items_price CHECK (price_per_unit > 0),
  INDEX idx_order_items_order (order_id),
  INDEX idx_order_items_product (product_id)
) ENGINE=InnoDB
""",
    "order_cancellations": """
CREATE TABLE order_cancellations (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  order_id VARCHAR(16) NOT NULL UNIQUE,
  customer_id VARCHAR(16) NOT NULL,
  session_id VARCHAR(64) NOT NULL DEFAULT '',
  trace_id VARCHAR(64) NOT NULL DEFAULT '',
  cancelled_at DATETIME NOT NULL
) ENGINE=InnoDB
""",
    "handoff_tickets": """
CREATE TABLE handoff_tickets (
  ticket_id VARCHAR(32) PRIMARY KEY,
  customer_id VARCHAR(16) NULL,
  reason VARCHAR(32) NOT NULL,
  summary TEXT NOT NULL,
  order_ids VARCHAR(255) NOT NULL DEFAULT '',
  session_id VARCHAR(64) NOT NULL DEFAULT '',
  trace_id VARCHAR(64) NOT NULL DEFAULT '',
  created_at DATETIME NOT NULL
) ENGINE=InnoDB
""",
}

# 导入顺序有依赖：customers、products 先建，orders 引用它们
IMPORT_ORDER = ["customers", "products", "orders", "order_items"]
COUNT_ORDER = ["customers", "products", "orders", "order_items", "order_cancellations", "handoff_tickets"]


def _load_rows(table: str) -> list[dict[str, Any]]:
    path = config.STRUCTURED_DIR / f"{table}.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError(f"{path} 应该是对象数组")
    return rows


def _columns(rows: list[dict[str, Any]]) -> list[str]:
    """列名取自 JSON 的键，保持第一条记录的顺序，后面的新键追加进去。"""
    cols: list[str] = []
    for row in rows:
        for key in row:
            if key not in cols:
                cols.append(key)
    return cols


def drop_tables(cur) -> None:
    cur.execute("SET FOREIGN_KEY_CHECKS = 0")
    for table in DROP_ORDER:
        cur.execute(f"DROP TABLE IF EXISTS `{table}`")
    cur.execute("SET FOREIGN_KEY_CHECKS = 1")


def create_tables(cur) -> None:
    for table in ["customers", "products", "orders", "order_items", "order_cancellations", "handoff_tickets"]:
        cur.execute(DDL[table])


def import_data(cur) -> None:
    for table in IMPORT_ORDER:
        rows = _load_rows(table)
        if not rows:
            continue
        cols = _columns(rows)
        col_list = ", ".join(f"`{c}`" for c in cols)
        placeholder = ", ".join(["%s"] * len(cols))
        sql = f"INSERT INTO `{table}` ({col_list}) VALUES ({placeholder})"
        cur.executemany(sql, [[row.get(c) for c in cols] for row in rows])


def setup_users(cur) -> None:
    db = config.MYSQL_DATABASE
    ro = config.MYSQL_RO_USER
    rw = config.MYSQL_RW_USER
    ro_pw = config.MYSQL_RO_PASSWORD
    rw_pw = config.MYSQL_RW_PASSWORD
    cur.execute("CREATE USER IF NOT EXISTS %s@'%%' IDENTIFIED BY %s", (ro, ro_pw))
    cur.execute("ALTER USER %s@'%%' IDENTIFIED BY %s", (ro, ro_pw))
    cur.execute("CREATE USER IF NOT EXISTS %s@'%%' IDENTIFIED BY %s", (rw, rw_pw))
    cur.execute("ALTER USER %s@'%%' IDENTIFIED BY %s", (rw, rw_pw))
    cur.execute(f"REVOKE ALL PRIVILEGES, GRANT OPTION FROM `{ro}`@'%', `{rw}`@'%'")
    for table in ["customers", "products", "orders", "order_items"]:
        cur.execute(f"GRANT SELECT ON `{db}`.`{table}` TO `{ro}`@'%'")
    cur.execute(f"GRANT SELECT ON `{db}`.`orders` TO `{rw}`@'%'")
    cur.execute(f"GRANT UPDATE (status, cancelled_at) ON `{db}`.`orders` TO `{rw}`@'%'")
    cur.execute(f"GRANT INSERT ON `{db}`.`order_cancellations` TO `{rw}`@'%'")
    cur.execute(f"GRANT INSERT ON `{db}`.`handoff_tickets` TO `{rw}`@'%'")
    cur.execute("FLUSH PRIVILEGES")


def print_counts(cur) -> None:
    for table in COUNT_ORDER:
        cur.execute(f"SELECT COUNT(*) AS n FROM `{table}`")
        print(f"{table}: {cur.fetchone()['n']}")


def main() -> None:
    import app.db  # 延迟导入，sys.path 已经生效

    conn = app.db.connect("root")
    try:
        with conn.cursor() as cur:
            drop_tables(cur)
            create_tables(cur)
            import_data(cur)
            setup_users(cur)
        conn.commit()
        with conn.cursor() as cur:
            print_counts(cur)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
