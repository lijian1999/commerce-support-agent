# 任务 15：知识库索引的目录可以按请求指定

先读 `AGENTS.md`。本任务修改这些文件：`app/runner.py`、`app/rag/retrieval.py`、`tests/test_retrieval.py`、`tests/test_graph.py`。不要改别的文件。

## 背景

`AppContext`（`app/context.py`，已改好，只读）多了一个字段 `index_dir: str | None = None`：知识库索引所在的目录，`None` 表示用默认目录。现在要让它生效：建 Context 时能指定，检索时用它。默认行为（不指定）不变。

## 第一步：`app/runner.py` 的 `make_context`

- 签名末尾加一个参数：`index_dir: str | None = None`。
- 需要读指针时（`index_version is None`）：`index_dir` 非空就用 `IndexStore(Path(index_dir))`，否则用 `IndexStore()`。
- 返回的 `AppContext` 带上 `index_dir=index_dir`。

## 第二步：`app/rag/retrieval.py`

- `RetrievalState` 加一个字段：`index_dir: str | None`（用哪个目录的索引，`None` 表示默认目录）。
- `retrieve` 里建索引对象的那一行改成：`state.get("index_dir")` 非空就用 `IndexStore(Path(state["index_dir"]))`，否则用 `IndexStore()`。
- `rag_agent_node` 调用检索子图时，输入里加上 `"index_dir": runtime.context.index_dir`。

## 第三步：测试

`tests/test_retrieval.py` 里的假 `IndexStore` 现在不接受参数。把它的 `__init__` 改成接受一个可选的目录参数，并把收到的值记到它已有的那个记录字典里，键名 `index_dir`（没传时记 `None`）。然后追加 2 个测试：

1. `test_retrieve_uses_default_index_dir`：Context 不带 `index_dir` 时调用 `rag_agent_node` → 记录里的 `index_dir` 是 `None`，结果 `status` 是 `ok`。
2. `test_retrieve_uses_given_index_dir`：Context 带 `index_dir="/tmp/some-index"` 时调用 `rag_agent_node` → 记录里的 `index_dir` 等于 `Path("/tmp/some-index")`。

`tests/test_graph.py` 末尾追加 1 个测试：

3. `test_make_context_passes_index_dir`：用 `monkeypatch` 把 `app.runner.IndexStore` 换成一个假类（`__init__` 接受一个可选参数并把它记下来，`read_pointer` 返回 `{"current": "v5"}`）→ `make_context("CUST-001", index_dir="/tmp/x")` 返回的 Context 里 `index_dir == "/tmp/x"`、`index_version == "v5"`，假类收到的参数等于 `Path("/tmp/x")`；`make_context("CUST-001")` 返回的 Context 里 `index_dir is None`。

```sh
uv run pytest tests/test_retrieval.py tests/test_graph.py -q
```

应输出 `43 passed`。

## 最后一步：收尾检查

```sh
uv run pytest -q --ignore=tests/test_ui.py
uv run python cli.py "标准配送要几天"
```

通过的标准：
- 第一条命令的输出里没有 `failed`、没有 `error`，通过的数量是 `258`。
- 第二条命令有一行以 `[t1/retrieve]` 开头，里面有 `命中 6 个片段`。

全部通过后最后一行输出 `DONE`。
