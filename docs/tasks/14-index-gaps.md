# 任务 14：索引版本的三处补全和界面的一处显示修正

先读 `AGENTS.md`。本任务修改这些文件：`scripts/index.py`、`app/runner.py`、`cli.py`、`ui.py`、`tests/test_graph.py`、`tests/test_rag_index.py`，并更新 `README.md` 里的一行。不要改别的文件。

## 第一步：`scripts/index.py` 增加 `gc` 子命令

现状：`IndexStore.gc(keep=3)` 已经实现（保留版本号最大的 `keep` 个版本，线上版本和上一个版本永远保留，其余删除），但脚本里没有入口。

改成：增加子命令 `gc`，带可选参数 `--keep`（整数，默认 3）。调用 `store.gc(keep=args.keep)`，然后打印一行：
- 有被删除的版本：`已清理: v1，v2   保留: v3，v4，v5`（两处都用中文逗号 `，` 分隔，「保留」是清理之后 `store.list_versions()` 的结果）。
- 没有被删除的版本：`没有需要清理的版本   保留: v1，v2`。

把打印这一行的逻辑写成函数 `_gc(store: IndexStore, keep: int) -> int`，返回 0。

## 第二步：`scripts/index.py` 的 `status` 不把上一个版本算作「已构建未发布」

现状：`_status` 里「已构建未发布」是所有版本号大于线上版本的版本。回滚之后（例如线上 v1、上一个 v5），v5 会被列进「已构建未发布」，但它是发布过的。

改成：「已构建未发布」= 版本号大于线上版本、并且不等于指针里 `previous` 的版本。没有指针时仍然是全部版本。

## 第三步：可以指定一个未发布的版本来查询

现状：`app/runner.py` 的 `make_context` 总是用指针里的线上版本。发布前想先在新版本上试一下（或跑评测）做不到。

改成：
- `app/runner.py`：`make_context` 增加最后一个参数 `index_version: str | None = None`。给了就用它，没给才读指针。读指针的那行改成只在没给的时候执行：

```python
def make_context(customer_id: str | None, today: str | None = None, fault: str = "off",
                 session_id: str = "", index_version: str | None = None) -> AppContext:
    if index_version is None:
        pointer = IndexStore().read_pointer()
        index_version = pointer["current"] if pointer else None
    return AppContext(
        customer_id=customer_id or None,
        today=today or config.DEFAULT_TODAY,
        fault=fault,
        index_version=index_version,
        session_id=session_id,
    )
```

- `cli.py`：增加参数 `--index-version`（默认 `None`，帮助文字「指定知识库的版本，可以是还没发布的版本；不给就用线上版本」）。`main` 里两处 `make_context(...)` 调用都加上 `index_version=args.index_version`。
- `tests/test_graph.py`：已有的测试 `test_cli_interactive_rebuilds_context_each_turn` 里那个假的 `make_context` 的参数表加上 `index_version=None`，否则它接不住新的关键字参数。
- `README.md`：「命令行参数：」那一行里，在 `--verbose（打印每一步的完整输出）` 前面加上 `` `--index-version v2`（指定知识库版本，可以是未发布的）、``。

## 第四步：界面的步骤标题不把反引号渲染成代码

现状：`ui.py` 的 `step_title` 返回的标题会被 Streamlit 当作 Markdown 渲染，摘要里的反引号（例如 SQL 里的 `` `orders.status` ``）变成了代码样式，标题看起来是断开的。

改成：`step_title` 里取到摘要的第一行之后、截断之前，把其中的反引号字符全部去掉（`first = first.replace("`", "")`）。展开后的 `st.code(step["summary"], ...)` 不变，完整的 SQL 仍然原样显示。

## 第五步：测试

在 `tests/test_rag_index.py` 末尾追加 2 个测试。这个文件里已有的测试用假的 `embed_fn` 和 `tmp_path`，照它们的写法准备索引。脚本里的函数这样导入（`scripts` 不是包）：

```python
import importlib.util
from pathlib import Path

def load_index_script():
    path = Path(__file__).resolve().parent.parent / "scripts" / "index.py"
    spec = importlib.util.spec_from_file_location("index_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
```

1. `test_status_excludes_previous_version`：在 `tmp_path` 下构建 v1、v2、v3，依次发布 v1 和 v3，再 `rollback()`（此时线上 v1、上一个 v3）。调用脚本模块的 `_status(store)`，用 `capsys` 读到的输出是 `线上: v1    上一个: v3    已构建未发布: v2`。
2. `test_gc_command_prints_removed_and_kept`：构建 v1 到 v5，依次发布 v1 到 v5（此时线上 v5、上一个 v4）。调用脚本模块的 `_gc(store, 2)` 返回 0，输出是 `已清理: v1，v2，v3   保留: v4，v5`；再调用一次，输出是 `没有需要清理的版本   保留: v4，v5`。

在 `tests/test_graph.py` 末尾追加 2 个测试：

3. `test_make_context_uses_given_index_version`：`monkeypatch.setattr("app.runner.IndexStore", ...)` 换成一个假类，它的 `read_pointer()` 返回 `{"current": "v7", "previous": None}` 并记录被调用的次数。`make_context("CUST-001").index_version == "v7"`，此时 `read_pointer` 被调用了 1 次；`make_context("CUST-001", index_version="v9").index_version == "v9"`，`read_pointer` 的调用次数没有增加。
4. `test_cli_passes_index_version`：`monkeypatch` 把 `cli.make_context` 换成记录收到的关键字参数、并返回 `AppContext(customer_id="CUST-002", index_version="v9", session_id="s-graph")` 的函数；路由队列放 `direct`，回答队列放一个文本；调用 `cli.main(["--index-version", "v9", "你好"])` 返回 0，假函数收到的 `index_version` 是 `"v9"`。

```sh
uv run pytest tests/test_rag_index.py tests/test_graph.py -q
```

应输出 `50 passed`。

## 最后一步：收尾检查

```sh
uv run pytest -q
uv run python scripts/index.py status
uv run python scripts/index.py gc
uv run python cli.py --customer CUST-001 --index-version v1 "退货政策是什么？"
grep -c 'first.replace("`", "")' ui.py
```

通过的标准：
- `uv run pytest -q` 的输出里没有 `failed`、没有 `error`。
- `status` 输出 `线上: v1    上一个: 无    已构建未发布: 无`。
- `gc` 输出 `没有需要清理的版本   保留: v1`。
- 命令行的第一行里有 `索引版本: v1`，并且有一行以 `[t1/retrieve]` 开头、内容是 `索引版本 v1，命中 6 个片段`。
- 最后一条命令输出 `1`。

全部通过后最后一行输出 `DONE`。
