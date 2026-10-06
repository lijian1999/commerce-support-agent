# 任务 10：转人工节点

先读 `AGENTS.md`，再读 `docs/contracts.md` 的第 2、3、4、8 节，以及 `app/state.py`。本任务新建这些文件：`app/handoff.py`、`tests/test_handoff.py`。不要改别的文件。

## 背景

转人工是图里一个明确的终点：往 `handoff_tickets` 表写一条工单，然后回复工单号。有三种触发原因：

| `reason` | 含义 | 谁判断的 |
|---|---|---|
| `user_request` | 用户明确要求人工 | Router 把请求分到了 `handoff` |
| `out_of_scope` | 投诉、纠纷等不该由机器人处理的请求 | Router 把请求分到了 `handoff` |
| `system_failure` | 系统处理不了：计划反复不合法、任务全部失败、模型不可用 | 代码规则 |

工单里带一段对话摘要，让人工客服不用重新问一遍。摘要平时由模型生成；但如果转人工的原因就是系统故障，就不能再依赖模型，直接存最近几轮原始对话。

已有的模块（直接用，不要改）：
- `app.models.get_model(role, schema, fault)`，`.invoke(messages) -> ModelResult(value, notes)`，失败抛 `app.models.ModelError`（有 `.notes`）。
- `app.db.connect("rw")`：写账号，对 `handoff_tickets` 只有 INSERT 权限（不能 SELECT）。连接 `autocommit=False`，用完要关闭。
- `app.state`：`State`、`make_step`、`last_user_text(messages)`、`history_text(messages)`。

`handoff_tickets` 的列：`ticket_id`、`customer_id`（可空）、`reason`、`summary`、`order_ids`、`session_id`、`trace_id`、`created_at`。

## 第一步：几个纯函数

```python
REASON_LABELS = {"user_request": "用户要求人工", "out_of_scope": "超出机器人处理范围", "system_failure": "系统处理失败"}

def resolve_trigger(state: State) -> dict
def raw_transcript(state: State) -> str
def extract_order_ids(state: State) -> list[str]
def new_ticket_id(today: str) -> str
```

**`resolve_trigger(state)`** 返回 `{"reason": ..., "detail": ...}`，按顺序判断：
1. `state.get("handoff")` 是字典且有 `reason` 键 → 原样返回它的 `reason` 和 `detail`（上游节点已经写好了触发信息）。
2. `state.get("route") == "handoff"`（Router 分过来的）→ 用户这句话（`last_user_text`）匹配正则 `人工|真人|客服人员|human|agent`（不区分大小写）时 `reason` 是 `user_request`，否则是 `out_of_scope`；`detail` 是 `state.get("route_reason") or ""`。
3. 其余情况 → `reason` 是 `system_failure`。`detail`：`state.get("plan_errors")` 非空时是 `"执行计划没有通过校验: " + "；".join(plan_errors)`；否则，`task_results` 里有失败或跳过的任务时是 `"任务全部失败: "` 加上每个这样的任务的 `f"{task_id}（{error}）"`，按任务编号排序、用 `；` 连接；都没有时是 `"系统无法处理这个请求"`。

**`raw_transcript(state)`**：`history = history_text(state["messages"])`；`history` 非空时返回 `history + "\n用户: " + 用户这句话`，否则返回 `"用户: " + 用户这句话`。

**`extract_order_ids(state)`**：把 `state["messages"]` 里所有消息的 `content` 转成字符串，用正则 `ORD-\d{4}-\d{4}` 找出订单号（先把文本转成大写），去重并保持首次出现的顺序。

**`new_ticket_id(today)`**：`"TKT-" + today 去掉连字符 + "-" + uuid.uuid4().hex[:6].upper()`，例如 `TKT-20251020-3FA9C1`。

## 第二步：`handoff_node`

签名 `(state: State, runtime: Runtime[AppContext]) -> dict`。

