# 任务 11：主图、运行入口、命令行

先读 `AGENTS.md`，再读 `docs/contracts.md` 全文，以及 `app/state.py`、`app/context.py`。本任务新建这些文件：`app/graph.py`、`app/runner.py`、`cli.py`、`tests/test_graph.py`。不要改别的文件。

## 背景

所有节点都已经写好，本任务把它们连成一张图，提供一个统一的运行入口 `run_turn`，再写一个命令行。命令行每个节点跑完就打印一行，这样能看到一次请求内部的每一步。

已有的节点（直接导入，不要改）：

| 来自 | 名字 |
|---|---|
| `app.router` | `router_node` |
| `app.supervisor.nodes` | `planner_node`、`validate_node`、`scheduler_node` |
| `app.sql_agent.graph` | `sql_agent_node` |
| `app.rag.retrieval` | `rag_agent_node` |
| `app.answer` | `answer_node` |
| `app.cancel` | `cancel_extract_node`、`cancel_check_node`、`cancel_confirm_node`、`cancel_execute_node`、`cancel_reply_node` |
| `app.handoff` | `handoff_node` |

## 第一步：`app/graph.py`

内容就是下面这样（可以补文档字符串和注释，逻辑不要改）：

```python
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app.answer import answer_node
from app.cancel import (cancel_check_node, cancel_confirm_node, cancel_execute_node,
                        cancel_extract_node, cancel_reply_node)
from app.context import AppContext
from app.handoff import handoff_node
from app.rag.retrieval import rag_agent_node
from app.router import router_node
from app.sql_agent.graph import sql_agent_node
from app.state import State, Task, TaskPayload, history_text, last_user_text
from app.supervisor.nodes import planner_node, scheduler_node, validate_node


def build_payload(state: State, task: Task) -> TaskPayload:
    results = state.get("task_results") or {}
    return {
        "task": task,
        "question": last_user_text(state["messages"]),
        "history": history_text(state["messages"]),
        "upstream": {dep: results[dep] for dep in task["depends_on"] if dep in results},
    }


def finish_route(state: State) -> str:
    """所有任务都结束之后去哪：有成功的结果就写回答；只是需要登录也交给回答节点；否则转人工。"""
    results = list((state.get("task_results") or {}).values())
    if any(r["status"] == "ok" for r in results):
        return "answer"
    if any(r["error_kind"] == "login_required" for r in results):
        return "answer"
    return "handoff"


def after_router(state: State):
    route = state["route"]
    if route in ("sql", "rag"):
        task: Task = {"id": "t1", "agent": route, "goal": last_user_text(state["messages"]), "depends_on": []}
        return [Send(f"{route}_agent", build_payload(state, task))]
    return {"supervisor": "planner", "cancel": "cancel_extract", "handoff": "handoff", "direct": "answer"}[route]


def after_planner(state: State) -> str:
    return "handoff" if state.get("handoff") else "validate"


def after_validate(state: State) -> str:
    if state.get("batches") is not None:
        return "scheduler"
    return "handoff" if state.get("handoff") else "planner"


def after_scheduler(state: State):
    if state["current_batch"]:
        tasks = {t["id"]: t for t in state["plan"]["tasks"]}
        return [Send(f"{tasks[tid]['agent']}_agent", build_payload(state, tasks[tid])) for tid in state["current_batch"]]
    return finish_route(state)


def after_task(state: State) -> str:
    return "scheduler" if state.get("plan") else finish_route(state)


def after_answer(state: State) -> str:
    return "handoff" if state.get("handoff") else END


def after_cancel_extract(state: State) -> str:
    return "cancel_check" if state["cancel"].get("order_id") else "cancel_reply"


def after_cancel_check(state: State) -> str:
    return "cancel_confirm" if state["cancel"].get("preview") else "cancel_reply"


def after_cancel_confirm(state: State) -> str:
    return "cancel_execute" if state["cancel"]["decision"]["confirm"] else "cancel_reply"


def build_graph(checkpointer=None):
    g = StateGraph(State, context_schema=AppContext)
    for name, node in [
        ("router", router_node), ("planner", planner_node), ("validate", validate_node),
        ("scheduler", scheduler_node), ("sql_agent", sql_agent_node), ("rag_agent", rag_agent_node),
        ("answer", answer_node), ("handoff", handoff_node),
        ("cancel_extract", cancel_extract_node), ("cancel_check", cancel_check_node),
        ("cancel_confirm", cancel_confirm_node), ("cancel_execute", cancel_execute_node),
        ("cancel_reply", cancel_reply_node),
    ]:
        g.add_node(name, node)

    g.add_edge(START, "router")
    g.add_conditional_edges("router", after_router,
                            ["sql_agent", "rag_agent", "planner", "cancel_extract", "handoff", "answer"])
    g.add_conditional_edges("planner", after_planner, ["validate", "handoff"])
    g.add_conditional_edges("validate", after_validate, ["scheduler", "planner", "handoff"])
    g.add_conditional_edges("scheduler", after_scheduler, ["sql_agent", "rag_agent", "answer", "handoff"])
    g.add_conditional_edges("sql_agent", after_task, ["scheduler", "answer", "handoff"])
    g.add_conditional_edges("rag_agent", after_task, ["scheduler", "answer", "handoff"])
    g.add_conditional_edges("answer", after_answer, ["handoff", END])
    g.add_edge("handoff", END)

    g.add_conditional_edges("cancel_extract", after_cancel_extract, ["cancel_check", "cancel_reply"])
    g.add_conditional_edges("cancel_check", after_cancel_check, ["cancel_confirm", "cancel_reply"])
    g.add_conditional_edges("cancel_confirm", after_cancel_confirm, ["cancel_execute", "cancel_reply"])
    g.add_edge("cancel_execute", "cancel_reply")
    g.add_edge("cancel_reply", END)
    return g.compile(checkpointer=checkpointer)
```

