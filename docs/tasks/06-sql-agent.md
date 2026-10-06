# 任务 06：SQL Agent 子图（生成查询计划 → 编译 → 执行）

先读 `AGENTS.md`，再读 `docs/contracts.md` 的第 2、3、4、5 节，以及 `app/state.py`。本任务新建这些文件：`app/sql_agent/graph.py`、`tests/test_sql_agent.py`。不要改别的文件。

## 背景

SQL Agent 是一个三节点的 LangGraph 子图，每个节点是一个可打印的步骤：

```
sql_plan（模型：生成查询计划）→ compile（代码：编译成参数化 SQL）→ execute（代码：执行）
```

- 计划被编译器拒绝且属于模型可以改对的错误时，带着错误信息回到 `sql_plan` 让模型重新生成**一次**；仍不通过则任务失败。不合法的计划永远到不了数据库。
- 一份查询计划只执行一次 SQL，没有循环。
- 主图通过 `sql_agent_node` 调用这个子图。

已有的模块（直接用，不要改）：
- `app.models.get_model(role, schema, fault)` 返回的对象有 `.invoke(messages) -> ModelResult(value, notes)`，失败时抛 `app.models.ModelError`（有 `.notes`）。
- `app.sql_agent.plan.QueryPlan`；`app.sql_agent.catalog.catalog_prompt()`；`app.sql_agent.compiler.compile_plan(plan, customer_id, today)`，它抛 `PlanRejected`（`.kind` 是 `"fixable"` / `"login_required"` / `"unanswerable"`，`.message` 是说明）或 `CompilerBug`。
- `app.db.execute_sql(sql, params)` 返回 `{"columns", "rows", "row_count"}`，数据库出错时抛 `pymysql.MySQLError`。
- `app.state` 里的 `Task`、`TaskResult`、`TaskPayload`、`Step`、`make_step`、`make_task_result`、`reset_or_add`。

## 第一步：子图的状态和提示词

在 `app/sql_agent/graph.py` 里：

```python
class SqlState(TypedDict, total=False):
    task: Task
    question: str
    history: str
    upstream: dict[str, TaskResult]
    query_plan: dict | None      # QueryPlan.model_dump()
    plan_error: str | None       # 编译器上一次的拒绝信息，交给模型修复用
    attempts: int                # 已经生成过几次查询计划
    compiled: dict | None        # {"sql": str, "params": list}
    result: TaskResult | None    # 任务的最终结果；一旦有值，子图结束
    steps: Annotated[list[Step], reset_or_add]

MAX_PLAN_ATTEMPTS = 2
```

系统提示词用下面这段原文，放在常量 `SQL_PLAN_SYSTEM` 里，`{today}` 和 `{catalog}` 两处在调用时填入（`catalog` 是 `catalog_prompt()` 的返回值）：

```
你是电商客服系统里的查询规划器。你的任务是把用户的问题翻译成一份结构化的查询计划，由程序编译成 SQL 执行。你不写 SQL。

今天是 {today}。

## 可以引用的表和列
{catalog}

## 规则
1. 列一律写成「表.列」，只能用上面列出的列。
2. 不需要、也不能指定「查谁的数据」。客户私有的表会由程序自动限定为当前登录的客户，直接引用即可。「我的订单」就是引用 orders 表。
3. 不写 JOIN。引用了哪些表，程序会自动把它们连起来。
4. 只引用 products 表，查到的是商品目录里的所有商品。要查「我买过的商品」，必须同时引用 orders 或 order_items 的列，例如在 select 里加上 orders.order_date。
5. 查明细用 select；做统计用 metrics（sum / count / count_distinct / avg / min / max），按什么分组写在 group_by 里。用了 metrics 时 select 留空。
6. metrics 的 alias 只能用小写字母、数字、下划线，并以字母开头。count 必须指定一列，例如 orders.order_id。
7. filters 之间是「并且」的关系，没有「或者」；同一列的多个取值用 in。values 里一律写字符串。
8. 日期写成 YYYY-MM-DD。「2024 年」写成 between 2024-01-01 和 2024-12-31；「最近 N 天」用 last_n_days，values 写 N；「最近一个月」按 30 天算。
9. 「最近一个」「最新的」用 order_by 降序加 limit 1，「最贵」「最便宜」同理。
10. 一笔订单的总金额是 orders.total_amount；在某类商品上花了多少钱用 sum(order_items.line_total)。
11. 枚举列的取值必须和上面列出的完全一致。用户用中文说的要对应过去，例如「键盘」对应 Keyboards，「已发货」对应 Shipped。
12. limit 写需要的行数，不确定时写 20。
13. 如果问题没法用这份结构表达（需要子查询、需要「或者」、需要表里没有的数据），把 answerable 设为 false，在 unanswerable_reason 里写明原因，其余列表留空，limit 写 0。能表达时 answerable 为 true，unanswerable_reason 留空字符串。
```