1. `trigger = resolve_trigger(state)`。
2. 摘要：
   - `trigger["reason"] == "system_failure"` → `summary = raw_transcript(state)`，不调模型。
   - 否则调用 `get_model("answer", fault=runtime.context.fault).invoke([("system", SUMMARY_SYSTEM), ("user", raw_transcript(state))])`，`summary` 是返回的文本。抛 `ModelError` 时改用 `raw_transcript(state)`，并把 `e.notes` 记进步骤的 `notes`。
   
   `SUMMARY_SYSTEM` 原文：

```
你在为人工客服写一段交接摘要。根据下面的对话，用两三句简体中文写清楚：用户遇到了什么问题、想要什么、提到了哪些订单或商品。只写对话里有的内容，不要编造，不要给出处理建议。
```

3. 工单：

```python
ticket = {
    "ticket_id": new_ticket_id(runtime.context.today),
    "customer_id": runtime.context.customer_id,      # 可以是 None
    "reason": trigger["reason"],
    "reason_label": REASON_LABELS[trigger["reason"]],
    "detail": trigger["detail"],
    "summary": summary,
    "order_ids": extract_order_ids(state),           # 列表
    "session_id": runtime.context.session_id,
    "trace_id": current_trace_id(),
    "created_at": 创建时间的 ISO 字符串,
}
```

   - 创建时间：`datetime.combine(date.fromisoformat(runtime.context.today), datetime.now().time()).replace(microsecond=0)`。
   - `current_trace_id()`：

```python
from langsmith.run_helpers import get_current_run_tree

def current_trace_id() -> str:
    run = get_current_run_tree()
    return str(run.trace_id) if run is not None else ""
```

4. 用写账号插入并提交。`order_ids` 列存逗号连接的字符串；`detail` 和 `reason_label` 不入库。

```sql
INSERT INTO handoff_tickets (ticket_id, customer_id, reason, summary, order_ids, session_id, trace_id, created_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
```

5. 成功时返回：

```python
{
    "handoff": ticket,
    "card": {"type": "ticket", **ticket},
    "answer": 回复文本,
    "messages": [AIMessage(content=回复文本)],
    "steps": [make_step("handoff", kind, f"工单 {ticket_id}   原因: {reason_label}   摘要来源: {来源}", ticket, notes)],
}
```

   - 回复文本：`f"已为您转接人工客服，工单号 {ticket_id}。人工客服会根据已有的记录继续处理，您不需要重复说明。"`
   - `kind`：这次调用了模型（不管成功还是失败）就是 `"model"`，否则 `"code"`。
   - 「摘要来源」：模型写的摘要是 `模型`，用原始对话时是 `原始对话`。
6. 数据库出错（`pymysql.MySQLError`）：不抛异常。返回 `{"handoff": ticket, "card": None, "answer": "系统繁忙，暂时无法创建工单，请稍后再试或直接联系人工客服。", "messages": [AIMessage(...)], "steps": [make_step("handoff", kind, f"写工单失败: {e}", ticket, notes)]}`。

## 第三步：`tests/test_handoff.py`

模型用假的：`monkeypatch` 替换 `app.handoff.get_model`，假工厂记录被调用的次数和参数，返回的对象 `.invoke(messages)` 返回预设的 `ModelResult` 或抛出预设的异常。

数据库用真的。测试里创建的工单要清理：写一个自动使用的 fixture（`autouse=True`），在每个测试之后用 `connect("root")` 执行 `DELETE FROM handoff_tickets WHERE session_id = 's-test'` 并提交。`runtime` 用 `types.SimpleNamespace(context=AppContext(customer_id="CUST-001", session_id="s-test"))`。读工单用 `connect("root")`。

每条一个测试函数：