```sh
uv run python -c "
from app.graph import build_graph
g = build_graph().get_graph()
print(len([n for n in g.nodes if not n.startswith('__')]), sorted(e.source for e in g.edges if e.target == 'cancel_execute'))"
```

应输出 `13 ['cancel_confirm']`。

## 第二步：`app/runner.py`

内容就是下面这样（可以补文档字符串和注释，逻辑不要改）：

```python
import subprocess
from typing import Any, Iterator

from langgraph.types import Command

from app import config
from app.context import AppContext
from app.rag.index import IndexStore
from app.state import new_turn_input


def make_context(customer_id: str | None, today: str | None = None, fault: str = "off", session_id: str = "") -> AppContext:
    """请求开始时读一次索引指针，这次请求里所有的检索都用这一个版本。"""
    pointer = IndexStore().read_pointer()
    return AppContext(
        customer_id=customer_id or None,
        today=today or config.DEFAULT_TODAY,
        fault=fault,
        index_version=pointer["current"] if pointer else None,
        session_id=session_id,
    )


def git_version() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=config.ROOT).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, cwd=config.ROOT).stdout.strip()
        return (sha or "unknown") + ("-dirty" if dirty else "")
    except OSError:
        return "unknown"


def run_metadata(ctx: AppContext) -> dict:
    """随每次运行上报到 LangSmith 的元数据。"""
    return {
        "git_version": git_version(),
        "models": {role: config.role_target(role).model for role in config.ROLES},
        "index_version": ctx.index_version,
        "eval_set_version": None,
        "today": ctx.today,
        "fault": ctx.fault,
        "session_id": ctx.session_id,
    }


def run_turn(graph, ctx: AppContext, thread_id: str, text: str | None = None, resume: Any = None) -> Iterator[tuple[str, Any]]:
    """跑一轮对话（给 text），或者从中断处恢复（给 resume）。逐个产出事件：
    ("step", Step)、("interrupt", 预览字典)、("final", 这一轮结束时的状态字典)。"""
    run_config = {"configurable": {"thread_id": thread_id}, "metadata": run_metadata(ctx), "run_name": "support_turn"}
    graph_input = Command(resume=resume) if resume is not None else new_turn_input(text)
    for _namespace, chunk in graph.stream(graph_input, run_config, context=ctx, stream_mode="updates", subgraphs=True):
        for node, update in chunk.items():
            if node == "__interrupt__":
                yield ("interrupt", update[0].value)
            elif isinstance(update, dict):
                for step in update.get("steps") or []:
                    yield ("step", step)
    yield ("final", graph.get_state(run_config).values)
```