用户消息由函数 `build_user_message(state: SqlState) -> str` 拼出，各部分之间空一行，没有内容的部分整段省略：

```
最近的对话：
<history>

用户的问题：<question>

本次任务：<task.goal>

前置任务的结果：
<json.dumps(upstream, ensure_ascii=False)>

上一次的查询计划被拒绝了：<plan_error>
上一次的查询计划：<json.dumps(query_plan, ensure_ascii=False)>
请修正后重新给出完整的查询计划。
```

- 「最近的对话」：`history` 为空串时省略。
- 「本次任务」：`task.goal` 和 `question` 相同时省略。
- 「前置任务的结果」：`upstream` 为空时省略。
- 最后三行：`plan_error` 为空时省略。

## 第二步：三个节点

子图节点的签名是 `(state: SqlState, runtime: Runtime[AppContext]) -> dict`。下面的 `tid` 指 `state["task"]["id"]`。

**`sql_plan_node`**
- 调用 `get_model("sql_planner", QueryPlan, fault=runtime.context.fault).invoke([("system", 系统提示词), ("user", build_user_message(state))])`。
- 成功：返回 `{"query_plan": result.value.model_dump(), "attempts": 原值 + 1, "steps": [...]}`。步骤：`make_step(f"{tid}/sql_plan", "model", 摘要, 完整的计划字典, result.notes)`。摘要是把计划字典里除 `reasoning`、`unanswerable_reason` 之外、值不为空列表的字段用 `json.dumps(..., ensure_ascii=False)` 输出成一行；第二次生成时在前面加上 `（修复）`。
- `ModelError`：返回 `{"result": make_task_result(task, "failed", error_kind="model_error", error="生成查询计划时模型不可用"), "steps": [make_step(f"{tid}/sql_plan", "model", "模型调用失败", None, e.notes)]}`。

**`compile_node`**
- `plan = QueryPlan.model_validate(state["query_plan"])`，然后 `compile_plan(plan, runtime.context.customer_id, runtime.context.today)`。
- 成功：返回 `{"compiled": {"sql": ..., "params": ...}, "plan_error": None, "steps": [...]}`。步骤摘要是 `f"{sql}\nparams = {params!r}"`（多行）；`detail` 是 `{"sql", "params", "customer_scope": 说明}`，其中说明是：SQL 里含有 `customer_id = %s` 时为 `"客户范围由编译器加入，值来自 Runtime"`，否则为 `"只涉及公开表，不加客户范围"`。
- `PlanRejected`，`kind == "fixable"`：
  - `state["attempts"] < MAX_PLAN_ATTEMPTS` → 返回 `{"compiled": None, "plan_error": e.message, "steps": [...]}`，步骤摘要 `f"计划被拒绝，交回模型修复: {e.message}"`。
  - 否则 → 返回 `{"result": make_task_result(task, "failed", error_kind="invalid_plan", error="没能生成合法的查询计划"), "steps": [...]}`，步骤摘要 `f"计划第 {attempts} 次仍被拒绝，任务失败: {e.message}"`。
