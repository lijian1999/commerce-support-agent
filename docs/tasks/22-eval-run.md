# 任务 22：评测的命令行入口

先读 `AGENTS.md`，再读接口约定 `docs/eval-contracts.md`（第 7、8 节）和数据结构 `evals/schema.py`。本任务新建这些文件：`evals/run.py`、`tests/test_eval_run.py`。不要改别的文件。

## 背景

评测的各个部件已经有了，本任务把它们串成一条命令：

| 部件 | 用到的函数 |
|---|---|
| `evals/schema.py` | `load_cases`、`REPORTS_DIR`、`RUNS_DIR` |
| `evals/manifest.py` | `case_hash`、`eval_set_version`、`current_state`、`load_manifest` |
| `evals/poisoned_index.py` | `build_poisoned_index()` |
| `evals/runner.py` | `run_cases(cases, repeats, workers, hashes, progress)` |
| `evals/report.py` | `evaluate_run(case, case_run, judge, votes)`、`summarize(cases, results, repeats)`、`render_text(report)` |

另有一个可选的运行方式：把每条 Case 作为 LangSmith 的一个实验样例来跑。它由 `evals/langsmith_run.py` 提供（另一个任务在写），本任务只负责在命令行里接上它：用到时才导入。

## 第一步：选 Case 和拼报告

- `select_cases(cases: list[Case], patterns: list[str] | None = None, set_name: str | None = None, split: str | None = None, business: str | None = None) -> list[Case]`：四个条件都满足的留下。`patterns` 非空时，`case.id` 要匹配其中至少一个（`fnmatch.fnmatchcase`）；其余三个不为 `None` 时要和 Case 的同名字段相等（`set_name` 对应 `case.set`）。
- `build_report(report_id: str, cases: list[Case], results: list[dict], state: dict, options: dict) -> dict`：
  - `cases` 是这次实际跑的 Case，`state` 是 `current_state(全部 Case)` 的返回值。
  - 返回的字典：`report_id`；`created_at`（本机时间的 ISO 格式，到秒）；`cases` 是这次跑的 Case 的 `{id: case_hash}`；`eval_set_version` 是对这个字典算的 `eval_set_version`；`agent`、`env`、`evaluator` 原样取自 `state`；`repeats` 取自 `options["repeats"]`；`options` 原样放入；`summary` 是 `summarize(cases, results, options["repeats"])`；`results` 原样放入。

## 第二步：`main`

`main(argv: list[str] | None = None) -> int`，文件末尾 `if __name__ == "__main__": raise SystemExit(main())`。

参数：

| 参数 | 含义 | 默认 |
|---|---|---|
| `--repeats N` | 每条 Case 跑几次 | 3 |
| `--workers N` | 并行数 | 4 |
| `--votes N` | 模型判分每项判几次 | 3 |
| `--no-judge` | 不做模型判分 | 做 |
| `--case 模式` | 只跑 id 匹配的 Case，可以写多次 | 全部 |
| `--set gate\|capability`、`--split dev\|holdout`、`--business 名字` | 按字段筛 | 不筛 |
| `--langsmith` | 作为 LangSmith 实验来跑 | 否 |
| `--force` | 数据库不是初始状态也照跑 | 否 |

流程：

1. `all_cases = load_cases()`，`cases = select_cases(...)`。一条都没选中：打印 `没有选中任何 Case`，返回 2。
2. `state = current_state(all_cases)`，`manifest = load_manifest()`。
   - 没有 Manifest：打印 `提示: 还没有 Manifest，先运行 uv run python scripts/eval_manifest.py`，继续。
   - 有 Manifest 且 `manifest["env"]["db_hash"] != state["env"]["db_hash"]`：打印 `数据库不是 Manifest 记录的初始状态（Manifest: …，当前: …），先运行 uv run python scripts/import_mysql.py 恢复，或者加 --force`；没有 `--force` 时返回 2。
3. 选中的 Case 里有 `env.index == "poisoned"` 的：调用 `build_poisoned_index()`，打印 `f"带诱导文档的索引 {version}，指纹 {index_hash}"`。
4. `report_id` 用本机时间 `strftime("%Y%m%d-%H%M%S")`。打印 `f"报告 {report_id}: {len(cases)} 条 Case × {repeats} 次，评测集 {state['eval_set_version']}"`。
5. 跑和评：
   - 没有 `--langsmith`：`runs = run_cases(cases, repeats, workers, hashes, progress)`，`hashes` 是选中的 Case 的 `{id: case_hash}`，`progress` 每次打印一行 `f"[{done}/{total}] {case_id} #{run_index}  {这次运行各轮 seconds 之和:.1f}s"`。然后用 `ThreadPoolExecutor(max_workers=workers)` 对每个 `CaseRun` 调用 `evaluate_run(对应的 Case, case_run, judge, votes)`，结果按 `(case_id, run_index)` 排序。
   - 有 `--langsmith`：`from evals.langsmith_run import execute_langsmith`，`runs, results, info = execute_langsmith(cases, repeats, workers, judge, votes, report_id, state)`，然后打印 `f"LangSmith 实验: {info['experiment']}   {info['url']}"`。
