# 任务 21：检查 Case 的标注有没有过期；只评检索

先读 `AGENTS.md`，再读接口约定 `docs/eval-contracts.md`（重点是第 2、9 节）和数据结构 `evals/schema.py`。本任务新建这些文件：`evals/check_cases.py`、`evals/retrieval_eval.py`、`tests/test_eval_aux.py`。不要改别的文件。

## 背景

- Case 里的期望值是对着数据库和文档的真值标的。数据或文档变了以后，旧的标注可能不再成立。`check_cases` 用三样东西发现这种过期：每条查库期望带的参考 SQL、每条关键事实带的文档原文摘录、每条检索期望标的（文档，小节）。
- 检索这一环节没有模型，可以脱离整个 Agent 单独评：给一批标好相关片段的检索词，看相关片段排第几。

## 第一步：`evals/check_cases.py`

三个检查函数，各返回问题说明的列表（没有问题返回空列表）：

- `check_reference_sql(case: Case, run_sql) -> list[str]`：对每一轮里 `expect.sql` 不为空且 `reference_sql` 不为空的期望，调用 `run_sql(reference_sql)` 得到行的列表（每行是字典），然后用和 `evals.checks` 里 `sql_rows` 检查相同的规则核对 `rows_contain`、`rows_exclude`、`row_count`。问题说明的形式：
  - `f"{case.id} 第 {轮次} 轮: 参考 SQL 的结果里没有 {值!r}"`
  - `f"{case.id} 第 {轮次} 轮: 参考 SQL 的结果里出现了不该有的 {值!r}"`
  - `f"{case.id} 第 {轮次} 轮: 参考 SQL 返回 {实际行数} 行，标注是 {row_count} 行"`
  - `run_sql` 抛异常时：`f"{case.id} 第 {轮次} 轮: 参考 SQL 执行失败: {异常}"`
  
  轮次从 1 数。
- `check_quotes(case: Case, corpus: str) -> list[str]`：对每一轮 `expect.answer.facts` 里是 `Fact` 对象且 `quote` 不为空的，`quote` 不在 `corpus` 里就报 `f"{case.id} 第 {轮次} 轮: 文档里找不到原文 {quote!r}"`。
- `check_relevant(case: Case, sections: set[tuple[str, str]]) -> list[str]`：对每一轮 `expect.retrieval.relevant` 的每一项：`section` 不为空时 `(doc, section)` 必须在 `sections` 里；`section` 为空时 `sections` 里必须有这篇 `doc` 的某个小节。不满足就报 `f"{case.id} 第 {轮次} 轮: 索引里没有 {doc}{' § ' + section if section else ''}"`。

真实环境的三个数据源：

- `run_sql`：`connect("root")` 后直接 `cur.execute(sql)`（不传参数列表），返回 `to_jsonable(cur.fetchall())`。
- `corpus`：`config.DOCS_DIR` 和 `evals.schema.POISON_DOCS_DIR` 下所有 `.md` 文件的内容用 `\n` 连起来。
- `sections`：`IndexStore().read_manifest(线上版本)["chunks"]` 里每个片段的 `(doc, section)`；`case.env.index == "poisoned"` 的 Case 改用 `IndexStore(POISONED_INDEX_DIR)` 的线上版本（它没有构建时，这些 Case 的检索标注跳过不查）。

命令行 `uv run python -m evals.check_cases`：对全部 Case 跑三项检查，每个问题打印一行；最后一行是 `f"检查了 {Case 数} 条 Case：参考 SQL {条数} 条，原文摘录 {条数} 条，检索标注 {条数} 条；{问题数} 个问题"`。有问题时退出码 1，否则 0。

```sh
uv run python -m evals.check_cases; echo "exit=$?"
```

应输出的最后两行是 `检查了 146 条 Case：参考 SQL 34 条，原文摘录 53 条，检索标注 58 条；0 个问题` 和 `exit=0`。（带诱导文档的索引还没有构建时，检索标注的条数会少 3 条，也算通过。）

## 第二步：`evals/retrieval_eval.py`

- `evaluate_retrieval(items: list[dict], search, k: int = 6) -> dict`：`items` 是 `evals/retrieval_set.yaml` 的内容；`search(query: str) -> list[dict]` 返回按名次排好的片段列表（每个片段有 `doc`、`section`、`score`），至少取回 10 条。
  - 每项的结果 `{"query", "mode", "rank", "recall", "top_score"}`：
    - `top_score`：第一个片段的 `score`（没有片段记 `None`）。
    - `mode == "none"`：`rank` 和 `recall` 都是 `None`。
    - 每个 `relevant` 的名次：片段列表里第一个 `doc` 相同且（没写 `section` 或 `section` 相同）的位置，从 1 数，找不到是 `None`。
    - `mode == "any"`：`rank` 是有名次的里面最小的（都没有是 `None`）；`recall` 是 1.0（`rank` 不为空且 ≤ k）或 0.0。
    - `mode == "all"`：`rank` 是各项名次里最大的（有一项找不到就是 `None`）；`recall` 是名次不为空且 ≤ k 的项数 ÷ 总项数。
  - 返回 `{"items": [...], "labelled": 有标注的条数（mode 不是 none）, "hit_at_1", "hit_at_3", "hit_at_k", "recall", "mrr", "none_top_scores": mode 为 none 的各项的 top_score 列表, "labelled_min_top_score": 有标注的各项里最小的 top_score}`。`hit_at_n` 是有标注的项里 `rank` 不为空且 ≤ n 的比率；`recall` 是它们的 `recall` 的平均；`mrr` 是 `1 / rank` 的平均（`rank` 为空按 0 算）。比率保留 4 位小数；没有有标注的项时这几个都是 `None`。
