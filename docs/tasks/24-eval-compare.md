# 任务 24：对比两份评测报告

先读 `AGENTS.md`，再读接口约定 `docs/eval-contracts.md`（第 7、8 节）。本任务新建这些文件：`evals/compare.py`、`tests/test_eval_compare.py`。不要改别的文件。

## 背景

改了提示词或代码之后重跑评测，要回答「相对上一份报告是变好了还是变坏了」。模型的输出不完全稳定，所以比较有三条规矩：只在两份报告里内容 hash 相同的 Case 上比；一条 Case 的通过次数小幅变动不下结论；总通过率差得不多也不下结论。

报告的结构见接口约定：`report["cases"]` 是 `{id: hash}`，`report["summary"]["case_table"]` 每条 Case 一行（含 `id`、`passes`、`runs`、`first_failed_stage`、`detail`），`report["summary"]["redlines"]`、`report["summary"]["gate"]["failed"]`、`report["summary"]["stages"]`。

## 第一步：`compare_reports`

`compare_reports(old: dict, new: dict, noise: float = 0.05) -> dict`，纯函数。

- `common`：两份报告的 `cases` 里 id 相同且 hash 相同的 id；`changed`：id 相同但 hash 不同的；`only_old`、`only_new`：只在一边的。四个列表都排序。
- 对 `common` 里的每条 Case，从两边的 `case_table` 取 `passes` 和 `runs`（哪一边的 `case_table` 里没有这条，就不参与下面的分类，也不计入通过率），算通过率 `passes / runs`，分类：
  - `regressed`：旧的通过率是 1.0，新的 ≤ 1/3。
  - `improved`：旧的 ≤ 1/3，新的是 1.0。
  - `same`：两边 `passes` 和 `runs` 都相同。
  - `wobble`：其余（有变化，但不够下结论）。
- 返回：

```python
{
  "old": {"report_id", "eval_set_version", "git": old["agent"]["git"]},
  "new": {同样三项},
  "common": [...], "changed": [...], "only_old": [...], "only_new": [...],
  "regressed": [{"id", "old": "3/3", "new": "0/3", "first_failed_stage": 新报告里的, "detail": 新报告里的}, ...],   # 按 id 排序
  "improved": [{"id", "old": "0/3", "new": "3/3"}, ...],
  "wobble": [{"id", "old": "3/3", "new": "2/3"}, ...],
  "same": 个数,
  "rate_old": 参与分类的 Case 在旧报告里的 passes 之和 ÷ runs 之和（保留 4 位小数，没有可比的 Case 时是 None）,
  "rate_new": 同上，新报告,
  "rate_delta": rate_new - rate_old（保留 4 位小数，任一个是 None 时是 None）,
  "rate_conclusive": rate_delta 不是 None 且 abs(rate_delta) >= noise,
  "redlines_old": len(old["summary"]["redlines"]), "redlines_new": len(new["summary"]["redlines"]),
  "gate_failed_new": new["summary"]["gate"]["failed"],
  "stages": [{"stage": "router", "metric": "accept_rate", "old": 数, "new": 数}, ...],
  "release_ok": 见下,
  "release_problems": [...],
}
```

- `stages`：按这个固定的顺序列出这些指标，两边都从 `summary["stages"]` 里取（取不到记 `None`）：`("router", "accept_rate")`、`("router", "prefer_rate")`、`("plan", "pass_rate")`、`("sql", "result_rate")`、`("sql", "rows_rate")`、`("retrieval", "hit_at_6")`、`("retrieval", "mrr")`、`("answer", "facts_rate")`、`("answer", "facts_rate_upstream_ok")`、`("answer", "grounded_rate")`、`("turn", "outcome_rate")`。
- `release_problems`（按这个顺序，满足就加一条），`release_ok` 是它为空：
  - `redlines_new > 0`：`f"红线违反 {redlines_new} 次"`
  - `gate_failed_new` 非空：`f"必过集有 {个数} 条没有稳定通过"`
  - `regressed` 非空：`f"有 {个数} 条 Case 回退"`
  - `rate_conclusive` 为真且 `rate_delta < 0`：`f"通过率下降 {abs(rate_delta):.1%}"`

## 第二步：`render_compare` 和命令行

`render_compare(result: dict) -> str`，逐行（比率显示成 `f"{x:.1%}"`，`None` 显示成 `-`）：

```
旧: {old.report_id}  {old.eval_set_version}  {old.git}
新: {new.report_id}  {new.eval_set_version}  {new.git}
可比的 Case {len(common)} 条（内容相同）；改过的 {len(changed)} 条、只在旧报告里的 {len(only_old)} 条、只在新报告里的 {len(only_new)} 条不参与比较
通过率 {rate_old} → {rate_new}（{rate_delta 带正负号的百分数，如 +3.2%}，{'超出' if rate_conclusive else '没有超出'}噪声范围）
红线违反 {redlines_old} → {redlines_new} 次
回退 {len(regressed)} 条   变好 {len(improved)} 条   小幅波动 {len(wobble)} 条   没变 {same} 条
```

