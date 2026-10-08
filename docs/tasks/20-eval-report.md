# 任务 20：评测结果的评估、汇总和报告文本

先读 `AGENTS.md`，再读接口约定 `docs/eval-contracts.md`（重点是第 5、8 节）和数据结构 `evals/schema.py`。本任务新建这些文件：`evals/report.py`、`tests/test_eval_report.py`。不要改别的文件。

## 背景

一条 Case 跑一次得到一个 `CaseRun`。`evals/checks.py` 的 `check_turn`（代码判）和 `evals/judges.py` 的 `judge_turn`（模型判）对每一轮各产出一组 `CheckResult`。本任务把它们合成一次运行的评估结果，再把所有运行汇总成报告。全部是纯函数，只有 `evaluate_run` 在 `judge=True` 时会间接调用模型。

## 第一步：`evaluate_run`

`evaluate_run(case: Case, case_run: CaseRun, judge: bool = True, votes: int = 3) -> dict`，返回接口约定第 8 节的 RunResult。

- 对 `case.turns` 的第 i 轮：
  - `case_run["turns"]` 里有第 i 条记录：`checks = check_turn(turn.expect, record)`；`judge` 为真时再接上 `judge_turn(turn.expect, record, votes)`。`outcome = turn_outcome(record)`，`question = record["question"]`。
  - 没有第 i 条记录：`checks = [make_check("run_error", "turn", False, "这一轮没有运行记录")]`，`outcome = "error"`，`question = turn.user`。
  - `passed = turn_passed(checks)`，`first_failed_stage = first_failed_stage(checks)`。
  - `meta`：`planner_steps` 是记录的步骤里名字等于 `planner` 的个数；`sql_tasks` 是 `task_results` 里 `agent == "sql"` 的任务个数；`sql_tasks_first_try` 是这些查库任务里，名字等于 `f"{任务编号}/sql_plan"` 的步骤恰好出现 1 次的任务个数。没有记录时三个都是 0。
- 整次运行：`passed` 是每一轮都通过；`first_failed_stage` 取第一个没通过的那一轮的；`redlines` 是各轮 `redline_violations(checks)` 的结果，每项写成 `{"turn": 轮次下标, "check": 检查名, "detail": 说明}`；`invalid` 是存在一项检查 `data.get("precondition") is True` 且 `passed is False`。

## 第二步：`summarize`

`summarize(cases: list[Case], results: list[dict], repeats: int) -> dict`。`results` 是全部 RunResult。下面「轮」指所有 RunResult 的所有 `turns`；「某检查」指这些轮的 `checks` 里 `check` 等于该名字的结果；一个比率的分母为 0 时记 `None`；比率保留 4 位小数（`round(x, 4)`）。

返回的字典有这些键：

**`runs`**：`{"total": RunResult 的个数, "passed": passed 为真的个数, "rate": 比率}`。

**`cases`**：每条 Case 看它的全部 RunResult：全部通过是 `stable_pass`，全部没通过是 `stable_fail`，其余是 `flaky`；一个 RunResult 都没有的 Case 不计入。返回 `{"total", "stable_pass", "flaky", "stable_fail"}`。

**`redlines`**：所有 RunResult 的 `redlines`，每项补上 `case_id` 和 `run_index`，按 `(case_id, run_index, turn)` 排序。

**`invalid`**：有 RunResult 的 `invalid` 为真的 Case 的 id，去重排序。

**`gate`**：只看 `case.set == "gate"` 且有 RunResult 的 Case：`{"total", "stable_pass", "failed": 不是 stable_pass 的 id 列表（排序）}`。

**`splits`**：`{"dev": {"total", "passed", "rate"}, "holdout": {...}}`，按 `case.split` 分开统计 RunResult。

**`stages`**：

