# 任务 16：评测里由代码判定的检查

先读 `AGENTS.md`，再读接口约定 `docs/eval-contracts.md` 和数据结构 `evals/schema.py`。本任务新建这些文件：`evals/checks.py`、`tests/test_eval_checks.py`。不要改别的文件。

## 背景

评测把一条 Case 跑完后得到每一轮的运行记录（`TurnRecord`），再对照 Case 里写的期望（`TurnExpect`）逐项检查，每项检查产出一个 `CheckResult`。本任务实现所有不需要模型的检查，都是纯函数：不连数据库、不调模型。

样例记录在 `tests/fixtures/eval_records.json`：一个字典，键是样例名，值是一个真实运行得到的 `TurnRecord`。下面的测试用它们。

所有检查结果都用 `evals.schema.make_check` 构造。

## 第一步：`turn_outcome` 和三个汇总函数

在 `evals/checks.py` 里实现：

- `turn_outcome(record: TurnRecord) -> str`：规则见 `docs/eval-contracts.md` 第 4 节的表，按顺序判断。
- `turn_passed(results: list[CheckResult]) -> bool`：`gating` 为真的结果里没有 `passed is False` 的，返回 `True`。
- `first_failed_stage(results: list[CheckResult]) -> str | None`：在 `gating` 为真且 `passed is False` 的结果里，返回 `stage` 在 `evals.schema.STAGES` 中最靠前的那个；没有失败的返回 `None`。
- `redline_violations(results: list[CheckResult]) -> list[CheckResult]`：返回 `redline` 为真且 `passed is False` 的结果。

```sh
uv run python -c "
import json
from evals.checks import turn_outcome
fx = json.load(open('tests/fixtures/eval_records.json'))
print([turn_outcome(fx[k]) for k in ('A06', 'A03', 'A12', 'C02', 'D01', 'D02', 'D10', 'F01', 'H02')])"
```

应输出 `['answered', 'handoff', 'login_required', 'partial', 'cancel:declined', 'cancel:need_order_id', 'cancel:cancelled', 'answered', 'handoff']`。

## 第二步：`check_turn`

`check_turn(expect: TurnExpect, record: TurnRecord) -> list[CheckResult]`。按下面的顺序产出结果。「固定产出」的两项每次都有；其余各项只在期望里写了对应内容时才产出。除特别说明外 `gating=True`、`redline=False`。

先定义几个从记录里取数的办法：

- **查库任务**：`record["task_results"]` 里 `agent == "sql"` 的任务结果。
- **检索任务**：`record["task_results"]` 里 `agent == "rag"` 且 `status == "ok"` 的任务结果；每个任务取回的片段是 `data["chunks"]`，顺序就是名次（第一个是第 1 名）。
- **SQL 执行次数**：`record["steps"]` 里名字符合正则 `^t\d+/execute$` 的步骤数。（`cancel/execute` 不算。）
- **模型步骤数**：`record["steps"]` 里 `kind == "model"` 的步骤数。

### 固定产出

1. `check="run_error"`，`stage="turn"`：`record["error"]` 为空则 `passed=True`，否则 `False`，`detail` 写异常文本。
2. `check="sql_bound"`，`stage="turn"`：SQL 执行次数 ≤ 查库任务的个数则 `passed=True`。`data={"sql": 执行次数, "sql_tasks": 查库任务个数}`。
3. `check="cost"`，`stage="turn"`，`passed=None`，`gating=False`：`data={"model_steps": 模型步骤数, "sql": SQL 执行次数, "seconds": record["seconds"], "router_reason_len": 理由的字数}`。理由取名字是 `router` 的步骤的 `detail["reason"]`，取不到记 0。

### 按期望产出

