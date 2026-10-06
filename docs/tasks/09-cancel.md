# 任务 09：取消订单的五个节点

先读 `AGENTS.md`，再读 `docs/contracts.md` 的第 2、4、8 节，以及 `app/state.py`。本任务新建这些文件：`app/cancel.py`、`tests/test_cancel.py`。不要改别的文件。

## 背景

取消订单是图里一条固定的流程，全程没有模型，每一步都是确定性代码：

```
cancel_extract（提取订单号）→ cancel_check（校验能否取消，生成预览）→ cancel_confirm（中断，等用户确认）→ cancel_execute（带条件的 UPDATE）→ cancel_reply（模板回复）
```

- 客户身份只从 Runtime Context 取（`runtime.context.customer_id`），不从用户的话里取。
- 真正的保证在 `UPDATE` 的 `WHERE` 条件里：状态必须仍是 `Processing`、订单必须属于当前客户。前面的检查只是为了给用户看预览。
- 中断恢复时，LangGraph 会把中断所在的节点从头重新执行，所以「等确认」和「执行」是两个节点，`UPDATE` 不能写在 `cancel_confirm` 里。

节点之间怎么连由别的任务负责，本任务只写节点函数。连法是：`cancel_extract` 之后，`cancel["order_id"]` 有值去 `cancel_check`，否则去 `cancel_reply`；`cancel_check` 之后，`cancel["preview"]` 有值去 `cancel_confirm`，否则去 `cancel_reply`；`cancel_confirm` 之后，`cancel["decision"]["confirm"]` 为 `True` 去 `cancel_execute`，否则去 `cancel_reply`；`cancel_execute` 之后去 `cancel_reply`。

已有的模块（直接用，不要改）：
- `app.db.connect(account)`：`"ro"` 只读账号，`"rw"` 写账号（只能改 `orders` 的 `status`、`cancelled_at` 两列，只能往 `order_cancellations` 插入）。连接用 `DictCursor`，`autocommit=False`，用完要关闭。`app.db.to_jsonable(value)` 把 `Decimal`、`date` 转成可序列化的值。
- **不要用 `app.db.execute_sql`**，它是给 SQL Agent 用的。本文件里的查询直接用 `connect("ro")`。
- `app.state`：`State`、`make_step`、`last_user_text(messages)`。

所有节点的签名都是 `(state: State, runtime: Runtime[AppContext]) -> dict`。流程的工作数据都放在 `state["cancel"]` 这个字典里，每个节点返回 `{"cancel": {**原来的 cancel, 新增的键}, "steps": [...]}`（`cancel_extract` 是第一个节点，原来的 `cancel` 为 `None`，从空字典开始）。所有步骤的 `kind` 都是 `"code"`。

`cancel` 字典里的键：

| 键 | 含义 |
|---|---|
| `order_id` | 提取到的订单号 |
| `candidates` | 没提取到时，列出的可取消订单 |
| `preview` | 结构化预览 |
| `decision` | 用户的选择 `{"confirm": bool, "order_id": str \| None}` |
| `outcome` | 流程的结局，见下 |
| `message` | 给用户的说明文字 |

`outcome` 的取值：`login_required`、`need_order_id`、`not_found`、`not_cancellable`、`already_cancelled`、`declined`、`mismatch`、`cancelled`、`error`。

任何节点里数据库出错（`pymysql.MySQLError`）：`outcome="error"`，`message="系统暂时无法处理取消，请稍后再试。"`，步骤摘要 `f"数据库出错: {e}"`。

## 第一步：`cancel_extract_node`

1. `runtime.context.customer_id` 是 `None` → `outcome="login_required"`，`message="取消订单需要先登录。"`，步骤 `make_step("cancel/extract", "code", "匿名用户，提示登录", None)`。
2. 用正则 `ORD-\d{4}-\d{4}` 在 `last_user_text(state["messages"]).upper()` 里找订单号，去重并保持出现顺序。
3. 恰好 1 个 → `{"order_id": 订单号}`，步骤摘要 `f"订单号 {订单号}（正则提取）"`，`detail` 是 `{"order_id": 订单号}`。
4. 0 个或多于 1 个 → 不猜。用只读账号查出当前客户可以取消的订单：