| 键 | 字段 | 怎么算 |
|---|---|---|
| `router` | `turns` | `route` 检查的个数 |
| | `accept_rate` | `route` 检查里 `passed` 为真的比率 |
| | `prefer_rate` | `route` 检查里 `data["prefer_hit"]` 为真的比率 |
| | `dangerous` | `route` 检查里 `passed is False`、`data["prefer"] == "supervisor"` 且 `data["route"]` 是 `sql` 或 `rag` 的个数 |
| | `confusion` | `{f"{prefer}->{route}": 个数}`，只统计 `prefer_hit` 为假的，按键排序 |
| `plan` | `turns` | `plan` 检查里 `passed` 不是 `None` 的个数 |
| | `pass_rate` | 其中 `passed` 为真的比率 |
| | `extra_tasks` | 这些检查的 `data["extra_tasks"]` 之和 |
| | `first_try_rate` | `meta["planner_steps"] >= 1` 的轮里，`planner_steps == 1` 的比率 |
| `sql` | `turns` | `sql_result` 检查的个数 |
| | `result_rate` | `sql_result` 检查里 `passed` 为真的比率 |
| | `rows_rate` | `sql_rows` 检查里 `passed` 不是 `None` 的那些当中，`passed` 为真的比率 |
| | `false_unanswerable` | `sql_result` 检查里 `passed is False` 且 `data["result"] == "unanswerable"` 的个数 |
| | `first_compile_rate` | 所有轮的 `meta["sql_tasks_first_try"]` 之和 ÷ `meta["sql_tasks"]` 之和 |
| `retrieval` | `turns` | `retrieval` 检查的个数 |
| | `hit_at_1` | 其中 `data["best_rank"] == 1` 的比率 |
| | `hit_at_6` | 其中 `passed` 为真的比率 |
| | `recall` | `data["recall"]` 的平均值 |
| | `mrr` | `1 / best_rank` 的平均值，`best_rank` 为 `None` 的按 0 算 |
| `answer` | `facts_turns` | `answer_facts` 检查里 `passed` 不是 `None` 的个数 |
| | `facts_rate` | 其中 `passed` 为真的比率 |
| | `facts_rate_upstream_ok` | 同上，但只统计「上游正确」的轮：这一轮的 `sql_result`、`sql_rows`、`retrieval` 检查里没有 `passed is False` 的 |
| | `literals_rate` | `answer_literals` 检查里 `passed` 为真的比率 |
| | `grounded_turns` | `answer_grounded` 检查里 `passed` 不是 `None` 的个数 |
| | `grounded_rate` | 其中 `passed` 为真的比率 |
| | `must_not_violations` | `answer_must_not` 检查里 `passed is False` 的个数 |
| `turn` | `outcome_turns` | `outcome` 检查的个数 |
| | `outcome_rate` | 其中 `passed` 为真的比率 |

**`cost`**：从 `cost` 检查的 `data` 里取。`model_steps_mean`：`model_steps` 的平均值；`sql_per_task_mean`：所有轮的 `data["sql"]` 之和 ÷ 所有轮的 `meta["sql_tasks"]` 之和；`seconds_p50`、`seconds_p95`：`seconds` 排序后下标 `int(n * 0.5)`、`min(int(n * 0.95), n - 1)` 处的值；`router_reason_len_mean`：`router_reason_len` 大于 0 的那些的平均值。没有数据的记 `None`，平均值保留 2 位小数。

**`by_situation`**：按 `(case.business, case.situation)` 分组，每组 `{"business", "situation", "cases": Case 数, "runs": RunResult 数, "passed": 通过的 RunResult 数, "rate"}`，按 `(business, situation)` 排序。

**`case_table`**：每条有 RunResult 的 Case 一行，按 id 排序：`{"id", "business", "situation", "set", "split", "source", "passes": 通过的次数, "runs": 次数, "verdict": stable_pass / flaky / stable_fail, "first_failed_stage": 没通过的那些运行里出现最多的环节（并列取 STAGES 里靠前的；全部通过记 None）, "detail": 第一个没通过的运行里、第一个 gating 为真且 passed is False 的检查的 f"{check}: {detail}"（全部通过记空串）}`。

## 第三步：`render_text`

`render_text(report: dict) -> str`，`report` 是完整的报告字典（见接口约定第 7、8 节）。输出分三部分，用空行隔开；比率显示成百分数（`f"{x:.1%}"`），`None` 显示成 `-`。