- `PlanRejected`，`kind == "login_required"`：返回 `{"result": make_task_result(task, "failed", error_kind="login_required", error=e.message), "steps": [...]}`，步骤摘要 `f"拒绝: {e.message}"`。不重试。
- `PlanRejected`，`kind == "unanswerable"`：同上，但 `error_kind="unanswerable"`，`error=f"这个问题超出了可查询的范围: {e.message}"`，步骤摘要 `f"模型声明无法用查询计划表达: {e.message}"`。
- `CompilerBug`：`error_kind="invalid_plan"`，`error="查询编译出错"`，步骤摘要 `f"编译器出口断言失败: {e}"`。
- 这个节点的步骤 `kind` 都是 `"code"`，`detail` 在失败时是 `{"error": 说明, "kind": e.kind}`（`CompilerBug` 时 `kind` 写 `"bug"`）。

**`execute_node`**
- `data = execute_sql(compiled["sql"], compiled["params"])`。
- 成功：返回 `{"result": make_task_result(task, "ok", data=data), "steps": [make_step(f"{tid}/execute", "code", f"{data['row_count']} 行", data)]}`。0 行也是成功。
- `pymysql.MySQLError`：返回 `{"result": make_task_result(task, "failed", error_kind="db_error", error="数据库查询失败"), "steps": [make_step(f"{tid}/execute", "code", f"执行失败: {e}", {"error": str(e)})]}`。

## 第三步：组图和 `sql_agent_node`

```python
def build_sql_graph() -> CompiledStateGraph:
    g = StateGraph(SqlState, context_schema=AppContext)
    g.add_node("sql_plan", sql_plan_node)
    g.add_node("compile", compile_node)
    g.add_node("execute", execute_node)
    g.add_edge(START, "sql_plan")
    g.add_conditional_edges("sql_plan", after_sql_plan, ["compile", END])
    g.add_conditional_edges("compile", after_compile, ["execute", "sql_plan", END])
    g.add_edge("execute", END)
    return g.compile()
```

- `after_sql_plan(state)`：`state.get("result")` 有值 → `END`，否则 `"compile"`。
- `after_compile(state)`：`state.get("result")` 有值 → `END`；`state.get("compiled")` 有值 → `"execute"`；否则 `"sql_plan"`。

```python
_SQL_GRAPH = build_sql_graph()

def sql_agent_node(payload: TaskPayload, runtime: Runtime[AppContext]) -> dict:
    out = _SQL_GRAPH.invoke(
        {"task": payload["task"], "question": payload["question"], "history": payload["history"],
         "upstream": payload["upstream"], "query_plan": None, "plan_error": None, "attempts": 0,
         "compiled": None, "result": None, "steps": None},
        context=runtime.context,
    )
    return {"task_results": {payload["task"]["id"]: out["result"]}}
```

`sql_agent_node` 就按上面这样写：它只返回 `task_results`，不返回 `steps`。

```sh
uv run python -c "
from app.sql_agent.graph import build_sql_graph
g = build_sql_graph().get_graph()
print(sorted(n for n in g.nodes if not n.startswith('__')))"
```

应输出 `['compile', 'execute', 'sql_plan']`。

## 第四步：`tests/test_sql_agent.py`

测试不调用真实的模型和数据库，用 `monkeypatch` 替换 `app.sql_agent.graph` 模块里的两个名字：
- `get_model` → 一个假工厂：返回的对象每次 `.invoke(messages)` 依次返回预先给定的结果（`ModelResult(value=QueryPlan 实例, notes=[])`，或者抛出给定的异常），并把每次收到的 `messages` 记下来。
- `execute_sql` → 一个假函数：记录每次收到的 `(sql, params)`，返回 `{"columns": ["orders.order_id"], "rows": [{"orders.order_id": "ORD-1"}], "row_count": 1}`，或者抛出给定的异常。

用 `build_sql_graph().invoke(初始状态, context=AppContext(customer_id="CUST-001"))` 运行，初始状态和 `sql_agent_node` 里的一样。任务用 `{"id": "t1", "agent": "sql", "goal": "查最近一个订单", "depends_on": []}`。