然后，`regressed` 非空时一行 `回退:`，每条一行 `f"  {id}  {old} → {new}  [{first_failed_stage}]  {detail}"`；`improved` 非空时一行 `变好:`，每条一行 `f"  {id}  {old} → {new}"`；`wobble` 非空时一行 `小幅波动（不下结论）:`，每条一行同上。

然后一行 `分环节:`，`stages` 里两边不都是 `None` 的每项一行 `f"  {stage}.{metric}  {old} → {new}"`（`mrr` 显示成两位小数，其余显示成百分数）。

最后一行：`release_ok` 为真是 `发布门槛: 通过`，否则是 `f"发布门槛: 不通过（{'；'.join(release_problems)}）"`。

命令行 `uv run python -m evals.compare <旧报告路径> <新报告路径>`：读两个 JSON，打印 `render_compare(compare_reports(old, new))`。`release_ok` 为真退出码 0，否则 1；文件不存在时打印 `找不到报告: <路径>`，退出码 2。

## 第三步：测试

`tests/test_eval_compare.py`。辅助 `make_report(report_id, rows, hashes=None, redlines=0, gate_failed=None, stages=None)`：`rows` 是 `{id: (passes, runs)}`；返回 `{"report_id": report_id, "eval_set_version": "ds-" + report_id, "agent": {"git": "g-" + report_id}, "cases": hashes 或 {id: "h"}, "summary": {"case_table": [{"id", "passes", "runs", "first_failed_stage": None 或（没全过时）"sql", "detail": "" 或（没全过时）"sql_rows: x"}], "redlines": [{}] * redlines, "gate": {"failed": gate_failed 或 []}, "stages": stages 或 {}}}`。

1. `test_classify`：旧 `{"a": (3,3), "b": (3,3), "c": (0,3), "d": (3,3), "e": (1,3), "f": (2,3)}`，新 `{"a": (3,3), "b": (1,3), "c": (3,3), "d": (2,3), "e": (3,3), "f": (0,3)}` → `regressed` 的 id 是 `["b"]`（它的 `old == "3/3"`、`new == "1/3"`、`first_failed_stage == "sql"`、`detail == "sql_rows: x"`）；`improved` 的 id 是 `["c", "e"]`；`wobble` 的 id 是 `["d", "f"]`；`same == 1`。
2. `test_only_same_hash_compared`：旧的 `hashes={"a": "h1", "b": "h1", "x": "h"}`，新的 `hashes={"a": "h1", "b": "h2", "y": "h"}`，两边 `a`、`b` 都在 `rows` 里 → `common == ["a"]`、`changed == ["b"]`、`only_old == ["x"]`、`only_new == ["y"]`；`b` 不出现在任何分类里。
3. `test_rate_and_noise`：旧 `{"a": (3,3), "b": (3,3)}`，新 `{"a": (3,3), "b": (2,3)}` → `rate_old == 1.0`、`rate_new == 0.8333`、`rate_delta == -0.1667`、`rate_conclusive is True`；`noise=0.2` 时 `rate_conclusive is False`。
4. `test_no_common_cases`：两份报告的 Case 完全不同 → `rate_old is None`、`rate_delta is None`、`rate_conclusive is False`、`release_ok is True`。
5. `test_release_problems_order`：新报告 `redlines=2`、`gate_failed=["g1"]`，并且有一条回退、通过率明显下降 → `release_ok is False`，`release_problems` 依次以 `红线违反 2 次`、`必过集有 1 条没有稳定通过`、`有 1 条 Case 回退`、`通过率下降` 开头。
6. `test_stages_fixed_order`：旧的 `stages={"router": {"accept_rate": 0.9}}`，新的 `stages={"router": {"accept_rate": 1.0}, "retrieval": {"mrr": 0.8}}` → `stages` 长度 11，第一项是 `{"stage": "router", "metric": "accept_rate", "old": 0.9, "new": 1.0}`，`("retrieval", "mrr")` 那项的 `old is None`、`new == 0.8`。
7. `test_render`：用第 1 条的数据 → 文本里有 `回退 1 条   变好 2 条   小幅波动 2 条   没变 1 条`、一行 `  b  3/3 → 1/3  [sql]  sql_rows: x`、`小幅波动（不下结论）:`，最后一行以 `发布门槛: 不通过（有 1 条 Case 回退` 开头；用两份完全相同的报告 → 最后一行是 `发布门槛: 通过`，文本里没有 `回退:` 这一行。
8. `test_cli`：把两份报告写到 `tmp_path`，调用 `evals.compare.main([旧路径, 新路径])`：相同的两份返回 0；有回退的返回 1；路径不存在返回 2 并打印 `找不到报告: `。

```sh
uv run pytest tests/test_eval_compare.py -q
```

应输出 `8 passed`。

## 最后一步：收尾检查

```sh
uv run pytest -q --ignore=tests/test_ui.py
```

通过的标准：输出里没有 `failed`、没有 `error`。全部通过后最后一行输出 `DONE`。
