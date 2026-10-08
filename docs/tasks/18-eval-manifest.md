# 任务 18：评测集版本、Manifest、发布前一致性校验

先读 `AGENTS.md`，再读接口约定 `docs/eval-contracts.md`（重点是第 6、7 节）和数据结构 `evals/schema.py`。本任务新建这些文件：`evals/manifest.py`、`scripts/eval_manifest.py`、`scripts/release_check.py`、`tests/test_eval_manifest.py`。不要改别的文件。

## 背景

评测报告要能回答「它评的是不是要发布的这个版本、用的是不是当前这套 Case」。做法：评测集、Agent、环境、判分方式各有一个由内容算出来的版本标识；Manifest 把评测集和环境的标识存进 Git；每份报告带着它运行时的全部标识；发布前用一个脚本核对「现在重算的」「Manifest 里的」「报告里的」三者是否一致。

环境和 Agent 的标识由 `evals/env.py`（只读）提供：`env_fingerprint()` 返回 `{"db_hash", "today"}`，`agent_version()` 返回 `{"git", "models", "models_hash", "index_version", "index_hash"}`。这两个函数会连数据库、读索引。

## 第一步：`evals/manifest.py` 里的纯函数

- `case_hash(case: Case) -> str`：见接口约定第 6 节。
- `eval_set_version(hashes: dict[str, str]) -> str`：见接口约定第 6 节。空字典时是 `"ds-"` 加空串的 SHA-256 的前 8 位。
- `diff_cases(old: dict[str, str], new: dict[str, str]) -> dict`：两份 `{id: hash}` 的差异，返回 `{"added": [...], "removed": [...], "modified": [...]}`，三个列表都按 id 排序。`added` 是只在 `new` 里的，`removed` 是只在 `old` 里的，`modified` 是两边都有但 hash 不同的。
- `describe_diff(diff: dict) -> str`：把差异写成一句话。三类都为空时返回 `"没有变化"`；否则把非空的类别按「修改、新增、删除」的顺序用 `，` 连接，每类写成 `f"{名称} {个数} 个（{前 5 个 id 用 、 连接}{' 等' if 超过 5 个 else ''}）"`。例：`"修改 1 个（a），新增 2 个（b、c）"`。
- `evaluator_version(files: list[Path] | None = None, judge_model: str | None = None) -> dict`：见接口约定第 6 节，返回 `{"version": "ev-xxxxxxxx", "judge_model": judge_model}`。`files` 为 `None` 时用约定里的三个文件；`judge_model` 为 `None` 时用 `config.role_target("judge").model`。文件不存在时当作空内容。

```sh
uv run python -c "
from evals.manifest import eval_set_version, diff_cases, describe_diff
print(eval_set_version({}), eval_set_version({'b': '2', 'a': '1'}) == eval_set_version({'a': '1', 'b': '2'}))
d = diff_cases({'a': '1', 'b': '2', 'x': '9'}, {'a': '1', 'b': '3', 'c': '4', 'd': '5'})
print(d); print(describe_diff(d))"
```

应输出三行：

```
ds-e3b0c442 True
{'added': ['c', 'd'], 'removed': ['x'], 'modified': ['b']}
修改 1 个（b），新增 2 个（c、d），删除 1 个（x）
```

## 第二步：Manifest 的生成和读写

- `current_state(cases: list[Case]) -> dict`：现在重算的全部标识，返回 `{"eval_set_version", "cases": {id: hash}, "agent": env.agent_version(), "env": env.env_fingerprint(), "evaluator": evaluator_version()}`。
- `build_manifest(cases: list[Case]) -> dict`：返回接口约定第 7 节里 `evals/manifest.json` 的那个结构。`generated_at` 用 UTC 的 ISO 时间。它不含 `agent`：Manifest 描述的是评测集和环境，不是某个候选版本。
- `write_manifest(manifest: dict, path: Path = MANIFEST_PATH) -> None`：写成 `json.dumps(…, ensure_ascii=False, indent=2, sort_keys=True)` 加一个换行。
- `load_manifest(path: Path = MANIFEST_PATH) -> dict | None`：文件不存在返回 `None`。

`scripts/eval_manifest.py`（开头照 `scripts/build_index.py` 的写法把项目根目录加进 `sys.path`）：读全部 Case → 读旧的 Manifest → 生成并写入新的 → 打印两行：

```
评测集版本 ds-xxxxxxxx，共 N 个 Case
相对上一份 Manifest：<describe_diff 的结果>
```