## 第三步：`cli.py`

用法：

```
uv run python cli.py [--customer CUST-001] [--today 2025-10-20] [--fault off|primary_down|all_down] [--yes | --no] [--verbose] [问题]
```

- `--customer`：当前登录的客户，代表上游网关已经验证过的身份；不给就是匿名用户。
- `--today`：Runtime 的「今天」，默认 `config.DEFAULT_TODAY`。
- `--fault`：模拟故障，默认 `off`。
- `--yes` / `--no`：遇到取消订单的确认时自动回答，不再提问（两个互斥）。
- `--verbose`：每个步骤下面再打印 `detail` 的 JSON（`json.dumps(detail, ensure_ascii=False, indent=2)`，每行缩进 18 个空格）。
- 给了「问题」就跑一轮后退出；没给就进入交互模式：循环用 `input("你> ")` 读一行，空行跳过，输入 `exit` 或 `quit` 或遇到 EOF 时退出。交互模式里所有轮次用同一个会话，所以能接上前面的对话。

程序结构：

```python
COLUMN = 18

def format_step(step: Step, color: bool = False, verbose: bool = False) -> str
def format_preview(preview: dict) -> str
def run_and_print(graph, ctx, thread_id, text=None, resume=None, auto_confirm: bool | None = None) -> dict
def main(argv: list[str] | None = None) -> int
```

**`format_step`**：
- 第一行是 `f"[{name}]".ljust(COLUMN)` 加上摘要的第一行。`f"[{name}]"` 的长度大于等于 `COLUMN` 时，在它后面补两个空格再接摘要。
- 摘要有多行时，后面的每一行前面加 `COLUMN` 个空格。
- `notes` 里的每一条单独占一行：`COLUMN` 个空格 + `"↻ "` + 这条记录。
- `verbose` 为 `True` 且 `detail` 不是 `None` 时，再接 `detail` 的 JSON，每行前面加 `COLUMN` 个空格。
- `color` 为 `True` 时，只给第一行的 `[name]` 这部分上色：`notes` 非空用黄色 `\033[33m`，否则 `kind == "model"` 用紫色 `\033[35m`，`kind == "code"` 用灰色 `\033[90m`；后面跟 `\033[0m` 复位。上色不改变对齐（填充的空格数按不含颜色码的长度算）。
- 各行用 `\n` 连接，结尾没有换行。

**`format_preview`**：把取消订单的预览打印成三行或更多行，每行前面加 `COLUMN` 个空格：

```
操作: 取消订单   订单: ORD-2025-0031   下单: 2025-10-17   状态: Processing
明细: Dell XPS 15 × 1   $1399.00
合计: $1399.00   执行后: 订单状态将变为已取消；此操作不可撤销
```

明细每件商品一行（`f"明细: {name} × {quantity}   ${price_per_unit:.2f}"`），合计是 `f"${total_amount:.2f}"`。

**`run_and_print`**：遍历 `run_turn(...)` 的事件。
- `("step", step)`：`print(format_step(step, color=sys.stdout.isatty(), verbose=...))`。
- `("interrupt", preview)`：打印 `"[cancel/confirm]".ljust(COLUMN) + "⏸ 等待确认"`，再打印 `format_preview(preview)`。然后取得用户的选择：`auto_confirm` 不是 `None` 时直接用它；否则 `input("确认取消吗？[y/n] ")`，只有回答 `y` 或 `yes`（不区分大小写，去掉首尾空白）算确认。这一轮的事件遍历结束后，递归调用 `run_and_print(graph, ctx, thread_id, resume={"confirm": 是否确认, "order_id": preview["order_id"]}, ...)` 并返回它的结果。
- `("final", values)`：记下来作为返回值。
- 一轮结束（没有中断）时，在最后打印一行统计：`f"—— 有模型参与的步骤 {n} 个: {名字用逗号加空格连接}"`（统计这次调用里 `kind == "model"` 的步骤；一个都没有时打印 `—— 有模型参与的步骤 0 个`）。`values.get("sources")` 非空时再打印一行 `f"—— 引用来源: {', '.join(sources)}"`。
- `verbose` 可以放在模块级变量里，或者给函数加一个参数，自己决定。