6. `report = build_report(...)`，`options` 是 `{"repeats", "judge", "votes", "workers", "langsmith", "filter": {"case": 模式列表, "set", "split", "business"}}`。
7. 建好 `REPORTS_DIR` 和 `RUNS_DIR`；报告写到 `REPORTS_DIR / f"{report_id}.json"`，全部 `CaseRun` 写到 `RUNS_DIR / f"{report_id}.json"`，都用 `json.dumps(…, ensure_ascii=False, indent=1)`。
8. 打印一个空行、`render_text(report)`、一个空行，再打印 `f"报告: evals/reports/{report_id}.json   原始记录: evals/runs/{report_id}.json"`。
9. 返回值：`summary["redlines"]` 非空或 `summary["gate"]["failed"]` 非空时返回 1，否则 0。

## 第三步：测试

`tests/test_eval_run.py`，不调模型、不连库：用 `monkeypatch` 替换 `evals.run` 里导入的函数（`load_cases`、`current_state`、`load_manifest`、`run_cases`、`evaluate_run`、`build_poisoned_index`），把 `evals.run.REPORTS_DIR` 和 `evals.run.RUNS_DIR` 指到 `tmp_path` 下。`summarize` 和 `render_text` 用真的。

辅助：`make_case(case_id, **kw)` 用 `Case.model_validate({"id": case_id, "business": "闲聊", "situation": "x", "turns": [{"user": "你好"}], **kw})`；假的 `current_state` 返回 `{"eval_set_version": "ds-all", "cases": {}, "agent": {"git": "g", "models": {}, "models_hash": "m", "index_version": "v1", "index_hash": "i"}, "env": {"db_hash": "d1", "today": "2025-10-20"}, "evaluator": {"version": "ev-1", "judge_model": "j"}}`；假的 `run_cases` 对每条 Case 的每次重复返回 `{"case_id", "case_hash", "run_index", "turns": [{"seconds": 1.5}]}` 并调用 `progress`；假的 `evaluate_run` 返回一个通过的 RunResult（`turns` 里放一轮，`checks` 为空列表，`meta` 三项都是 0）。

1. `test_select_cases`：三条 Case（`ord-a` 是 gate/dev，`ord-b` 是 capability/holdout，`kb-c` 是 capability/dev）→ `patterns=["ord-*"]` 选出前两条；`set_name="gate"` 选出 `ord-a`；`split="holdout"` 选出 `ord-b`；`patterns=["kb-*", "ord-a"]` 选出 `ord-a` 和 `kb-c`；不给条件时三条都在。
2. `test_build_report_versions`：两条 Case、一个通过的结果 → 报告的 `cases` 是这两条的 `{id: case_hash}`，`eval_set_version` 等于对它算的版本（不是 `state` 里的 `ds-all`），`agent`、`env`、`evaluator` 和 `state` 里的相同，`repeats` 等于 `options["repeats"]`，`summary["runs"]["total"] == 1`。
3. `test_main_writes_report_and_runs`：两条 Case，`main(["--repeats", "2", "--no-judge"])` → 返回 0；`tmp_path` 下的报告目录和记录目录各有一个 `.json` 文件且文件名相同；报告里 `summary["runs"] == {"total": 4, "passed": 4, "rate": 1.0}`、`options["judge"] is False`；打印的内容里有 `[4/4]`、`运行 4/4 通过（100.0%）` 和以 `报告: evals/reports/` 开头的一行。
4. `test_main_no_case_selected`：`main(["--case", "nothing-*"])` → 返回 2，打印 `没有选中任何 Case`，`run_cases` 没有被调用。
5. `test_main_db_mismatch`：假的 `load_manifest` 返回 `{"env": {"db_hash": "other"}}` → `main([])` 返回 2 且 `run_cases` 没有被调用；`main(["--force"])` 返回 0。
6. `test_main_exit_code_on_gate_failure`：一条 `set: gate` 的 Case，假的 `evaluate_run` 返回 `passed: False` 的结果（那一轮里放一个 `make_check("route", "router", False, "x")`）→ `main([])` 返回 1。
7. `test_main_builds_poisoned_index_only_when_needed`：没有 `poisoned` 的 Case 时 `build_poisoned_index` 没有被调用；有一条 `env: {index: poisoned}` 的 Case 时被调用 1 次（假函数返回 `{"version": "v1", "index_hash": "h"}`）。
8. `test_main_langsmith_path`：往 `sys.modules["evals.langsmith_run"]` 放一个假模块，它的 `execute_langsmith` 记录参数并返回 `([], [一个通过的 RunResult], {"experiment": "e1", "url": "http://x"})` → `main(["--langsmith"])` 打印的内容里有 `LangSmith 实验: e1   http://x`，`run_cases` 没有被调用。测试结束时把假模块从 `sys.modules` 里拿掉。

```sh
uv run pytest tests/test_eval_run.py -q
```

应输出 `8 passed`。

## 最后一步：收尾检查

```sh
uv run pytest -q --ignore=tests/test_ui.py
uv run python -m evals.run --case ord-spend-keyboards --case chat-hello --repeats 1 --no-judge; echo "exit=$?"
```

通过的标准：
- 第一条命令的输出里没有 `failed`、没有 `error`。
- 第二条命令（会调用真实模型）：有两行分别以 `[1/2]`、`[2/2]` 开头；有一行 `每条跑 1 次   运行 2/2 通过（100.0%）`；有一行 `全部 Case 稳定通过`；最后是 `exit=0`。

全部通过后最后一行输出 `DONE`。
