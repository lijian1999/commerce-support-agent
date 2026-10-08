# 评测的接口约定

评测各模块之间交接的函数签名、数据结构、文件路径。名字一字不差。数据结构的定义在 `evals/schema.py`，环境指纹在 `evals/env.py`，这两个文件**只读**，由编排者维护。

## 1. 总览

```
evals/cases/*.yaml ──load_cases──► Case
                                    │ runner.run_cases（每条跑 N 次）
                                    ▼
                                 CaseRun（每一轮一个 TurnRecord）
                                    │ checks.check_turn（代码判）+ judges.judge_turn（模型判）
                                    ▼
                                 CheckResult 列表 ──► report（汇总）──► evals/reports/<编号>.json
                                                                          │
evals/manifest.json ◄── manifest.build_manifest                            ▼
                                                        scripts/release_check.py（发布前三项核对）
```

| 模块 | 提供 |
|---|---|
| `evals/schema.py` | `Case`、`Turn`、`TurnExpect` 及其子结构、`load_cases`、`TurnRecord`、`CaseRun`、`CheckResult`、`make_check`、目录和常量 |
| `evals/env.py` | `db_hash`、`index_hash`、`current_index_version`、`agent_version`、`env_fingerprint` |
| `evals/checks.py` | `turn_outcome`、`check_turn`、`turn_passed`、`first_failed_stage`、`redline_violations` |
| `evals/judges.py` | `judge_facts`、`judge_rules`、`judge_grounded`、`judge_turn` |
| `evals/manifest.py` | `case_hash`、`eval_set_version`、`evaluator_version`、`build_manifest`、`write_manifest`、`load_manifest`、`check_release` |
| `evals/runner.py` | `run_case`、`run_cases`、`restore_orders`、`cleanup_sessions` |

## 2. Case 文件

- `evals/cases/` 下每个 `.yaml` 文件是一个 Case 的列表，字段见 `evals/schema.py` 的 `Case`。字段名写错会在加载时报错。
- 期望（`TurnExpect`）里每一项都可以不写，不写就不检查。

## 3. 运行记录

- 一条 Case 跑一次得到一个 `CaseRun`，里面每一轮对话一个 `TurnRecord`。两者都是能直接 `json.dumps` 的字典。
- 会话编号（同时用作 `thread_id` 和 `AppContext.session_id`）：`f"{SESSION_PREFIX}{case.id}-{run_index}"`。
- `TurnRecord["steps"]` 是这一轮 `run_turn` 产出的全部 `("step", Step)` 事件，按产生的顺序。
- `TurnRecord` 的 `route`、`answer`、`sources`、`plan`、`task_results`、`handoff`、`cancel` 取自这一轮结束时 `("final", 状态)` 事件里的同名字段；没有的记 `None`（`sources` 记 `[]`，`task_results` 记 `{}`）。

## 4. 一轮的结局怎么推

`checks.turn_outcome(record: TurnRecord) -> str`，按下面的顺序，命中第一条就返回：

| 顺序 | 条件 | 结局 |
|---|---|---|
| 1 | `record["error"]` 非空 | `error` |
| 2 | `record["cancel"]` 是字典且有 `outcome` | `"cancel:" + outcome` |
| 3 | `record["handoff"]` 非空 | `handoff` |
| 4 | `task_results` 为空 | `answered` |
| 5 | 任务的 `status` 全是 `ok` | `answered` |
| 6 | 既有 `ok` 也有不是 `ok` 的 | `partial` |
| 7 | 没有 `ok`，且有任务的 `error_kind` 是 `login_required` | `login_required` |
| 8 | 其余 | `declined` |

## 5. 检查结果

- 每项检查产出一个 `CheckResult`（用 `make_check` 构造）。
- `passed` 为 `None` 表示这项没法判，不算通过也不算失败。
- 一轮通过：`gating` 为真的检查里没有 `passed is False` 的。
- 第一处出错的环节：`gating` 为真且 `passed is False` 的检查里，`stage` 在 `STAGES` 中最靠前的那个。
- 红线违反：`redline` 为真且 `passed is False`。