**`main`**：解析参数；`graph = build_graph(checkpointer=InMemorySaver())`（`InMemorySaver` 来自 `langgraph.checkpoint.memory`）；`thread_id = uuid.uuid4().hex[:8]`；`ctx = make_context(args.customer, args.today, args.fault, session_id=thread_id)`；先打印一行 `f"会话 {thread_id}   客户: {ctx.customer_id or '未登录'}   今天: {ctx.today}   索引版本: {ctx.index_version or '无'}   模拟故障: {ctx.fault}"`，然后按上面的用法执行。返回 0。文件末尾 `if __name__ == "__main__": raise SystemExit(main())`。

## 第四步：`tests/test_graph.py`

模型全部用假的；数据库用真的；检索用假的。

**假模型**：写一个 `FakeModels` 类，按角色各存一个结果队列（`router`、`planner`、`sql_planner`、`answer`）。它的 `factory(role, schema=None, fault="off")` 返回一个对象，`.invoke(messages)` 从该角色的队列里弹出下一个结果：是异常实例就抛出，否则包成 `ModelResult(value=结果, notes=[])` 返回；同时把 `(role, messages)` 记进 `calls` 列表。用 `monkeypatch.setattr` 把下面 5 处的 `get_model` 都换成它的 `factory`：`app.router.get_model`、`app.supervisor.nodes.get_model`、`app.sql_agent.graph.get_model`、`app.answer.get_model`、`app.handoff.get_model`。

**假检索**：`monkeypatch.setattr("app.rag.retrieval.embed_query", ...)` 换成记录文本并返回 `[0.1] * 8` 的函数；`monkeypatch.setattr("app.rag.retrieval.IndexStore", ...)` 换成一个假类，`check_runtime` 什么都不做，`query` 返回 `[{"doc": "policies/return_policy.md", "section": "Return Windows", "text": "30 days", "score": 0.9}]`。

**运行**：`graph = build_graph(checkpointer=InMemorySaver())`；上下文直接构造 `AppContext(customer_id="CUST-002", index_version="v1", session_id="s-graph")`（不用 `make_context`）；写一个辅助函数 `run(graph, ctx, thread, text=None, resume=None)`，把 `run_turn` 的事件收集成 `(步骤名列表, 中断的预览列表, final 的状态)`。

**清理**：一个 `autouse=True` 的 fixture，每个测试之后用 `connect("root")` 执行并提交：

```sql
UPDATE orders SET status = 'Processing', cancelled_at = NULL WHERE order_id = 'ORD-2025-0038';
DELETE FROM order_cancellations WHERE order_id = 'ORD-2025-0038';
DELETE FROM handoff_tickets WHERE session_id = 's-graph';
```

常用的假结果：
- 路由：`RouteDecision(reason="r", route="sql")` 等。
- 合法的查询计划：`QueryPlan(reasoning="", answerable=True, unanswerable_reason="", select=["orders.order_id", "orders.status"], metrics=[], group_by=[], filters=[], order_by=[{"key": "orders.order_date", "direction": "desc"}], limit=1)`。
- 执行计划 A（三个任务）：`t1 sql "查最近一个订单"` 无依赖；`t2 rag "查这个订单里商品的保修期"` 依赖 `t1`；`t3 rag "退货政策"` 无依赖。
- 有环的执行计划 B：`t1 sql` 依赖 `t2`，`t2 sql` 依赖 `t1`。

每条一个测试函数：

**结构**

1. 图里的节点（不含 `__start__`、`__end__`）恰好是这 13 个：`router`、`planner`、`validate`、`scheduler`、`sql_agent`、`rag_agent`、`answer`、`handoff`、`cancel_extract`、`cancel_check`、`cancel_confirm`、`cancel_execute`、`cancel_reply`。
2. `cancel_execute` 只有一条入边，来自 `cancel_confirm`。
3. 用 `build_graph().get_graph().edges` 建邻接表做可达性搜索：从 `sql_agent`、`rag_agent`、`planner`、`validate`、`scheduler`、`answer`、`handoff` 中的任何一个出发，都到不了任何名字以 `cancel_` 开头的节点。
4. `app/` 目录下所有 `.py` 文件的源码文本里都不含 `bind_tools`。

