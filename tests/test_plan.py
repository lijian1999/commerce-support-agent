"""执行计划校验和批次调度的单元测试：固定计划，不调模型。"""
import copy

from app.state import make_task_result
from app.supervisor.plan import ExecutionPlan, next_batch, validate_plan


def t(id, agent="sql", deps=()):
    return {"id": id, "agent": agent, "goal": f"目标 {id}", "depends_on": list(deps)}


def plan(*tasks):
    return {"reasoning": "", "tasks": list(tasks)}


def has_error(check, text):
    return any(text in e for e in check.errors)


# ---------- 通过的计划和批次 ----------

def test_single_task_plan_is_valid():
    check = validate_plan(plan(t("t1")))
    assert check.ok and check.errors == [] and check.batches == [["t1"]]


def test_independent_tasks_share_batch_zero():
    check = validate_plan(plan(t("t1", "sql"), t("t2", "rag")))
    assert check.ok and check.batches == [["t1", "t2"]]


def test_design_doc_example():
    # 显示器保修 + 退款到账：t2 依赖 t1，t3 独立
    check = validate_plan(plan(t("t1", "sql"), t("t2", "rag", ["t1"]), t("t3", "rag")))
    assert check.ok and check.batches == [["t1", "t3"], ["t2"]]


def test_forward_reference_is_legal():
    # t1 依赖写在它后面的 t3
    check = validate_plan(plan(t("t1", "rag", ["t3"]), t("t2", "sql"), t("t3", "sql")))
    assert check.ok and check.batches == [["t2", "t3"], ["t1"]]


def test_batch_is_max_dependency_batch_plus_one():
    # 菱形加一条长边：t4 依赖 t2、t3；t5 依赖 t1、t4
    check = validate_plan(plan(t("t1"), t("t2", deps=["t1"]), t("t3", deps=["t1"]),
                               t("t4", deps=["t2", "t3"]), t("t5", deps=["t1", "t4"])))
    assert check.ok and check.batches == [["t1"], ["t2", "t3"], ["t4"], ["t5"]]


def test_accepts_model_dump_of_execution_plan():
    model = ExecutionPlan.model_validate(plan(t("t1"), t("t2", "rag", ["t1"])))
    check = validate_plan(model.model_dump())
    assert check.ok and check.batches == [["t1"], ["t2"]]


# ---------- 被拒绝的计划 ----------

def test_empty_plan():
    check = validate_plan(plan())
    assert not check.ok and check.batches == []
    assert has_error(check, "任务数量必须在 1 到 5 之间，当前为 0")


def test_too_many_tasks():
    check = validate_plan(plan(*[t(f"t{i}") for i in range(1, 7)]))
    assert not check.ok and has_error(check, "任务数量必须在 1 到 5 之间，当前为 6")


def test_duplicate_id():
    check = validate_plan(plan(t("t1"), t("t1", "rag"), t("t2")))
    assert not check.ok and check.errors == ["任务编号重复: t1"]


def test_unknown_dependency():
    check = validate_plan(plan(t("t1"), t("t2", deps=["t9"])))
    assert not check.ok and check.errors == ["任务 t2 依赖了不存在的任务: t9"]


def test_illegal_agent_type():
    check = validate_plan(plan(t("t1", "cancel")))
    assert not check.ok and check.errors == ["任务 t1 的类型不合法: cancel（只能是 sql 或 rag）"]


def test_sql_cannot_depend_on_rag():
    check = validate_plan(plan(t("t1", "rag"), t("t2", "sql", ["t1"])))
    assert not check.ok and check.errors == ["sql 任务 t2 不能依赖 rag 任务 t1"]


def test_rag_may_depend_on_rag_and_sql():
    check = validate_plan(plan(t("t1", "rag"), t("t2", "sql"), t("t3", "rag", ["t1", "t2"])))
    assert check.ok and check.batches == [["t1", "t2"], ["t3"]]


def test_stage_two_collects_all_errors():
    check = validate_plan(plan(t("t1", "rag"), t("t2", "sql", ["t1", "t8"]), t("t3", "email")))
    assert not check.ok
    assert len(check.errors) == 3
    assert has_error(check, "sql 任务 t2 不能依赖 rag 任务 t1")
    assert has_error(check, "任务 t2 依赖了不存在的任务: t8")
    assert has_error(check, "任务 t3 的类型不合法: email")