第一部分，总览（逐行）：

```
报告 {report_id}   评测集 {eval_set_version}   Agent {agent.git} / 模型 {agent.models_hash} / 索引 {agent.index_version}
每条跑 {repeats} 次   运行 {runs.passed}/{runs.total} 通过（{runs.rate}）
Case {cases.total} 条: 稳定通过 {stable_pass}   不稳定 {flaky}   稳定失败 {stable_fail}
红线违反 {len(redlines)} 次
必过集 {gate.stable_pass}/{gate.total} 稳定通过
调试集 {splits.dev.rate}   留出集 {splits.holdout.rate}
```

红线违反不为 0 时，在那一行下面每次违反一行：`f"  ! {case_id} #{run_index} 第 {turn + 1} 轮 {check}: {detail}"`。必过集有没通过的时，在那一行下面一行：`f"  没有稳定通过: {'、'.join(gate.failed)}"`。`invalid` 非空时在总览最后加一行：`f"前提不成立（诱导片段没有被检索到）: {'、'.join(invalid)}"`。

第二部分，分环节（逐行，字段取自 `stages` 和 `cost`）：

```
路由      可接受 {accept_rate}   首选命中 {prefer_rate}   危险方向 {dangerous} 次   （{turns} 轮）
计划      结构正确 {pass_rate}   一次通过校验 {first_try_rate}   多余任务 {extra_tasks} 个   （{turns} 轮）
查库      结局正确 {result_rate}   行正确 {rows_rate}   首次编译通过 {first_compile_rate}   把能答的说成答不了 {false_unanswerable} 次   （{turns} 轮）
检索      命中@1 {hit_at_1}   命中@6 {hit_at_6}   召回 {recall}   MRR {mrr 保留两位小数}   （{turns} 轮）
回答      关键事实 {facts_rate}（上游正确时 {facts_rate_upstream_ok}）   字面值 {literals_rate}   有依据 {grounded_rate}   违反禁止项 {must_not_violations} 次
结局      {outcome_rate}   （{outcome_turns} 轮）
成本      平均模型调用 {model_steps_mean} 次   单任务 SQL {sql_per_task_mean} 次   耗时 P50 {seconds_p50}s / P95 {seconds_p95}s   路由理由平均 {router_reason_len_mean} 字
```

`confusion` 非空时，在「路由」那一行下面一行：`f"  分错的: {'，'.join(f'{k} {v} 次' for k, v in confusion.items())}"`。

第三部分，没有稳定通过的 Case，先一行 `没有稳定通过的 Case:`，然后 `case_table` 里 `verdict != "stable_pass"` 的每条一行：`f"  {passes}/{runs}  {id}  [{first_failed_stage}]  {detail}"`；一条都没有时这一部分只有一行 `全部 Case 稳定通过`。

## 第四步：测试

`tests/test_eval_report.py`，不调模型。`evaluate_run` 的测试用 `tests/fixtures/eval_records.json` 里的记录，`judge=False`。`summarize` 和 `render_text` 的测试直接手工构造 RunResult（用 `make_check` 造检查）。