- 真实环境的 `search`：`IndexStore().query(版本, embed_query(query), 10)`。版本默认用线上版本，命令行可以用 `--index-version` 指定。
- 命令行 `uv run python -m evals.retrieval_eval [--index-version v2]`：每项打印一行 `f"{rank 或 '>10' 或 '-'}  {recall 或 ''}  {top_score:.3f}  {query}"`（`mode` 是 `none` 的 `rank` 位置打印 `-`，找不到的打印 `>10`），最后两行：

```
有标注的 {labelled} 条: 命中@1 {hit_at_1:.2f}  命中@3 {hit_at_3:.2f}  命中@6 {hit_at_k:.2f}  前6召回 {recall:.2f}  MRR {mrr:.2f}
无答案的 {个数} 条最高分 {none_top_scores 各保留 3 位}，有答案的最低最高分 {labelled_min_top_score:.3f}
```

```sh
uv run python -m evals.retrieval_eval | tail -2
```

应输出（会调用真实的 Embedding 服务）：

```
有标注的 33 条: 命中@1 0.73  命中@3 0.85  命中@6 0.88  前6召回 0.89  MRR 0.80
无答案的 3 条最高分 [0.527, 0.491, 0.454]，有答案的最低最高分 0.528
```

## 第三步：测试

`tests/test_eval_aux.py`，不连库、不调模型：`run_sql`、`corpus`、`sections`、`search` 都用测试里构造的假数据。Case 用 `Case.model_validate` 构造。

1. `test_reference_sql_ok`：期望 `rows_contain: ["ThinkPad", 237]`、`rows_exclude: ["HP"]`、`row_count: 2`，假的 `run_sql` 返回 `[{"name": "Lenovo ThinkPad X1"}, {"s": 237.0}]` → 没有问题。
2. `test_reference_sql_problems`：同样的期望，假的 `run_sql` 返回 `[{"name": "HP Pavilion"}]` → 恰好 4 个问题（缺 `ThinkPad`、缺 `237`、出现 `HP`、行数不符），每个都以 `c1 第 1 轮: ` 开头。
3. `test_reference_sql_error_and_skip`：`run_sql` 抛 `RuntimeError("bad")` → 一个问题，里面有 `参考 SQL 执行失败`；没有 `reference_sql` 的期望 → 没有问题，`run_sql` 没有被调用。
4. `test_quotes`：两条事实，一条的 `quote` 在 `corpus` 里，一条不在；另有一条纯字符串的事实 → 恰好 1 个问题，里面有那条找不到的原文。
5. `test_relevant`：`sections = {("a.md", "S1")}`；标注 `{doc: a.md, section: S1}`、`{doc: a.md}` 都没有问题；`{doc: a.md, section: S2}` 和 `{doc: b.md}` 各报一个问题，前者的说明以 `索引里没有 a.md § S2` 结尾。
6. `test_retrieval_any_all_none`：假的 `search` 对三个检索词返回固定的片段列表；三项分别是 `any`（相关片段排第 2）、`all`（两个相关片段，一个排第 1，一个找不到）、`none` → 三项的 `rank` 依次是 `2`、`None`、`None`，`recall` 依次是 `1.0`、`0.5`、`None`；`labelled == 2`、`hit_at_1 == 0.0`、`hit_at_3 == 0.5`、`hit_at_k == 0.5`、`recall == 0.75`、`mrr == 0.25`；`none_top_scores` 的长度是 1。
7. `test_retrieval_rank_beyond_k`：`any`，相关片段排第 8，`k=6` → `rank == 8`、`recall == 0.0`、`hit_at_k == 0.0`。
8. `test_retrieval_empty`：`items` 全是 `none` → `labelled == 0`，`hit_at_1 is None`。

```sh
uv run pytest tests/test_eval_aux.py -q
```

应输出 `8 passed`。

## 最后一步：收尾检查

```sh
uv run pytest -q --ignore=tests/test_ui.py
```

通过的标准：输出里没有 `failed`、没有 `error`；第一步和第二步的检查命令各再运行一次，符合那里写的预期。全部通过后最后一行输出 `DONE`。