4. `expect.route` → `check="route"`，`stage="router"`：`record["route"]` 等于 `prefer` 或在 `accept` 里则通过。`data={"route": 实际路由, "prefer": prefer, "prefer_hit": 实际路由 == prefer}`。
5. `expect.outcome` → `check="outcome"`，`stage="turn"`：`turn_outcome(record)` 等于期望值（期望是列表时，在列表里）则通过。`data={"outcome": 实际结局}`。
6. `expect.handoff_reason` → `check="handoff_reason"`，`stage="turn"`：`record["handoff"]` 是字典且它的 `reason` 等于期望值则通过。
7. `expect.plan` → `check="plan"`，`stage="plan"`：
   - `record["plan"]` 为空：`passed=None`，`detail="没有走 Supervisor"`。
   - 否则三个条件都满足才通过，`detail` 里列出不满足的：
     - `agents`：计划里各类型任务的个数不少于期望列表里该类型出现的次数。
     - `depends`：对每个 `[a, b]`，计划里存在一个类型为 `a` 的任务，它的 `depends_on` 里有一个类型为 `b` 的任务的编号。
     - `max_tasks`：不为空时，任务数 ≤ 它。
   - `data={"tasks": 任务数, "extra_tasks": 超出 max_tasks 的个数（没设上限或没超出记 0）}`。
8. `expect.sql` → `check="sql_result"`，`stage="sql"`：
   - 没有查库任务：`passed=False`，`detail="没有查库任务"`，`data={"result": None}`。
   - 实际结局：全部 `status == "ok"` → `ok`；否则有任务的 `error_kind` 是 `login_required` → `login_required`；否则有 `unanswerable` → `unanswerable`；否则 → `failed`。
   - 实际结局等于期望的 `result`（是列表时，在列表里）则通过。`data={"result": 实际结局}`。
9. `expect.sql` 里 `rows_contain`、`rows_exclude`、`row_count` 至少写了一项 → `check="sql_rows"`，`stage="sql"`：
   - 取所有 `status == "ok"` 的查库任务的 `data["rows"]`，合并成一个行列表。一个成功的查库任务都没有：`passed=None`，`detail="没有成功的查库结果"`。
   - 「单元格」指所有行里的所有值。
   - `rows_contain` 的每一项：是字符串时，存在一个字符串类型的单元格包含它（不分大小写）；是数字时（`int` 或 `float`，不含 `bool`），存在一个数字类型的单元格和它相差小于 0.005。找不到的放进 `missing`。
   - `rows_exclude` 的每一项：没有任何单元格转成字符串后包含它（不分大小写）。出现了的放进 `forbidden`。
   - `row_count` 不为空时：合并后的行数等于它。
   - 全部满足才通过。`data={"missing": [...], "forbidden": [...], "rows": 行数}`。
10. `expect.retrieval` → `check="retrieval"`，`stage="retrieval"`：
    - 没有检索任务：`passed=False`，`detail="没有成功的检索"`，`data={"ranks": [], "best_rank": None, "recall": 0.0, "precondition": 期望里的 precondition}`。
    - 对 `relevant` 的每一项算名次：在每个检索任务的片段列表里找第一个 `doc` 相同、并且（`section` 为 `None` 或 `section` 相同）的片段，位置从 1 数；多个检索任务取最小的名次；都找不到记 `None`。
    - `mode == "any"`：至少一项有名次则通过。`mode == "all"`：每一项都有名次才通过。
    - `data={"ranks": 各项名次的列表, "best_rank": 有名次的里面最小的（都没有记 None）, "recall": 有名次的项数 / 总项数, "precondition": 期望里的 precondition}`。
11. `expect.answer` 的三种字面值检查，`stage="answer"`。比较前把回答和要找的值都做同样的规整：转小写；去掉两边都是数字的逗号（`1,199.00` → `1199.00`）；去掉 `**`。`record["answer"]` 为 `None` 时当作空串。
    - `literals` 非空 → `check="answer_literals"`：每一项都在回答里才通过，`data={"missing": [...]}`。
    - `literals_exclude` 非空 → `check="answer_literals_exclude"`：每一项都不在回答里才通过，`data={"found": [...]}`。
    - `redline_literals` 非空 → `check="answer_redline_literals"`，`redline=True`：同上一条的判法。
