"""SQL Agent 子图：生成查询计划 → 编译成参数化 SQL → 执行。"""
import json
from typing import Annotated, TypedDict

import pymysql
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime

from app.context import AppContext
from app.db import execute_sql
from app.models import ModelError, get_model
from app.sql_agent.catalog import catalog_prompt
from app.sql_agent.compiler import CompilerBug, PlanRejected, compile_plan
from app.sql_agent.plan import QueryPlan
from app.state import Step, Task, TaskPayload, TaskResult, make_step, make_task_result, reset_or_add

MAX_PLAN_ATTEMPTS = 2   # 一份查询计划最多让模型生成几次，两次都不过就失败


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


SQL_PLAN_SYSTEM = """你是电商客服系统里的查询规划器。你的任务是把用户的问题翻译成一份结构化的查询计划，由程序编译成 SQL 执行。你不写 SQL。

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
13. 如果问题没法用这份结构表达（需要子查询、需要「或者」、需要表里没有的数据），把 answerable 设为 false，在 unanswerable_reason 里写明原因，其余列表留空，limit 写 0。能表达时 answerable 为 true，unanswerable_reason 留空字符串。"""


def build_user_message(state: SqlState) -> str:
    """拼出给模型的用户消息，空的部分整段省略，各段之间空一行。"""
    history = state.get("history") or ""
    question = state.get("question") or ""
    task = state.get("task")
    upstream = state.get("upstream") or {}
    plan_error = state.get("plan_error")
    query_plan = state.get("query_plan")

    blocks: list[str] = []
    if history:
        blocks.append(f"最近的对话：\n{history}")
    blocks.append(f"用户的问题：{question}")
    if task is not None and task["goal"] != question:
        blocks.append(f"本次任务：{task['goal']}")
    if upstream:
        blocks.append(f"前置任务的结果：\n{json.dumps(upstream, ensure_ascii=False)}")
    if plan_error:
        blocks.append(
            f"上一次的查询计划被拒绝了：{plan_error}\n"
            f"上一次的查询计划：{json.dumps(query_plan, ensure_ascii=False)}\n"
            "请修正后重新给出完整的查询计划。"
        )
    return "\n\n".join(blocks)


def sql_plan_node(state: SqlState, runtime: Runtime[AppContext]) -> dict:
    """计划节点：一次模型调用，把问题翻译成结构化的查询计划。"""
    tid = state["task"]["id"]
    attempts = state.get("attempts") or 0
    system = SQL_PLAN_SYSTEM.replace("{today}", runtime.context.today).replace(
        "{catalog}", catalog_prompt())
    try:
        result = get_model("sql_planner", QueryPlan, fault=runtime.context.fault).invoke(
            [("system", system), ("user", build_user_message(state))])
    except ModelError as e:
        return {
            "result": make_task_result(state["task"], "failed", error_kind="model_error",
                                       error="生成查询计划时模型不可用"),
            "steps": [make_step(f"{tid}/sql_plan", "model", "模型调用失败", None, e.notes)],
        }
    plan = result.value.model_dump()
    summary = {key: value for key, value in plan.items()
               if key not in ("reasoning", "unanswerable_reason") and value != []}
    text = json.dumps(summary, ensure_ascii=False)
    if attempts >= 1:
        text = f"（修复）{text}"
    return {
        "query_plan": plan,
        "attempts": attempts + 1,
        "steps": [make_step(f"{tid}/sql_plan", "model", text, plan, result.notes)],
    }