准备两份计划：合法的（`select=["orders.order_id"]`，其余为空，`answerable=True`，`limit=1`）和不合法的（`select=["orders.customer_id"]`）。

每条一个测试函数：

1. 正常路径：`result["status"] == "ok"`；步骤名依次是 `["t1/sql_plan", "t1/compile", "t1/execute"]`，`kind` 依次是 `["model", "code", "code"]`；`execute_sql` 恰好被调用 1 次，收到的 `params[0] == "CUST-001"`；模型恰好被调用 1 次。
2. 修复一次后成功：模型先返回不合法的计划、再返回合法的。`result["status"] == "ok"`；步骤名依次是 `["t1/sql_plan", "t1/compile", "t1/sql_plan", "t1/compile", "t1/execute"]`；模型被调用 2 次，第二次收到的用户消息里包含 `上一次的查询计划被拒绝了` 和 `orders.customer_id`；`execute_sql` 恰好被调用 1 次。
3. 两次都不合法：`result["status"] == "failed"`、`error_kind == "invalid_plan"`；模型恰好被调用 2 次；`execute_sql` 没有被调用。
4. 匿名用户（`AppContext(customer_id=None)`）加合法的计划：`error_kind == "login_required"`；模型恰好被调用 1 次；`execute_sql` 没有被调用。
5. 模型声明答不了（`answerable=False`，`unanswerable_reason="需要子查询"`）：`error_kind == "unanswerable"`，`error` 里包含 `需要子查询`；模型 1 次；`execute_sql` 没有被调用。
6. 模型不可用（假对象抛 `ModelUnavailable("x", ["主模型第 1 次调用失败"])`）：`error_kind == "model_error"`；只有一个步骤 `t1/sql_plan`，它的 `notes == ["主模型第 1 次调用失败"]`。
7. 数据库出错（假 `execute_sql` 抛 `pymysql.err.OperationalError(1205, "timeout")`）：`error_kind == "db_error"`，`status == "failed"`。
8. 查到 0 行（假 `execute_sql` 返回 `{"columns": [], "rows": [], "row_count": 0}`）：`status == "ok"`，最后一个步骤的摘要是 `0 行`。
9. 模型的重试记录进入步骤：模型返回 `ModelResult(value=合法计划, notes=["主模型第 1 次调用失败", "主模型调用成功"])` 时，`t1/sql_plan` 步骤的 `notes` 等于这个列表。
10. `sql_agent_node`：用 `types.SimpleNamespace(context=AppContext(customer_id="CUST-001"))` 当作 `runtime`，返回值的键恰好是 `{"task_results"}`，`task_results["t1"]["status"] == "ok"`。
11. `build_user_message`：`history` 为空、`goal` 等于 `question`、`upstream` 为空、`plan_error` 为空时，结果恰好是 `用户的问题：<question>`；四项都有内容时，结果里依次出现 `最近的对话：`、`用户的问题：`、`本次任务：`、`前置任务的结果：`、`上一次的查询计划被拒绝了：`。
12. 所有步骤的 `detail` 都能被 `json.dumps(..., ensure_ascii=False)` 序列化（在正常路径上检查）。

```sh
uv run pytest tests/test_sql_agent.py -q
```

应输出 `12 passed`。

## 最后一步：收尾检查

用真实的模型和数据库跑一次：

```sh
uv run python -c "
from types import SimpleNamespace
from app.context import AppContext
from app.sql_agent.graph import sql_agent_node
q = '我在键盘上一共花了多少钱？'
task = {'id': 't1', 'agent': 'sql', 'goal': q, 'depends_on': []}
out = sql_agent_node({'task': task, 'question': q, 'history': '', 'upstream': {}}, SimpleNamespace(context=AppContext(customer_id='CUST-001')))
r = out['task_results']['t1']
print(r['status'], r['data']['row_count'])"
```

通过的标准：这条命令输出 `ok 1`；`uv run pytest tests/test_sql_agent.py -q` 输出 `12 passed`。全部通过后最后一行输出 `DONE`。