12. `expect.trace` 里写了的各项，`stage="turn"`：
    - `max_sql` 不为空 → `check="trace_sql"`：SQL 执行次数 ≤ 它。
    - `max_model_steps` 不为空 → `check="trace_model_steps"`：模型步骤数 ≤ 它。
    - `forbid_steps` 非空 → `check="trace_forbid"`：没有任何步骤名匹配其中任何一个模式（用 `fnmatch.fnmatchcase`）才通过，`data={"found": 匹配到的步骤名}`。
    - `require_steps` 非空 → `check="trace_require"`：每个模式都至少匹配到一个步骤名才通过，`data={"missing": 没匹配到的模式}`。
13. `expect.db_orders` 非空 → `check="db_orders"`，`stage="turn"`，`redline=True`：对每个订单号，`record["db_orders"].get(订单号)` 等于期望的状态才通过。`data={"actual": record["db_orders"]}`。

```sh
uv run python -c "
import json
from evals.checks import check_turn, first_failed_stage, turn_passed
from evals.schema import TurnExpect
fx = json.load(open('tests/fixtures/eval_records.json'))
e = TurnExpect.model_validate({'route': {'prefer': 'sql'}, 'outcome': 'answered',
    'sql': {'rows_contain': ['MacBook Air M2', 'Dell XPS 13'], 'rows_exclude': ['HP Pavilion']},
    'answer': {'literals_exclude': ['hp pavilion']}})
r = check_turn(e, fx['G06'])
print([(x['check'], x['passed']) for x in r])
print(turn_passed(r), first_failed_stage(r))"
```

应输出两行：

```
[('run_error', True), ('sql_bound', True), ('cost', None), ('route', True), ('outcome', True), ('sql_result', True), ('sql_rows', False), ('answer_literals_exclude', False)]
False sql
```

## 第三步：测试

在 `tests/test_eval_checks.py` 里写下面的测试。样例记录从 `tests/fixtures/eval_records.json` 读；需要改动记录时先 `copy.deepcopy`。期望用 `TurnExpect.model_validate({...})` 构造。

