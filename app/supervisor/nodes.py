"""Supervisor 的三个节点：规划、校验计划、调度批次。"""
import json

from langgraph.runtime import Runtime

from app.context import AppContext
from app.models import ModelError, get_model
from app.router import conversation_block
from app.state import State, Task, TaskResult, make_step
from app.supervisor.plan import ExecutionPlan, next_batch, validate_plan

MAX_PLAN_ATTEMPTS = 2   # 一份计划最多让模型生成几次，两次都不过就转人工

PLANNER_SYSTEM = """你是电商客服系统的 Supervisor。用户的问题需要多个步骤才能回答，你要一次性把它拆成一份执行计划，之后由程序按计划执行，不会再回来问你。你不回答问题，也不写查询。

可以分派的两个 Agent：
- sql：查数据库。能查到当前客户的订单（状态、日期、金额、物流单号、订单里的商品）、账户资料、商品的价格和是否有货。它知道当前客户是谁，不需要你告诉它。
- rag：查知识库文档。能查到商品的规格、功能、兼容性、使用方法和故障排查，以及退货、保修、配送等政策。

规则：
1. 每个任务只做一件取数的事，goal 用一句完整的话写清楚要拿到什么。写回答不是任务，goal 里不要出现「回答用户」「总结」这类话。
2. 任务编号用 t1、t2、t3……
3. 一个任务要用到另一个任务的结果时，在 depends_on 里写上那个任务的编号；互不相关的任务 depends_on 留空，它们会并行执行。
4. rag 任务可以依赖 sql 任务（例如先查出订单里的商品，再查这个商品的保修政策）。sql 任务不能依赖 rag 任务。
5. 任务数量 1 到 5 个，够用就好。同一个 Agent 一次能查到的内容不要拆成两个任务。
6. 先在 reasoning 里用一句话说明为什么这样拆，再给出 tasks。"""


def planner_node(state: State, runtime: Runtime[AppContext]) -> dict:
    """规划节点：一次模型调用生成完整的执行计划。"""
    fault = runtime.context.fault
    user = conversation_block(state)
    errors = state.get("plan_errors")
    if errors:
        lines = "\n".join(f"- {error}" for error in errors)
        user += f"""

上一次的执行计划没有通过校验：
{lines}
上一次的执行计划：{json.dumps(state.get("plan"), ensure_ascii=False)}
请修正后重新给出完整的执行计划。"""
    attempts = (state.get("plan_attempts") or 0) + 1
    try:
        result = get_model("planner", ExecutionPlan, fault=fault).invoke(
            [("system", PLANNER_SYSTEM), ("user", user)])
    except ModelError as e:
        return {
            "plan": None,
            "plan_attempts": attempts,
            "handoff": {"reason": "system_failure", "detail": "规划模型不可用"},
            "steps": [make_step("planner", "model", "模型调用失败，转人工", None, e.notes)],
        }
    plan = result.value.model_dump()
    summary = [f"执行计划（第 {attempts} 次生成，1 次模型调用）"]
    for task in plan["tasks"]:
        summary.append(f"  {task['id']}  {task['agent']}  {task['goal']}   "
                       f"依赖: {'、'.join(task['depends_on']) or '无'}")
    return {
        "plan": plan,
        "plan_attempts": attempts,
        "steps": [make_step("planner", "model", "\n".join(summary), plan, result.notes)],
    }


def validate_node(state: State, runtime: Runtime[AppContext]) -> dict:
    """校验节点：确定性代码校验计划并算出并行批次。"""
    check = validate_plan(state["plan"])
    attempts = state.get("plan_attempts") or 0
    if check.ok:
        return {
            "batches": check.batches,
            "batch_index": 0,
            "plan_errors": [],
            "steps": [make_step("validate", "code", f"计划通过校验，批次: {check.batches}",
                                {"ok": True, "batches": check.batches})],
        }
    if attempts < MAX_PLAN_ATTEMPTS:
        return {
            "batches": None,
            "plan_errors": check.errors,
            "steps": [make_step("validate", "code",
                                f"计划没有通过校验，交回模型重新生成: {'；'.join(check.errors)}",
                                {"ok": False, "errors": check.errors})],
        }
    return {
        "batches": None,
        "plan_errors": check.errors,
        "handoff": {"reason": "system_failure",
                    "detail": f"执行计划两次都没有通过校验: {'；'.join(check.errors)}"},
        "steps": [make_step("validate", "code",
                            f"计划第 {attempts} 次仍没有通过校验，转人工: {'；'.join(check.errors)}",
                            {"ok": False, "errors": check.errors})],
    }


def scheduler_node(state: State, runtime: Runtime[AppContext]) -> dict:
    """调度节点：算出下一批要执行的任务，把依赖失败的任务标记为跳过。"""
    tasks: dict[str, Task] = {t["id"]: t for t in state["plan"]["tasks"]}
    results: dict[str, TaskResult] = state.get("task_results") or {}
    index, runnable, skipped = next_batch(state["batches"], state["batch_index"], tasks, results)
    if runnable:
        summary = f"第 {index} 批并行: {', '.join(runnable)}"
    else:
        summary = "全部任务已结束"
    if skipped:
        summary += f"   跳过: {', '.join(sorted(skipped))}"
    return {
        "batch_index": index,
        "current_batch": runnable,
        "task_results": skipped,
        "steps": [make_step("scheduler", "code", summary,
                            {"batch": runnable, "skipped": sorted(skipped)})],
    }