没有旧 Manifest 时第二行是 `相对上一份 Manifest：之前没有 Manifest`。

## 第三步：`check_release`

`check_release(report: dict, manifest: dict | None, current: dict) -> list[dict]`。纯函数，不连库、不读文件。`report` 是报告（字段见接口约定第 7 节），`current` 是 `current_state` 的返回值。返回恰好三项，依次是下面三行，每项是 `{"name": 名称, "ok": 是否一致, "line": 展示用的一行, "problems": [问题说明]}`，`ok` 等于 `problems` 为空。

**第 1 项，`name="评测集版本"`**

- `line`：`f"本地重算 {current 的版本}   Manifest {manifest 的版本，没有 Manifest 时写 无}   报告 {report 的版本}"`。
- 问题（可以同时有多条，按这个顺序）：
  - 没有 Manifest：`"没有 Manifest，先运行 scripts/eval_manifest.py"`。
  - 有 Manifest，但它的版本和 `current` 的不同：`f"Case 改了但没有重新生成 Manifest：{describe_diff(diff_cases(manifest['cases'], current['cases']))}"`。
  - `report` 的版本和 `current` 的不同：`f"报告是在 {report 的版本} 上跑的，当前评测集是 {current 的版本}：{describe_diff(diff_cases(report['cases'], current['cases']))}"`。

**第 2 项，`name="Agent 版本"`**

- `line`：`f"当前候选 {current.agent.git} / 模型 {current.agent.models_hash} / 索引 {current.agent.index_version}（{current.agent.index_hash}）"`。
- 问题（按这个顺序）：
  - `current["agent"]["git"]` 以 `-dirty` 结尾：`"工作区有未提交的修改，不能用于发布"`。
  - `report["agent"]["git"]` 以 `-dirty` 结尾：`"报告是在有未提交修改的代码上跑的"`。
  - 两边的 `git` 不同：`f"报告是在代码 {report 的 git} 上跑的，当前是 {current 的 git}"`。
  - 两边的 `models_hash` 不同：`f"模型配置不同，不一样的角色：{、连接的角色名，按字母序}"`（比较两边 `models` 字典里值不同或只有一边有的角色）。
  - 两边的 `index_hash` 不同：`f"知识库索引不同：报告用的是 {report 的 index_version}（{report 的 index_hash}），当前是 {current 的 index_version}（{current 的 index_hash}）"`。

**第 3 项，`name="环境"`**

- `line`：`f"数据库 {current.env.db_hash}   日期 {current.env.today}   Evaluator {current.evaluator.version}"`。
- 问题（按这个顺序）：
  - `report["env"]["db_hash"]` 和 `current` 的不同：`f"数据库内容不同：报告是在 {report 的} 上跑的，当前是 {current 的}"`。
  - 有 Manifest 且它的 `env.db_hash` 和 `current` 的不同：`f"数据库内容和 Manifest 记录的不同（Manifest: {manifest 的}，当前: {current 的}），重跑 scripts/import_mysql.py 可以恢复初始数据"`。
  - `report["env"]["today"]` 和 `current` 的不同：`f"Runtime 的日期不同：报告 {report 的}，当前 {current 的}"`。
  - `report["evaluator"]["version"]` 和 `current` 的不同：`f"判分方式变了：报告用的是 {report 的}，当前是 {current 的}"`。

## 第四步：`scripts/release_check.py`

用法：`uv run python scripts/release_check.py --report evals/reports/<编号>.json`。

读报告 → `load_cases()` → `current_state` → `load_manifest()` → `check_release` → 打印三行，每行是 `f"{name}   {line}   {'✓' if ok else '✗'}"`，有问题的在这一行下面逐条打印 `f"    - {问题}"`；最后一行是 `结论: 一致，可以发布` 或 `结论: 不一致，不能发布`。全部一致时退出码 0，否则 1。报告文件不存在时打印 `找不到报告: <路径>`，退出码 2。

## 第五步：测试

`tests/test_eval_manifest.py`，不连数据库、不调模型。先写两个辅助：