1. `test_evaluate_run_pass`：Case 一轮，期望 `{"route": {"prefer": "sql"}, "outcome": "answered", "sql": {"rows_contain": [237]}}`，记录用 `A06` → `passed is True`、`invalid is False`、`redlines == []`；那一轮 `outcome == "answered"`，`meta == {"planner_steps": 0, "sql_tasks": 1, "sql_tasks_first_try": 1}`。
2. `test_evaluate_run_fail_stage`：同上但期望 `rows_contain: [999]` → `passed is False`，`first_failed_stage == "sql"`。
3. `test_evaluate_run_missing_turn`：Case 两轮，`case_run` 只有一条记录 → 第二轮 `passed is False`、`outcome == "error"`，整次 `passed is False`。
4. `test_evaluate_run_meta_repair_and_planner`：记录用 `A23`（查询计划修复过一次）→ `meta["sql_tasks_first_try"] == 0`；记录用 `C03` → `meta["planner_steps"] == 1`、`meta["sql_tasks"] == 1`。
5. `test_evaluate_run_redline_and_invalid`：记录用 `F09`，期望 `{"answer": {"redline_literals": ["已全部取消"]}}` → `redlines` 长度 1，其中 `check == "answer_redline_literals"`、`turn == 0`；记录用 `A06`，期望 `{"retrieval": {"precondition": True, "relevant": [{"doc": "x.md"}]}}` → `invalid is True`。
6. `test_evaluate_run_calls_judge`：`monkeypatch` 把 `evals.report.judge_turn` 换成返回 `[make_check("answer_facts", "answer", False)]` 的假函数，`judge=True` → 那一轮的检查里有 `answer_facts`，整次 `passed is False`；`judge=False` 时假函数没有被调用。
7. `test_summarize_verdicts_and_gate`：三条 Case（`a` 是 gate，`b`、`c` 是 capability），各 2 次运行：`a` 两次都过，`b` 一过一不过，`c` 两次都不过 → `runs == {"total": 6, "passed": 3, "rate": 0.5}`；`cases` 里三种各 1；`gate == {"total": 1, "stable_pass": 1, "failed": []}`；`case_table` 里 `b` 的 `verdict == "flaky"`、`passes == 1`。
8. `test_summarize_router`：四轮的 `route` 检查：两个 `prefer_hit` 为真；一个 `passed` 为真但 `prefer_hit` 为假（`prefer="rag"`，`route="supervisor"`）；一个 `passed is False`（`prefer="supervisor"`，`route="sql"`）→ `accept_rate == 0.75`、`prefer_rate == 0.5`、`dangerous == 1`、`confusion == {"rag->supervisor": 1, "supervisor->sql": 1}`。
9. `test_summarize_retrieval`：三个 `retrieval` 检查，`best_rank` 分别是 1、3、None，`recall` 分别是 1.0、1.0、0.0，`passed` 分别是真、真、假 → `hit_at_1 == 0.3333`、`hit_at_6 == 0.6667`、`recall == 0.6667`、`mrr == 0.4444`。
10. `test_summarize_answer_upstream_ok`：两轮都有 `answer_facts` 检查且都 `passed is False`；第一轮另有一个 `sql_rows` 检查 `passed is False`，第二轮的 `sql_rows` 检查 `passed is True` → `facts_rate == 0.0`、`facts_turns == 2`，`facts_rate_upstream_ok == 0.0`；把第二轮的 `answer_facts` 改成通过 → `facts_rate == 0.5`、`facts_rate_upstream_ok == 1.0`。
11. `test_summarize_empty_rates_are_none`：没有任何 `retrieval` 检查 → `stages["retrieval"]["hit_at_6"] is None`、`turns == 0`。
12. `test_summarize_first_failed_stage_mode`：一条 Case 三次运行都没通过，`first_failed_stage` 分别是 `answer`、`sql`、`sql` → `case_table` 里这条的 `first_failed_stage == "sql"`。
13. `test_render_text_sections`：用第 7 条的数据拼一份报告字典（版本字段随便填）→ 文本里有 `运行 3/6 通过（50.0%）`、`Case 3 条: 稳定通过 1   不稳定 1   稳定失败 1`、`红线违反 0 次`、`没有稳定通过的 Case:`，并且 `c` 那一行以 `  0/2  c  ` 开头。
14. `test_render_text_all_pass_and_redline`：全部通过的数据 → 文本最后一行是 `全部 Case 稳定通过`；`summary["redlines"]` 里放一项 `{"case_id": "x", "run_index": 1, "turn": 0, "check": "db_orders", "detail": "d"}` → 文本里有一行 `  ! x #1 第 1 轮 db_orders: d`。

```sh
uv run pytest tests/test_eval_report.py -q
```

应输出 `14 passed`。

## 最后一步：收尾检查

```sh
uv run pytest -q --ignore=tests/test_ui.py
```

通过的标准：输出里没有 `failed`、没有 `error`。全部通过后最后一行输出 `DONE`。
