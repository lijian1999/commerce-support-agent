# 任务 03：执行计划的结构、校验、批次调度

先读 `AGENTS.md`，再读 `docs/contracts.md` 的第 6.1 节。本任务新建文件 `app/supervisor/plan.py`。测试文件 `tests/test_plan.py` 已经写好，**只读**，不要改它。不要改别的文件。

## 背景

Supervisor 的模型一次生成一份执行计划：拆成哪几个任务、交给哪个 Agent（`sql` 或 `rag`）、谁依赖谁。计划在执行前要经过确定性的校验，校验的同时算出并行批次；执行过程中按批次调度，依赖失败的任务跳过。本任务全部是纯函数，不涉及模型、数据库和图。

## 第一步：结构

在 `app/supervisor/plan.py` 里写 `docs/contracts.md` 第 6.1 节的 `PlanTask`、`ExecutionPlan` 两个 pydantic 类和 `PlanCheck` 数据类，字段顺序不变，不加默认值。pydantic 字段加 `Field(description="...")`，一句中文说明（例如 `goal` →「这个任务要拿到什么数据，一句完整的话」，`depends_on` →「依赖的任务编号列表，没有依赖时为空列表」）。另外定义两个常量：

```python
MAX_TASKS = 5
READ_ONLY_AGENTS = frozenset({"sql", "rag"})   # 任务类型 → 只读。只读与否由类型决定，不由模型声明
```

```sh
uv run python -c "from app.supervisor.plan import ExecutionPlan, PlanTask, PlanCheck, MAX_TASKS, READ_ONLY_AGENTS; print(list(ExecutionPlan.model_fields), list(PlanTask.model_fields), MAX_TASKS, sorted(READ_ONLY_AGENTS))"
```

应输出 `['reasoning', 'tasks'] ['id', 'agent', 'goal', 'depends_on'] 5 ['rag', 'sql']`。

## 第二步：`validate_plan(plan: dict) -> PlanCheck`

入参是字典 `{"reasoning": str, "tasks": [{"id", "agent", "goal", "depends_on"}, ...]}`（不是 pydantic 实例，测试会传入 pydantic 不允许的值）。按下面的顺序检查。每一阶段把发现的所有错误都收集起来；某一阶段有错误，就不再做后面的阶段，直接返回 `PlanCheck(ok=False, errors=错误列表, batches=[])`。

**阶段 1：数量和登记编号（第一遍扫描）**
- 任务数不在 1 到 `MAX_TASKS` 之间 → 错误 `任务数量必须在 1 到 5 之间，当前为 <数量>`。
- 遍历任务登记编号。同一个编号出现多次 → 错误 `任务编号重复: <编号>`（每个重复的编号报一次）。

**阶段 2：类型和依赖引用（第二遍扫描）**
- `agent` 不在 `READ_ONLY_AGENTS` 里 → 错误 `任务 <编号> 的类型不合法: <agent>（只能是 sql 或 rag）`。
- `depends_on` 里的编号没有登记过 → 错误 `任务 <编号> 依赖了不存在的任务: <被依赖的编号>`。因为第一遍已经登记了全部编号，依赖一个写在自己后面的任务是合法的。
- 一个 `sql` 任务直接依赖了一个 `rag` 任务 → 错误 `sql 任务 <编号> 不能依赖 rag 任务 <被依赖的编号>`。`rag` 依赖 `sql`、`sql` 依赖 `sql`、`rag` 依赖 `rag` 都允许。

**阶段 3：环检测和批次号（DFS 三色标记）**
- 每个任务有三种颜色：白（未访问）、灰（正在访问，在当前的递归路径上）、黑（已完成）。按任务在计划里的顺序，对每个白色任务做深度优先遍历，沿 `depends_on` 往下走，依赖按列表里的顺序访问。
- 访问到一个灰色任务说明有环 → 错误 `存在循环依赖: <环上的编号用 " -> " 连接，首尾是同一个编号>`，例如 `存在循环依赖: t1 -> t2 -> t1`，自己依赖自己是 `存在循环依赖: t1 -> t1`。发现第一个环就停止，返回失败。环的写法：从被再次访问到的那个灰色任务开始，沿当前递归路径走到底，最后再写一次这个任务。
- 任务由灰变黑时算它的批次号：没有依赖是 0，否则是它所有依赖的批次号的最大值加 1。

**通过时**返回 `PlanCheck(ok=True, errors=[], batches=批次列表)`。`batches[i]` 是批次号为 `i` 的任务编号列表，同一批里按任务在计划里出现的顺序排列。

## 第三步：`next_batch`

```python
def next_batch(batches: list[list[str]], batch_index: int, tasks: dict[str, Task],
               results: dict[str, TaskResult]) -> tuple[int, list[str], dict[str, TaskResult]]
```

- `tasks`：任务编号 → 任务字典（`Task` 来自 `app.state`）。`results`：已经有结果的任务（`TaskResult` 来自 `app.state`）。不要修改传入的 `results`。
- 作用：从下标 `batch_index` 的批次开始，找出下一批要执行的任务。返回 `(新的 batch_index, 要执行的任务编号列表, 这次新标记为跳过的结果)`。
- 做法：依次看下标为 `batch_index`、`batch_index + 1`…的批次。对批次里的每个任务：
  - 它的依赖里只要有一个的结果 `status` 不是 `"ok"`（`failed` 或 `skipped`），这个任务就标记为跳过：用 `app.state.make_task_result(task, "skipped", error_kind="dependency_failed", error="依赖的任务 <那个依赖的编号> 没有成功，已跳过")` 生成结果（多个依赖都不成功时写第一个）。本次调用里前面批次被标记为跳过的任务，对后面批次的判断同样算数。
  - 否则它是可执行的。
  - 这一批有可执行的任务 → 返回 `(这一批的下标 + 1, 可执行的任务编号（保持批内顺序）, 到目前为止本次新标记的跳过结果)`。
  - 这一批全部被跳过 → 继续看下一批。
- 所有批次都看完了 → 返回 `(len(batches), [], 本次新标记的跳过结果)`。

## 最后一步：收尾检查

```sh
uv run pytest tests/test_plan.py -q
```

通过的标准：输出里没有 `failed`、没有 `error`，全部测试 `passed`。全部通过后最后一行输出 `DONE`。