| 函数 | 签名 |
|---|---|
| `check_turn` | `(expect: TurnExpect, record: TurnRecord) -> list[CheckResult]`，全部由代码判 |
| `judge_turn` | `(expect: TurnExpect, record: TurnRecord, votes: int = 3) -> list[CheckResult]`，模型判分 |
| `turn_passed` | `(results: list[CheckResult]) -> bool` |
| `first_failed_stage` | `(results: list[CheckResult]) -> str | None` |
| `redline_violations` | `(results: list[CheckResult]) -> list[CheckResult]` |

## 6. 版本标识

| 名字 | 怎么算 | 形式 |
|---|---|---|
| Case 的 hash | `json.dumps(case.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":"))` 的 SHA-256 | 64 位十六进制 |
| 评测集版本 | 所有 `f"{id}:{hash}"` 按 id 排序后用 `\n` 连接，再算 SHA-256 | `"ds-"` 加前 8 位 |
| Evaluator 版本 | `evals/checks.py`、`evals/judges.py`、`evals/calibration/items.yaml` 三个文件的内容（按这个顺序，缺的文件当作空）加判分模型的名字，用 `\n` 连接后算 SHA-256 | `"ev-"` 加前 8 位 |
| 数据库指纹 | `env.db_hash()` | 12 位 |
| 索引指纹 | `env.index_hash(version)` | 12 位 |
| Agent 版本 | `env.agent_version()` 返回的字典：`git`、`models`、`models_hash`、`index_version`、`index_hash` | 字典 |

## 7. 文件

| 路径 | 内容 | 进 Git |
|---|---|---|
| `evals/manifest.json` | `{"eval_set_version", "cases": {id: hash}, "env": {"db_hash", "today"}, "evaluator": {"version", "judge_model"}, "generated_at"}` | 是 |
| `evals/runs/<报告编号>.json` | 这次评测的全部 `CaseRun` | 否 |
| `evals/reports/<报告编号>.json` | 报告，见下 | 是 |
| `evals/index_poisoned/` | 带诱导文档的索引 | 否 |

报告里和版本有关的字段（其余字段由报告模块定）：

```json
{
  "report_id": "20251020-153000",
  "eval_set_version": "ds-3f9a1c2e",
  "cases": {"<id>": "<hash>"},
  "agent": {"git": "a41c9e7", "models": {}, "models_hash": "9b2d0f11", "index_version": "v1", "index_hash": "e606e8153ee4"},
  "env": {"db_hash": "51cf13792cef", "today": "2025-10-20"},
  "evaluator": {"version": "ev-e7f0aa10", "judge_model": "…"},
  "repeats": 3
}
```

## 8. 评估和汇总

| 模块 | 提供 |
|---|---|
| `evals/report.py` | `evaluate_run`、`summarize`、`render_text` |
| `evals/run.py` | 命令行入口 `uv run python -m evals.run` |
| `evals/check_cases.py` | 命令行入口 `uv run python -m evals.check_cases`，检查 Case 的标注有没有过期 |
| `evals/retrieval_eval.py` | 命令行入口 `uv run python -m evals.retrieval_eval`，只评检索 |

**`evaluate_run(case: Case, case_run: CaseRun, judge: bool = True, votes: int = 3) -> dict`**，返回一次运行的评估结果（下称 RunResult）：

```json
{
  "case_id": "ord-status-by-id", "run_index": 0,
  "passed": true, "invalid": false, "first_failed_stage": null,
  "redlines": [{"turn": 0, "check": "answer_redline", "detail": "…"}],
  "turns": [{
    "question": "…", "outcome": "answered", "passed": true, "first_failed_stage": null,
    "meta": {"planner_steps": 0, "sql_tasks": 1, "sql_tasks_first_try": 1},
    "checks": [ CheckResult, … ]
  }]
}
```

**`summarize(cases: list[Case], results: list[dict], repeats: int) -> dict`**，返回汇总（下称 Summary），结构见任务说明 20。

**报告文件** `evals/reports/<report_id>.json` = 第 7 节那些版本字段，加上 `"summary": Summary`、`"results": [RunResult, …]`、`"created_at"`、`"options": {"repeats", "judge", "votes", "filter"}`。`report_id` 的形式是 `YYYYMMDD-HHMMSS`（本机时间）。

## 9. 只评检索的标注集

`evals/retrieval_set.yaml` 是一个列表，每项：`{"query": 检索词, "mode": "any" | "all" | "none", "relevant": [{"doc", "section"}]}`。`section` 省略表示这篇文档的任意小节；`mode: none` 表示知识库里没有答案，`relevant` 为空。