**流程**

5. 单领域查库：路由 `sql` → 步骤名恰好是 `["router", "t1/sql_plan", "t1/compile", "t1/execute", "answer"]`；`final["answer"]` 是假回答模型返回的文本；`final["task_results"]["t1"]["data"]["row_count"] == 1`；被调用的模型角色依次是 `["router", "sql_planner", "answer"]`。
6. 单领域查文档：路由 `rag` → 步骤名恰好是 `["router", "t1/retrieve", "answer"]`；`final["sources"] == ["policies/return_policy.md"]`；被调用的模型角色依次是 `["router", "answer"]`。
7. 跨领域：路由 `supervisor`，规划返回执行计划 A → 步骤名的前 4 个是 `["router", "planner", "validate", "scheduler"]`；最后 4 个是 `["scheduler", "t2/retrieve", "scheduler", "answer"]`；中间的 4 个（顺序不定）排序后是 `["t1/compile", "t1/execute", "t1/sql_plan", "t3/retrieve"]`；`planner` 角色恰好被调用 1 次；假 `embed_query` 收到的文本里，有一条以 `查这个订单里商品的保修期` 开头并且包含 `ORD-2025-0031`（上游 `t1` 查到的订单号进入了 `t2` 的检索词）；`final["task_results"]` 的三个任务都是 `ok`。
8. 计划两次都不合法：规划两次都返回执行计划 B → 步骤名恰好是 `["router", "planner", "validate", "planner", "validate", "handoff"]`；`final["handoff"]["reason"] == "system_failure"`；`final["card"]["type"] == "ticket"`；`planner` 角色被调用 2 次，`answer` 角色 0 次。
9. 计划第一次不合法、第二次合法：规划依次返回 B、A → 步骤名的前 5 个是 `["router", "planner", "validate", "planner", "validate"]`，最后一个是 `answer`；第二次调用规划模型时收到的用户消息里包含 `上一次的执行计划没有通过校验`。
10. 任务全部失败转人工：路由 `sql`，查询计划两次都不合法（`select=["orders.customer_id"]`）→ 步骤名的最后一个是 `handoff`，没有 `t1/execute`，没有 `answer`；`final["handoff"]["reason"] == "system_failure"`。
11. 匿名用户查订单：`customer_id=None`，路由 `sql`，合法的查询计划 → 步骤名恰好是 `["router", "t1/sql_plan", "t1/compile", "answer"]`；`final["answer"]` 等于 `app.answer.LOGIN_REPLY`；`answer` 角色 0 次调用；数据库里没有 `session_id` 为 `s-graph` 的工单。
12. 闲聊：路由 `direct` → 步骤名恰好是 `["router", "answer"]`。
13. Router 要求转人工：路由 `handoff`、消息 `帮我转人工`、回答模型返回一段摘要 → 步骤名恰好是 `["router", "handoff"]`；`final["handoff"]["reason"] == "user_request"`；`final["answer"]` 里包含 `final["handoff"]["ticket_id"]`。
14. 路由模型不可用：路由队列里放 `ModelUnavailable("x", ["n1"])` → 步骤名恰好是 `["router", "handoff"]`；`final["handoff"]["reason"] == "system_failure"`；`answer` 角色 0 次调用（系统故障时摘要不依赖模型）。
15. 回答模型不可用：路由 `sql`、合法的查询计划、回答队列里放 `ModelUnavailable("x", [])` → 步骤名恰好是 `["router", "t1/sql_plan", "t1/compile", "t1/execute", "answer", "handoff"]`。
16. 取消订单并确认：路由 `cancel`、消息 `帮我取消订单 ORD-2025-0038` → 第一次 `run` 的步骤名恰好是 `["router", "cancel/extract", "cancel/check"]`，中断的预览恰好 1 个且 `order_id` 是 `ORD-2025-0038`；用 `resume={"confirm": True, "order_id": "ORD-2025-0038"}` 再 `run` → 步骤名恰好是 `["cancel/confirm", "cancel/execute", "cancel/reply"]`，`final["answer"] == "订单 ORD-2025-0038 已取消。"`；数据库里这个订单的 `status` 是 `Cancelled`；整个过程除了 `router` 没有调用任何模型。
17. 取消订单但不确认：同上，`resume={"confirm": False, "order_id": "ORD-2025-0038"}` → 步骤名恰好是 `["cancel/confirm", "cancel/reply"]`；数据库里订单的 `status` 仍是 `Processing`。
18. 多轮对话：同一个 `thread` 上先后跑两轮（都是路由 `direct`）→ 第二轮的步骤名恰好是 `["router", "answer"]`（不含第一轮的）；第二轮结束时 `final["messages"]` 有 4 条；第二轮路由模型收到的用户消息里包含 `最近的对话：` 和第一轮的用户消息文本。
19. 每轮重置：同一个 `thread` 上第一轮路由 `sql`（成功），第二轮路由 `direct` → 第二轮结束时 `final["task_results"] == {}`、`final["plan"] is None`、`final["route"] == "direct"`。
20. `run_turn` 的事件：单领域查库的一轮里，最后一个事件的类型是 `final`，其余事件的类型都是 `step`；每个步骤都有 `name`、`kind`、`summary`、`detail`、`notes` 五个键。