- `make_case(case_id, user="你好")`：`Case.model_validate({"id": case_id, "business": "闲聊", "situation": "问候", "turns": [{"user": user}]})`。
- `make_state(cases, git="abc1234", db="dbhash000000")`：手工拼一个和 `current_state` 同结构的字典：`cases` 用 `case_hash` 算，`eval_set_version` 用 `eval_set_version` 算，`agent` 是 `{"git": git, "models": {"router": "m1", "answer": "m1"}, "models_hash": "mh1", "index_version": "v1", "index_hash": "ih1"}`，`env` 是 `{"db_hash": db, "today": "2025-10-20"}`，`evaluator` 是 `{"version": "ev-1", "judge_model": "j"}`。
- 报告就用 `{"report_id": "r1", "repeats": 3, **make_state(...)}`；Manifest 用 `{"eval_set_version": …, "cases": …, "env": …, "evaluator": …, "generated_at": "t"}`（从同一个 state 里取）。

测试：

1. `test_case_hash_is_stable_and_sensitive`：同一个 Case 算两次相同，长度 64；`user` 不同的两个 Case 不同。
2. `test_case_hash_ignores_yaml_key_order`：用两个键顺序不同、内容相同的字典各 `model_validate` 一次，hash 相同。
3. `test_eval_set_version_format_and_order`：结果匹配正则 `^ds-[0-9a-f]{8}$`；字典的插入顺序不影响结果；改一个 hash 结果就变。
4. `test_diff_and_describe`：第一步检查命令里的那组数据，结果和那里的预期一致；两份相同时 `describe_diff` 返回 `"没有变化"`；一类里有 7 个 id 时，这一类写成前 5 个加 ` 等`。
5. `test_evaluator_version_follows_file_content`：在 `tmp_path` 里建两个文件，`evaluator_version(files=[…], judge_model="j")` 的 `version` 匹配 `^ev-[0-9a-f]{8}$`；改其中一个文件的内容后变化；换 `judge_model` 后变化；列表里有不存在的文件时不报错。
6. `test_manifest_roundtrip`：`write_manifest` 到 `tmp_path` 再 `load_manifest`，内容相同；路径不存在时 `load_manifest` 返回 `None`。
7. `test_release_all_consistent`：报告、Manifest、current 来自同一个 state → 三项的 `ok` 都为真，`name` 依次是 `评测集版本`、`Agent 版本`、`环境`。
8. `test_release_case_modified_after_report`：报告和 Manifest 用 `[make_case("a"), make_case("b")]`，current 用 `[make_case("a"), make_case("b", user="改了")]` → 第 1 项不一致，有两条问题：第一条以 `Case 改了但没有重新生成 Manifest：修改 1 个（b）` 开头，第二条里有 `报告是在` 和 `修改 1 个（b）`。
9. `test_release_missing_manifest`：`manifest=None` → 第 1 项的第一条问题是 `没有 Manifest，先运行 scripts/eval_manifest.py`，`line` 里有 `Manifest 无`；第 3 项不因为没有 Manifest 而多出问题。
10. `test_release_dirty_worktree`：current 的 `git` 是 `abc1234-dirty`，报告的是 `abc1234` → 第 2 项的问题依次是 `工作区有未提交的修改，不能用于发布` 和以 `报告是在代码 abc1234 上跑的` 开头的一条。
11. `test_release_model_changed`：报告的 `agent.models` 是 `{"router": "m1", "answer": "m1"}`、`models_hash` 是 `mh1`；current 的是 `{"router": "m2", "answer": "m1"}`、`mh2` → 第 2 项有一条问题等于 `模型配置不同，不一样的角色：router`。
12. `test_release_index_changed`：两边 `index_hash` 不同 → 第 2 项有一条以 `知识库索引不同` 开头的问题。
13. `test_release_env_changed`：报告的 `db_hash` 和 current 不同、Manifest 的和 current 相同，报告的 `evaluator.version` 是 `ev-0` → 第 3 项恰好两条问题，第一条以 `数据库内容不同` 开头，第二条以 `判分方式变了` 开头。

```sh
uv run pytest tests/test_eval_manifest.py -q
```

应输出 `13 passed`。

## 最后一步：收尾检查

```sh
uv run pytest -q --ignore=tests/test_ui.py
uv run python scripts/eval_manifest.py
uv run python scripts/release_check.py --report evals/reports/does-not-exist.json; echo "exit=$?"
```

通过的标准：
- 第一条命令的输出里没有 `failed`、没有 `error`。
- 第二条命令打印两行，第一行以 `评测集版本 ds-` 开头，第二行以 `相对上一份 Manifest：` 开头。
- 第三条命令打印 `找不到报告: evals/reports/does-not-exist.json` 和 `exit=2`。

全部通过后最后一行输出 `DONE`。