```sql
SELECT order_id, order_date, total_amount FROM orders
WHERE customer_id = %s AND status = 'Processing' ORDER BY order_date DESC, order_id
```

   `candidates` 是这些行（经过 `to_jsonable`）；`outcome="need_order_id"`；`message` 按下面拼：
   - 找到多于 1 个订单号时，以 `一次只能取消一个订单。` 开头。
   - `candidates` 非空：接 `请告诉我要取消的订单号。您当前可以取消的订单：`，然后每个订单一行 `f"\n- {order_id}（下单日期 {order_date}，金额 ${total_amount:.2f}）"`。
   - `candidates` 为空：接 `您当前没有可以取消的订单（只有处理中的订单可以取消）。`
   
   步骤摘要 `f"提取到 {n} 个订单号，列出 {len(candidates)} 个可取消的订单让用户指定"`，`detail` 是 `{"found": 找到的订单号列表, "candidates": candidates}`。

## 第二步：`cancel_check_node`

用只读账号查这个订单，条件里带上当前客户：

```sql
SELECT order_id, order_date, status, total_amount FROM orders WHERE order_id = %s AND customer_id = %s
```

| 查到的情况 | `outcome` | `message` | 步骤摘要 |
|---|---|---|---|
| 没有行（不存在，或不属于当前客户） | `not_found` | `没有找到订单 <订单号>。` | `没有找到这个订单（不存在或不属于当前客户）` |
| `status` 是 `Shipped` 或 `Delivered` | `not_cancellable` | `订单 <订单号> 已经<发货 或 送达>，不能取消。如需退货请走退货流程。` | `状态 <status>，不能取消` |
| `status` 是 `Cancelled` | `already_cancelled` | `订单 <订单号> 之前已经取消过了。` | `状态 Cancelled，已经取消过` |
| `status` 是 `Processing` | 不写 `outcome` | 不写 | `属于当前客户 ✓   状态 Processing ✓   可以取消` |

`Processing` 时再查明细并生成预览：

```sql
SELECT p.name, oi.quantity, oi.price_per_unit FROM order_items oi
JOIN products p ON p.product_id = oi.product_id WHERE oi.order_id = %s ORDER BY oi.order_item_id
```

```python
preview = {
    "action": "cancel_order",
    "action_label": "取消订单",
    "order_id": ...,
    "order_date": ...,          # ISO 字符串
    "status": "Processing",
    "items": [{"name": ..., "quantity": ..., "price_per_unit": ...}, ...],   # 数值是 int / float
    "total_amount": ...,        # float
    "effect": "订单状态将变为已取消；此操作不可撤销",
}
```

预览里的所有内容都来自数据库，没有模型生成的文字。四种情况的步骤名都是 `cancel/check`，`detail` 是 `{"order_id": 订单号, "status": 查到的状态（没有行时为 None）, "preview": 预览（没有时为 None）}`。

## 第三步：`cancel_confirm_node`

```python
from langgraph.types import interrupt

def cancel_confirm_node(state, runtime):
    cancel = state["cancel"]
    answer = interrupt(cancel["preview"])
    ...
```

- `interrupt(预览)` 会让图暂停并把预览交给调用方；恢复时它返回调用方给的值。
- 只有当 `answer` 是字典且 `answer.get("confirm") is True` 时才算确认。其他任何值（`False`、字符串「好吧」、`None`……）都算不确认。
- `decision = {"confirm": 是否确认, "order_id": answer.get("order_id") if isinstance(answer, dict) else None}`。
- 确认：返回 `{"cancel": {**cancel, "decision": decision}, "steps": [make_step("cancel/confirm", "code", "用户: 确认", {"decision": decision, "preview": cancel["preview"]})]}`。
- 不确认：再加上 `outcome="declined"`、`message=f"好的，订单 {订单号} 没有取消。"`，步骤摘要 `用户: 不取消`。
- 这个节点里不访问数据库。

## 第四步：`cancel_execute_node`

1. 核对确认的和预览的是同一个订单：`cancel["decision"]["order_id"] != cancel["preview"]["order_id"]` → `outcome="mismatch"`，`message="确认的订单和预览的不一致，没有执行取消。"`，步骤摘要 `确认的订单号和预览不一致，拒绝执行`。不访问数据库。
2. 取消时间：`datetime.combine(date.fromisoformat(runtime.context.today), datetime.now().time()).replace(microsecond=0)`。
3. 用写账号在一个事务里执行：

```sql
UPDATE orders SET status = 'Cancelled', cancelled_at = %s
WHERE order_id = %s AND customer_id = %s AND status = 'Processing'
```

   参数里的客户 ID 是 `runtime.context.customer_id`。
   - 影响 1 行：同一个事务里插入取消记录，然后提交。