**命令行的格式函数**（从 `cli` 导入）

21. `format_step({"name": "router", "kind": "model", "summary": "route=sql   理由: x", "detail": None, "notes": []})` 等于 `"[router]          route=sql   理由: x"`（`[router]` 后面是 10 个空格）。
22. 多行摘要和重试记录：`summary="SELECT 1\nparams = []"`、`notes=["主模型第 1 次调用失败"]`、名字 `t1/compile` → 结果恰好是三行：`"[t1/compile]      SELECT 1"`、18 个空格加 `params = []`、18 个空格加 `↻ 主模型第 1 次调用失败`。
23. 上色：`color=True`、`kind="model"`、`notes=[]` 时结果以 `\033[35m[router]\033[0m` 开头；`notes` 非空时以 `\033[33m` 开头；`kind="code"` 时以 `\033[90m` 开头。
24. `format_preview`：对预览 `{"action_label": "取消订单", "order_id": "ORD-2025-0031", "order_date": "2025-10-17", "status": "Processing", "items": [{"name": "Dell XPS 15", "quantity": 1, "price_per_unit": 1399.0}], "total_amount": 1399.0, "effect": "订单状态将变为已取消；此操作不可撤销"}`，结果的三行去掉行首空格后依次是 `操作: 取消订单   订单: ORD-2025-0031   下单: 2025-10-17   状态: Processing`、`明细: Dell XPS 15 × 1   $1399.00`、`合计: $1399.00   执行后: 订单状态将变为已取消；此操作不可撤销`。

```sh
uv run pytest tests/test_graph.py -q
```

应输出 `24 passed`。

## 最后一步：收尾检查

先跑全部单元测试，再用真实的模型、数据库、索引各跑一次命令行：

```sh
uv run pytest -q
uv run python cli.py --customer CUST-001 "我最近一个订单到哪了？"
uv run python cli.py --customer CUST-001 "我最近一个订单到哪了？另外退货政策是什么？"
uv run python cli.py --customer CUST-002 --no "帮我取消订单 ORD-2025-0031"
```

通过的标准：
- `uv run pytest -q` 的输出里没有 `failed`、没有 `error`。
- 第一条命令行：输出里依次出现以 `[router]`、`[t1/sql_plan]`、`[t1/compile]`、`[t1/execute]`、`[answer]` 开头的行；`[t1/compile]` 那一步里有一行 `params = ['CUST-001']`；`[t1/execute]` 那一行是 `1 行`。
- 第二条命令行：输出里有以 `[planner]`、`[validate]`、`[scheduler]` 开头的行，并且同时有一个 `/execute` 步骤和一个 `/retrieve` 步骤；最后有一行以 `—— 引用来源:` 开头。
- 第三条命令行：输出里依次出现 `[cancel/extract]`、`[cancel/check]`、`⏸ 等待确认`、`[cancel/confirm]`、`[cancel/reply]`，没有 `[cancel/execute]`，回复是 `好的，订单 ORD-2025-0031 没有取消。`

全部通过后最后一行输出 `DONE`。