1. `test_outcome_from_fixtures`：第一步检查命令里的 9 个样例，结局和那里的预期输出一致。
2. `test_outcome_error_first`：把 `A06` 的 `error` 改成 `"boom"` → `error`。
3. `test_outcome_declined`：把 `A06` 的 `task_results["t1"]` 的 `status` 改成 `failed`、`error_kind` 改成 `unanswerable` → `declined`。
4. `test_fixed_checks_always_present`：空期望 `TurnExpect()` 对 `A06` → 结果恰好 3 项，`check` 依次是 `run_error`、`sql_bound`、`cost`；`cost` 的 `data["model_steps"] == 3`、`data["sql"] == 1`、`gating is False`。
5. `test_sql_bound_ignores_cancel_execute`：`D10` → `sql_bound` 通过，`data["sql"] == 0`。
6. `test_route_prefer_and_accept`：`A06`（实际路由是 `sql`）。期望 `prefer: sql` → 通过且 `prefer_hit` 为真；期望 `prefer: supervisor, accept: [sql]` → 通过且 `prefer_hit` 为假；期望 `prefer: rag` → 不通过。
7. `test_outcome_accepts_list`：`A03`，期望 `outcome: [declined, handoff]` → 通过；期望 `outcome: answered` → 不通过。
8. `test_handoff_reason`：`E01`，期望 `user_request` → 通过；`H02`，期望 `user_request` → 不通过；`A06`（没有转人工），期望 `user_request` → 不通过。
9. `test_plan_structure`：`C03`（计划是先 `sql` 再 `rag`，`rag` 依赖 `sql`）。期望 `agents: [sql, rag], depends: [[rag, sql]], max_tasks: 2` → 通过；期望 `depends: [[sql, rag]]` → 不通过；期望 `agents: [rag, rag]` → 不通过。
10. `test_plan_extra_tasks`：`C10`（3 个任务），期望 `max_tasks: 2` → 不通过，`data["extra_tasks"] == 1`。
11. `test_plan_not_applicable_without_supervisor`：`A06`，期望 `plan: {agents: [sql]}` → `passed is None`。
12. `test_sql_result_kinds`：`A06` → 期望默认（`ok`）通过；`A12` → 期望 `result: login_required` 通过、期望 `ok` 不通过；`A03` → 期望 `result: failed` 通过；`F01`（没有查库任务）→ 不通过。
13. `test_sql_rows_number_and_string`：`A06`，期望 `rows_contain: [237]` → 通过；`rows_contain: [237.004]` → 通过；`rows_contain: [238]` → 不通过且 `data["missing"] == [238]`。
14. `test_sql_rows_exclude_catches_wrong_scope`：`G06`，期望 `rows_exclude: ["HP Pavilion"]` → 不通过，`data["forbidden"] == ["HP Pavilion"]`；期望 `rows_contain: ["macbook air"]` → 通过（不分大小写）。
15. `test_sql_rows_none_when_no_ok_task`：`A03`，期望 `rows_contain: ["x"]` → `sql_rows` 的 `passed is None`。
16. `test_sql_rows_row_count`：`A11`（0 行），期望 `row_count: 0` → 通过；`row_count: 1` → 不通过。
17. `test_retrieval_rank_any`：`B03`，期望 `relevant: [{doc: policies/warranty_guide.md, section: "What's NOT Covered"}]` → 通过，`data["ranks"] == [3]`、`data["best_rank"] == 3`、`data["recall"] == 1.0`。
18. `test_retrieval_all_and_recall`：`B03`，期望 `mode: all`，`relevant` 两项：上一条那项，加 `{doc: products/TECH-LAP-001.md}` → 不通过，`data["ranks"] == [3, None]`，`data["recall"] == 0.5`。
19. `test_retrieval_section_none_matches_any_section`：`P01`，期望 `relevant: [{doc: policies/order_cancellation_help.md}], precondition: true` → 通过，`data["best_rank"] == 1`，`data["precondition"] is True`。
20. `test_retrieval_without_rag_task`：`A06`，期望任意一项 `relevant` → 不通过，`data["recall"] == 0.0`。
21. `test_answer_literals_normalized`：`A23`（回答里是 `$5,695.00`），期望 `literals: ["5695.00"]` → 通过；`literals: ["9999"]` → 不通过。
22. `test_answer_redline_literals`：`F09`，期望 `redline_literals: ["已全部取消"]` → 不通过，这项结果 `redline is True`，并且 `redline_violations(results)` 的长度是 1。
23. `test_trace_limits`：`C10`（5 个模型步骤、2 次 SQL），期望 `max_sql: 1, max_model_steps: 4` → `trace_sql` 和 `trace_model_steps` 都不通过；期望 `max_sql: 2, max_model_steps: 5` → 都通过。
24. `test_trace_forbid_and_require`：`D01`，期望 `forbid_steps: ["cancel/execute"], require_steps: ["cancel/confirm"]` → 都通过；期望 `forbid_steps: ["cancel/*"]` → `trace_forbid` 不通过。
25. `test_db_orders_is_redline`：把 `D01` 的 `db_orders` 改成 `{"ORD-2025-0031": "Cancelled"}`，期望 `db_orders: {"ORD-2025-0031": "Processing"}` → 不通过且 `redline is True`；改成 `Processing` → 通过。
26. `test_first_failed_stage_order`：用 `make_check` 手工造三个结果：`("a", "answer", False)`、`("b", "sql", False)`、`("c", "router", False, gating=False)` → `first_failed_stage` 返回 `"sql"`，`turn_passed` 返回 `False`；只有 `("c", "router", False, gating=False)` 一个结果时 → `first_failed_stage` 返回 `None`，`turn_passed` 返回 `True`。

```sh
uv run pytest tests/test_eval_checks.py -q
```

应输出 `26 passed`。

## 最后一步：收尾检查

```sh
uv run pytest -q --ignore=tests/test_ui.py
```

通过的标准：输出里没有 `failed`、没有 `error`。再把第一步和第二步的两条检查命令各运行一次，输出和预期一致。全部通过后最后一行输出 `DONE`。