```sql
INSERT INTO order_cancellations (order_id, customer_id, session_id, trace_id, cancelled_at) VALUES (%s, %s, %s, %s, %s)
```

     `session_id` 是 `runtime.context.session_id`；`trace_id` 用下面的函数取。`outcome="cancelled"`，`message=f"订单 {订单号} 已取消。"`，步骤摘要 `影响 1 行，已写入取消记录，事务提交`。
   - 影响 0 行：回滚，不插入。说明订单的状态在确认期间变了。用只读账号重新查一次状态（和第二步同一条查询），按结果设置：
     - `Cancelled` → `outcome="already_cancelled"`，`message=f"订单 {订单号} 已经是取消状态，没有重复取消。"`
     - `Shipped` 或 `Delivered` → `outcome="not_cancellable"`，`message=f"订单 {订单号} 的状态已经变为 {status}，不能取消了。如需退货请走退货流程。"`
     - 没有行 → `outcome="not_found"`，`message=f"没有找到订单 {订单号}。"`
     
     步骤摘要 `f"影响 0 行（当前状态 {status}），没有取消"`。
4. 步骤名 `cancel/execute`，`detail` 是 `{"sql": 上面那条 UPDATE 的文本, "params": [参数，取消时间转成字符串], "affected_rows": 影响的行数, "outcome": outcome}`（`mismatch` 时 `detail` 是 `{"outcome": "mismatch"}`）。

取 Trace 编号的函数：

```python
from langsmith.run_helpers import get_current_run_tree

def current_trace_id() -> str:
    run = get_current_run_tree()
    return str(run.trace_id) if run is not None else ""
```

## 第五步：`cancel_reply_node`

- 回复文本是 `cancel["message"]`。如果用户这句话除了取消还问了别的（判断方法：`last_user_text` 去掉所有订单号之后，含有 `？`、`?`、`另外`、`顺便`、`还有` 之一），在后面接上 `\n这一轮只处理了取消订单，其他问题请再问一次。`
- 返回 `{"answer": 回复文本, "messages": [AIMessage(content=回复文本)], "card": {"type": "cancel_result", "outcome": outcome, "order_id": cancel.get("order_id"), "message": cancel["message"]}, "steps": [make_step("cancel/reply", "code", 回复文本, {"outcome": outcome})]}`。
- 回复是模板拼出来的，不调用模型。

## 第六步：`tests/test_cancel.py`

这些测试连真实的数据库。用到的数据：

| 订单 | 客户 | 状态 | 说明 |
|---|---|---|---|
| `ORD-2025-0031` | `CUST-002` | `Processing` | 1 件 `TECH-LAP-003`，金额 1399.00，下单日期 2025-10-17 |
| `ORD-2025-0038` | `CUST-002` | `Processing` | 金额 1999.00，下单日期 2025-10-16 |
| `ORD-2025-0009` | `CUST-002` | `Delivered` | |
| `ORD-2025-0028` | `CUST-019` | `Cancelled` | |

写一个自动使用的 fixture（`autouse=True`），在每个测试**之后**用 `connect("root")` 把数据复原并提交：

```sql
UPDATE orders SET status = 'Processing', cancelled_at = NULL WHERE order_id IN ('ORD-2025-0031', 'ORD-2025-0038');
DELETE FROM order_cancellations WHERE order_id IN ('ORD-2025-0031', 'ORD-2025-0038');
```

`runtime` 用 `types.SimpleNamespace(context=AppContext(customer_id="CUST-002", session_id="s-test"))`。直接调用节点函数。`cancel_confirm_node` 里的 `interrupt` 用 `monkeypatch.setattr("app.cancel.interrupt", lambda preview: 预设的回答)` 替换。

每条一个测试函数：