def compile_node(state: SqlState, runtime: Runtime[AppContext]) -> dict:
    """编译节点：把查询计划编译成带参数的 SQL。计划有问题时按类别决定修复或失败。"""
    tid = state["task"]["id"]
    task = state["task"]
    attempts = state.get("attempts") or 0
    plan = QueryPlan.model_validate(state["query_plan"])
    try:
        query = compile_plan(plan, runtime.context.customer_id, runtime.context.today)
    except PlanRejected as e:
        if e.kind == "fixable":
            if attempts < MAX_PLAN_ATTEMPTS:
                return {
                    "compiled": None,
                    "plan_error": e.message,
                    "steps": [make_step(
                        f"{tid}/compile", "code", f"计划被拒绝，交回模型修复: {e.message}",
                        {"error": e.message, "kind": e.kind})],
                }
            return {
                "result": make_task_result(task, "failed", error_kind="invalid_plan",
                                           error="没能生成合法的查询计划"),
                "steps": [make_step(
                    f"{tid}/compile", "code", f"计划第 {attempts} 次仍被拒绝，任务失败: {e.message}",
                    {"error": e.message, "kind": e.kind})],
            }
        if e.kind == "login_required":
            return {
                "result": make_task_result(task, "failed", error_kind="login_required", error=e.message),
                "steps": [make_step(f"{tid}/compile", "code", f"拒绝: {e.message}",
                                    {"error": e.message, "kind": e.kind})],
            }
        return {
            "result": make_task_result(task, "failed", error_kind="unanswerable",
                                       error=f"这个问题超出了可查询的范围: {e.message}"),
            "steps": [make_step(f"{tid}/compile", "code",
                                f"模型声明无法用查询计划表达: {e.message}",
                                {"error": e.message, "kind": e.kind})],
        }
    except CompilerBug as e:
        return {
            "result": make_task_result(task, "failed", error_kind="invalid_plan", error="查询编译出错"),
            "steps": [make_step(f"{tid}/compile", "code", f"编译器出口断言失败: {e}",
                                {"error": str(e), "kind": "bug"})],
        }
    scope = ("客户范围由编译器加入，值来自 Runtime" if "customer_id = %s" in query.sql
             else "只涉及公开表，不加客户范围")
    return {
        "compiled": {"sql": query.sql, "params": list(query.params)},
        "plan_error": None,
        "steps": [make_step(f"{tid}/compile", "code",
                            f"{query.sql}\nparams = {query.params!r}",
                            {"sql": query.sql, "params": list(query.params), "customer_scope": scope})],
    }


def execute_node(state: SqlState, runtime: Runtime[AppContext]) -> dict:
    """执行节点：执行编译好的那一条 SQL，没有循环。"""
    tid = state["task"]["id"]
    compiled = state["compiled"]
    try:
        data = execute_sql(compiled["sql"], compiled["params"])
    except pymysql.MySQLError as e:
        return {
            "result": make_task_result(state["task"], "failed", error_kind="db_error",
                                       error="数据库查询失败"),
            "steps": [make_step(f"{tid}/execute", "code", f"执行失败: {e}", {"error": str(e)})],
        }
    return {
        "result": make_task_result(state["task"], "ok", data=data),
        "steps": [make_step(f"{tid}/execute", "code", f"{data['row_count']} 行", data)],
    }


def after_sql_plan(state: SqlState) -> str:
    if state.get("result"):
        return END
    return "compile"


def after_compile(state: SqlState) -> str:
    if state.get("result"):
        return END
    if state.get("compiled"):
        return "execute"
    return "sql_plan"


def build_sql_graph() -> CompiledStateGraph:
    """三节点的 SQL 子图：sql_plan → compile → execute，编译不通过时最多回炉一次。"""
    g = StateGraph(SqlState, context_schema=AppContext)
    g.add_node("sql_plan", sql_plan_node)
    g.add_node("compile", compile_node)
    g.add_node("execute", execute_node)
    g.add_edge(START, "sql_plan")
    g.add_conditional_edges("sql_plan", after_sql_plan, ["compile", END])
    g.add_conditional_edges("compile", after_compile, ["execute", "sql_plan", END])
    g.add_edge("execute", END)
    return g.compile()


_SQL_GRAPH = build_sql_graph()


def sql_agent_node(payload: TaskPayload, runtime: Runtime[AppContext]) -> dict:
    """主图里的 SQL 节点：内部调用子图，步骤由子图的节点产生，这里只汇总任务结果。"""
    out = _SQL_GRAPH.invoke(
        {"task": payload["task"], "question": payload["question"], "history": payload["history"],
         "upstream": payload["upstream"], "query_plan": None, "plan_error": None, "attempts": 0,
         "compiled": None, "result": None, "steps": None},
        context=runtime.context,
    )
    return {"task_results": {payload["task"]["id"]: out["result"]}}