1. `resolve_trigger`：`state["handoff"]` 是 `{"reason": "system_failure", "detail": "回答模型不可用"}` 时原样返回这两个键。
2. `resolve_trigger`：`route="handoff"`、消息 `帮我转人工` → `reason == "user_request"`；消息 `我要投诉，你们发错货了` → `reason == "out_of_scope"`；`detail` 等于 `route_reason`。
3. `resolve_trigger`：`route="supervisor"`、`plan_errors=["存在循环依赖: t1 -> t2 -> t1"]` → `reason == "system_failure"`，`detail == "执行计划没有通过校验: 存在循环依赖: t1 -> t2 -> t1"`。
4. `resolve_trigger`：`route="sql"`、`task_results` 里 `t1` 失败且 `error` 是 `数据库查询失败` → `detail == "任务全部失败: t1（数据库查询失败）"`。
5. `raw_transcript`：消息是 `[HumanMessage("我的订单到哪了"), AIMessage("已发货"), HumanMessage("帮我转人工")]` → `"用户: 我的订单到哪了\n客服: 已发货\n用户: 帮我转人工"`；只有一条用户消息时 → `"用户: 帮我转人工"`。
6. `extract_order_ids`：消息里依次出现 `ord-2025-0031`、`ORD-2024-0089`、`ORD-2025-0031` → `["ORD-2025-0031", "ORD-2024-0089"]`。
7. `new_ticket_id("2025-10-20")` 匹配正则 `^TKT-20251020-[0-9A-F]{6}$`，连续两次的结果不同。
8. 用户要求人工：`route="handoff"`、消息 `订单 ORD-2025-0031 有问题，帮我转人工`、假模型返回 `用户反馈订单有问题，要求人工处理。` → 假工厂被调用 1 次，`role` 是 `answer`、`schema` 是 `None`；步骤 `kind` 是 `model`，摘要里包含 `摘要来源: 模型`；数据库里这张工单的 `reason` 是 `user_request`、`summary` 是假模型返回的文本、`order_ids` 是 `ORD-2025-0031`、`customer_id` 是 `CUST-001`、`session_id` 是 `s-test`；返回的 `card["type"] == "ticket"`、`card["reason_label"] == "用户要求人工"`；`answer` 里包含工单号；`messages` 是一条内容等于 `answer` 的 `AIMessage`。
9. 系统故障不调模型：`handoff={"reason": "system_failure", "detail": "回答模型不可用"}` → 假工厂完全没有被调用；步骤 `kind` 是 `code`，摘要里包含 `摘要来源: 原始对话`；数据库里工单的 `summary` 等于 `raw_transcript(state)`。
10. 写摘要时模型不可用：`route="handoff"`、假模型抛 `ModelUnavailable("x", ["主模型第 1 次调用失败"])` → 工单照样写入，`summary` 等于 `raw_transcript(state)`；步骤 `kind` 是 `model`，`notes == ["主模型第 1 次调用失败"]`。
11. 匿名用户：`customer_id=None`（`session_id` 仍是 `s-test`）→ 工单写入成功，数据库里 `customer_id` 是 `NULL`。
12. 数据库出错：`monkeypatch` 把 `app.handoff.connect` 换成一个抛 `pymysql.err.OperationalError(2003, "down")` 的函数 → 不抛异常，`card is None`，`answer` 以 `系统繁忙` 开头，步骤摘要以 `写工单失败` 开头。
13. 步骤的 `detail` 能被 `json.dumps(..., ensure_ascii=False)` 序列化（在第 8 条的场景上检查）。
14. 不绑定工具：`app/handoff.py` 的源码文本里不含 `bind_tools`、`execute_sql`。

```sh
uv run pytest tests/test_handoff.py -q
```

应输出 `14 passed`。

## 最后一步：收尾检查

```sh
uv run pytest tests/test_handoff.py -q && uv run python -c "
from app.db import connect
c = connect('root'); cur = c.cursor()
cur.execute('SELECT COUNT(*) AS n FROM handoff_tickets'); print(cur.fetchone()['n']); c.close()"
```

通过的标准：pytest 输出 `14 passed`；第二条命令输出 `0`（测试没有在数据库里留下工单）。全部通过后最后一行输出 `DONE`。