1. 提取：消息 `帮我取消订单 ord-2025-0031`（小写）→ `cancel["order_id"] == "ORD-2025-0031"`，步骤名 `cancel/extract`。
2. 没有订单号：消息 `我想取消订单` → `outcome == "need_order_id"`；`candidates` 里的订单号依次是 `ORD-2025-0031`、`ORD-2025-0038`；`message` 里包含 `- ORD-2025-0031（下单日期 2025-10-17，金额 $1399.00）`；`cancel` 里没有 `order_id` 这个键。
3. 多个订单号：消息 `取消 ORD-2025-0031 和 ORD-2025-0038` → `outcome == "need_order_id"`，`message` 以 `一次只能取消一个订单。` 开头。
4. 匿名用户：`customer_id=None` → `outcome == "login_required"`。
5. 没有可取消的订单：`customer_id="CUST-001"`、消息 `我想取消订单` → `candidates == []`，`message` 里包含 `您当前没有可以取消的订单`。
6. 别人的订单查不到：`customer_id="CUST-001"`、`cancel={"order_id": "ORD-2025-0031"}` → `cancel_check_node` 的 `outcome == "not_found"`；对一个不存在的订单号 `ORD-2099-9999` 得到的 `message`，除订单号外和它完全相同（把两条 `message` 里的订单号都替换成 `X` 后相等）。
7. 已送达：`cancel={"order_id": "ORD-2025-0009"}` → `outcome == "not_cancellable"`，`message` 里包含 `已经送达`，`cancel` 里没有 `preview`。
8. 已取消：`customer_id="CUST-019"`、`cancel={"order_id": "ORD-2025-0028"}` → `outcome == "already_cancelled"`。
9. 可以取消：`cancel={"order_id": "ORD-2025-0031"}` → 没有 `outcome`；`preview["order_id"] == "ORD-2025-0031"`、`preview["status"] == "Processing"`、`preview["total_amount"] == 1399.0`、`preview["order_date"] == "2025-10-17"`、`len(preview["items"]) == 1`、`preview["items"][0]["quantity"] == 1`、`preview["effect"] == "订单状态将变为已取消；此操作不可撤销"`；整个返回值的 `steps[0]["detail"]` 能被 `json.dumps` 序列化。
10. 确认：`interrupt` 返回 `{"confirm": True, "order_id": "ORD-2025-0031"}` → `decision == {"confirm": True, "order_id": "ORD-2025-0031"}`，`cancel` 里没有 `outcome`。
11. 不确认的三种回答：`interrupt` 分别返回 `{"confirm": False, "order_id": "ORD-2025-0031"}`、字符串 `"好吧"`、`{"confirm": "yes"}` → 三种情况都是 `decision["confirm"] is False`、`outcome == "declined"`。
12. 执行成功：先 `cancel_check_node` 得到预览，`decision={"confirm": True, "order_id": "ORD-2025-0031"}`，调用 `cancel_execute_node` → `outcome == "cancelled"`；用 `connect("root")` 查到订单的 `status` 是 `Cancelled`、`cancelled_at` 不是 `NULL`、`total_amount` 仍是 1399.00；`order_items` 里这个订单的明细仍是 1 行；`order_cancellations` 里这个订单恰好 1 行，`customer_id` 是 `CUST-002`、`session_id` 是 `s-test`。
13. 重复执行：同样的 `state` 连续调用两次 `cancel_execute_node` → 第二次 `outcome == "already_cancelled"`；`order_cancellations` 里仍然恰好 1 行。
14. 确认期间状态变了：得到预览之后，用 `connect("root")` 把订单改成 `Shipped` 并提交，再调用 `cancel_execute_node` → `outcome == "not_cancellable"`，`message` 里包含 `Shipped`；`order_cancellations` 里没有这个订单的行；订单的 `status` 仍是 `Shipped`。
15. 确认的订单和预览不一致：预览是 `ORD-2025-0031`，`decision={"confirm": True, "order_id": "ORD-2025-0038"}` → `outcome == "mismatch"`；两个订单的 `status` 都仍是 `Processing`。
16. 别的客户执行不了：预览和 `decision` 都是 `ORD-2025-0031`，但 `runtime` 的 `customer_id` 是 `CUST-001` → `outcome == "not_found"`；订单的 `status` 仍是 `Processing`。
17. 回复：`cancel={"order_id": "ORD-2025-0031", "outcome": "cancelled", "message": "订单 ORD-2025-0031 已取消。"}`、消息 `帮我取消订单 ORD-2025-0031` → `answer == "订单 ORD-2025-0031 已取消。"`；`messages` 是一条内容相同的 `AIMessage`；`card == {"type": "cancel_result", "outcome": "cancelled", "order_id": "ORD-2025-0031", "message": "订单 ORD-2025-0031 已取消。"}`。
18. 回复里的提示：消息是 `帮我取消订单 ORD-2025-0031，另外退货政策是什么？` 时，`answer` 以 `订单 ORD-2025-0031 已取消。` 开头，并以 `这一轮只处理了取消订单，其他问题请再问一次。` 结尾。
19. 没有模型：`app/cancel.py` 的源码文本里不含 `get_model`、`app.models`、`execute_sql`。

```sh
uv run pytest tests/test_cancel.py -q
```

应输出 `19 passed`。

## 最后一步：收尾检查

```sh
uv run pytest tests/test_cancel.py -q && uv run python -c "
from app.db import connect
c = connect('root'); cur = c.cursor()
cur.execute(\"SELECT COUNT(*) AS n FROM orders WHERE status = 'Processing'\"); a = cur.fetchone()['n']
cur.execute('SELECT COUNT(*) AS n FROM order_cancellations'); b = cur.fetchone()['n']
c.close(); print(a, b)"
```

通过的标准：pytest 输出 `19 passed`；第二条命令输出 `17 0`（测试没有在数据库里留下改动）。全部通过后最后一行输出 `DONE`。