def test_two_task_cycle():
    check = validate_plan(plan(t("t1", deps=["t2"]), t("t2", deps=["t1"])))
    assert not check.ok and check.errors == ["存在循环依赖: t1 -> t2 -> t1"] and check.batches == []


def test_self_cycle():
    check = validate_plan(plan(t("t1", deps=["t1"])))
    assert not check.ok and check.errors == ["存在循环依赖: t1 -> t1"]


def test_cycle_not_through_start():
    # 从 t1 出发走到 t2 -> t3 -> t2，环上没有 t1
    check = validate_plan(plan(t("t1", deps=["t2"]), t("t2", deps=["t3"]), t("t3", deps=["t2"])))
    assert not check.ok and check.errors == ["存在循环依赖: t2 -> t3 -> t2"]


def test_three_task_cycle_with_healthy_branch():
    check = validate_plan(plan(t("t0", "rag"), t("t1", deps=["t2"]), t("t2", deps=["t3"]), t("t3", deps=["t1"])))
    assert not check.ok and check.errors == ["存在循环依赖: t1 -> t2 -> t3 -> t1"]


# ---------- 调度：next_batch ----------

def tasks_of(*tasks):
    return {x["id"]: x for x in tasks}


def ok(task):
    return make_task_result(task, "ok", data={})


def failed(task):
    return make_task_result(task, "failed", error_kind="db_error", error="查询失败")


def test_next_batch_walks_batches_in_order():
    a, b, c = t("t1"), t("t2", "rag", ["t1"]), t("t3", "rag")
    tasks, batches = tasks_of(a, b, c), [["t1", "t3"], ["t2"]]
    assert next_batch(batches, 0, tasks, {}) == (1, ["t1", "t3"], {})
    results = {"t1": ok(a), "t3": ok(c)}
    assert next_batch(batches, 1, tasks, results) == (2, ["t2"], {})
    results["t2"] = ok(b)
    assert next_batch(batches, 2, tasks, results) == (2, [], {})


def test_failed_dependency_skips_downstream_only():
    a, b, c, d = t("t1"), t("t2", "rag"), t("t3", "rag", ["t1"]), t("t4", "rag", ["t2"])
    tasks, batches = tasks_of(a, b, c, d), [["t1", "t2"], ["t3", "t4"]]
    results = {"t1": failed(a), "t2": ok(b)}
    before = copy.deepcopy(results)
    index, runnable, skipped = next_batch(batches, 1, tasks, results)
    assert (index, runnable) == (2, ["t4"])
    assert set(skipped) == {"t3"}
    assert skipped["t3"]["status"] == "skipped"
    assert skipped["t3"]["error_kind"] == "dependency_failed"
    assert skipped["t3"]["error"] == "依赖的任务 t1 没有成功，已跳过"
    assert skipped["t3"]["task_id"] == "t3" and skipped["t3"]["agent"] == "rag"
    assert results == before   # 不修改入参


def test_skip_propagates_through_fully_skipped_batches():
    a, b, c = t("t1"), t("t2", deps=["t1"]), t("t3", "rag", ["t2"])
    tasks, batches = tasks_of(a, b, c), [["t1"], ["t2"], ["t3"]]
    index, runnable, skipped = next_batch(batches, 1, tasks, {"t1": failed(a)})
    assert (index, runnable) == (3, [])
    assert set(skipped) == {"t2", "t3"}
    assert skipped["t3"]["error"] == "依赖的任务 t2 没有成功，已跳过"


def test_skipped_batch_is_passed_over_to_reach_runnable_one():
    a, b, c, d = t("t1"), t("t2", "rag"), t("t3", deps=["t1"]), t("t4", "rag", ["t2", "t3"])
    e = t("t5", "rag", ["t2"])
    # 批次: [t1, t2] -> [t3, t5] -> [t4]；t1 失败后 t3 跳过、t5 可执行
    tasks, batches = tasks_of(a, b, c, d, e), [["t1", "t2"], ["t3", "t5"], ["t4"]]
    results = {"t1": failed(a), "t2": ok(b)}
    index, runnable, skipped = next_batch(batches, 1, tasks, results)
    assert (index, runnable, set(skipped)) == (2, ["t5"], {"t3"})
    results.update(skipped)
    results["t5"] = ok(e)
    index, runnable, skipped = next_batch(batches, 2, tasks, results)
    assert (index, runnable, set(skipped)) == (3, [], {"t4"})
    assert skipped["t4"]["error"] == "依赖的任务 t3 没有成功，已跳过"
