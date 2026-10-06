"""执行计划的结构、确定性校验（同时算出并行批次）和批次调度。全部纯函数，不涉及模型、数据库和图。"""
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from app.state import Task, TaskResult, make_task_result

MAX_TASKS = 5
READ_ONLY_AGENTS = frozenset({"sql", "rag"})   # 任务类型 → 只读。只读与否由类型决定，不由模型声明


class PlanTask(BaseModel):
    """计划里的一个任务。"""
    id: str = Field(description="任务编号，如 t1")
    agent: Literal["sql", "rag"] = Field(description="由哪个 Agent 执行，只能是 sql 或 rag")
    goal: str = Field(description="这个任务要拿到什么数据，一句完整的话")
    depends_on: list[str] = Field(description="依赖的任务编号列表，没有依赖时为空列表")


class ExecutionPlan(BaseModel):
    """Supervisor 一次生成的完整执行计划。"""
    reasoning: str = Field(description="为什么这样拆任务，一段简短的说明")
    tasks: list[PlanTask] = Field(description="要执行的任务列表，编号唯一，依赖指向同一份计划里的任务")


@dataclass
class PlanCheck:
    """计划校验结果。ok 为 False 时 errors 有内容、batches 是空列表。"""
    ok: bool
    errors: list[str]
    batches: list[list[str]]    # 校验不通过时为空列表


class _CycleError(Exception):
    """DFS 走到灰色任务时抛出，携带环的文本描述。"""


def _normalize(plan: dict) -> list[dict]:
    """把 plan["tasks"] 取成列表，字段缺失时补默认值，避免脏数据把校验本身弄崩。"""
    tasks = plan.get("tasks") or []
    items: list[dict] = []
    for item in tasks:
        if not isinstance(item, dict):
            continue
        deps = item.get("depends_on") or []
        items.append({"id": item.get("id"), "agent": item.get("agent"),
                      "goal": item.get("goal", ""), "depends_on": list(deps)})
    return items


def validate_plan(plan: dict) -> PlanCheck:      # 入参是 ExecutionPlan.model_dump() 形状的字典
    """分三个阶段确定性地校验计划，通过时算出并行批次。"""
    items = _normalize(plan)
    errors: list[str] = []

    # 阶段 1：数量 + 登记编号
    if not 1 <= len(items) <= MAX_TASKS:
        errors.append(f"任务数量必须在 1 到 {MAX_TASKS} 之间，当前为 {len(items)}")
    registered: set[str] = set()
    duplicated: set[str] = set()
    for item in items:
        tid = item["id"]
        if tid in registered and tid not in duplicated:
            duplicated.add(tid)
            errors.append(f"任务编号重复: {tid}")
        registered.add(tid)
    if errors:
        return PlanCheck(ok=False, errors=errors, batches=[])

    # 阶段 2：类型和依赖引用
    agent_of: dict[str, str] = {item["id"]: item["agent"] for item in items}
    for item in items:
        tid, agent = item["id"], item["agent"]
        if agent not in READ_ONLY_AGENTS:
            errors.append(f"任务 {tid} 的类型不合法: {agent}（只能是 sql 或 rag）")
        for dep in item["depends_on"]:
            if dep not in registered:
                errors.append(f"任务 {tid} 依赖了不存在的任务: {dep}")
            elif agent == "sql" and agent_of[dep] == "rag":
                errors.append(f"sql 任务 {tid} 不能依赖 rag 任务 {dep}")
    if errors:
        return PlanCheck(ok=False, errors=errors, batches=[])

    # 阶段 3：DFS 三色标记，环检测 + 批次号
    deps_of: dict[str, list[str]] = {item["id"]: item["depends_on"] for item in items}
    white, gray, black = 0, 1, 2
    color: dict[str, int] = {tid: white for tid in registered}
    batch_of: dict[str, int] = {}
    path: list[str] = []

    def visit(tid: str) -> None:
        color[tid] = gray
        path.append(tid)
        for dep in deps_of[tid]:
            if color[dep] == gray:
                cycle = path[path.index(dep):] + [dep]
                raise _CycleError("存在循环依赖: " + " -> ".join(cycle))
            if color[dep] == white:
                visit(dep)
        path.pop()
        color[tid] = black
        # 没有依赖是 0 批，否则是依赖的最大批次加 1
        batch_of[tid] = 0 if not deps_of[tid] else max(batch_of[d] for d in deps_of[tid]) + 1

    try:
        for item in items:
            if color[item["id"]] == white:
                visit(item["id"])
    except _CycleError as exc:
        return PlanCheck(ok=False, errors=[str(exc)], batches=[])

    highest = max(batch_of.values()) if batch_of else 0
    batches: list[list[str]] = [[] for _ in range(highest + 1)]
    for item in items:        # 同一批里按任务在计划里出现的顺序
        batches[batch_of[item["id"]]].append(item["id"])
    return PlanCheck(ok=True, errors=[], batches=batches)


def next_batch(batches: list[list[str]], batch_index: int, tasks: dict[str, Task],
               results: dict[str, TaskResult]) -> tuple[int, list[str], dict[str, TaskResult]]:
    """从 batch_index 起找下一批要执行的任务，返回（新的下标, 任务编号, 本次新标记跳过的结果）。"""
    view = dict(results)      # 入参不可变，跳过结果只写进副本
    skipped: dict[str, TaskResult] = {}
    for index in range(batch_index, len(batches)):
        runnable: list[str] = []
        for tid in batches[index]:
            task = tasks.get(tid)
            blocked_by: str | None = None
            for dep in (task["depends_on"] if task else []):
                old = view.get(dep)
                if old is not None and old["status"] != "ok":
                    blocked_by = dep
                    break
            if blocked_by is not None:
                # 有多个不成功的依赖时写第一个
                result = make_task_result(task, "skipped", error_kind="dependency_failed",
                                          error=f"依赖的任务 {blocked_by} 没有成功，已跳过")
                skipped[tid] = result
                view[tid] = result     # 后面的批次也能看到这条跳过
            else:
                runnable.append(tid)
        if runnable:
            return index + 1, runnable, skipped
        # 这一批全部跳过，继续看下一批
    return len(batches), [], skipped
