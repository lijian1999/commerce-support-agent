# 任务 07：Retrieval 子图和 `rag_agent_node`

先读 `AGENTS.md`，再读 `docs/contracts.md` 的第 2、7 节，以及 `app/state.py`。本任务新建这些文件：`app/rag/retrieval.py`、`tests/test_retrieval.py`。不要改别的文件。

## 背景

知识库的内容是不可信的（文档里可能夹着诱导模型的话），所以检索被做成一个隔离的子图：

- 子图里没有模型，只有一个节点 `retrieve`：把检索词转成向量，去指定版本的索引里查，返回若干片段。全程是确定性代码。
- 子图的状态很窄：输入只有检索词和索引版本，输出只有片段列表。它拿不到客户 ID，也看不到对话。
- 子图对知识库只读：只调用查询的方法。

主图通过 `rag_agent_node` 调用这个子图。

已有的模块（直接用，不要改）：
- `app.rag.embedding.embed_query(text) -> list[float]`
- `app.rag.index.IndexStore()`：`check_runtime(version, cfg)`（配置不一致时抛 `IndexConfigMismatch`）、`query(version, embedding, top_k)`（返回 `[{"doc", "section", "text", "score"}, ...]`）；`app.rag.index.runtime_config()`。
- `app.config.RAG_TOP_K`。
- `app.state` 里的 `TaskPayload`、`make_step`、`make_task_result`。

## 第一步：Retrieval 子图

```python
class RetrievalState(TypedDict, total=False):
    query: str
    index_version: str
    chunks: list[dict]
    error: str | None

def retrieve(state: RetrievalState) -> dict
def build_retrieval_graph() -> CompiledStateGraph
```

- `retrieve` 的参数**只有 `state` 一个**（不接收 `runtime`，这样它读不到 Runtime Context）。做三件事：
  1. `store = IndexStore()`；`store.check_runtime(state["index_version"], runtime_config())`。
  2. `embedding = embed_query(state["query"])`。
  3. `chunks = store.query(state["index_version"], embedding, config.RAG_TOP_K)`。
  
  成功返回 `{"chunks": chunks, "error": None}`。这三步里抛出任何异常（`Exception`）都返回 `{"chunks": [], "error": f"{type(e).__name__}: {e}"}`。
- `build_retrieval_graph()`：`StateGraph(RetrievalState)`，一个节点 `"retrieve"`，`START → retrieve → END`，`compile()` 后返回。不设 `context_schema`。

```sh
uv run python -c "
import inspect
from app.rag.retrieval import build_retrieval_graph, retrieve
g = build_retrieval_graph().get_graph()
print(sorted(n for n in g.nodes if not n.startswith('__')), list(inspect.signature(retrieve).parameters))"
```

应输出 `['retrieve'] ['state']`。

## 第二步：`rag_agent_node`

```python
def build_query(goal: str, upstream: dict[str, TaskResult]) -> str
def rag_agent_node(payload: TaskPayload, runtime: Runtime[AppContext]) -> dict
```

**`build_query`**：检索词是任务目标，后面接上它所依赖的 `sql` 任务查到的值（这个任务不调模型，所以直接把上游结果拼进检索词）。
- 从 `goal` 开始。
- 按任务编号排序遍历 `upstream`：只看 `status == "ok"` 且 `agent == "sql"` 的结果，取它 `data["rows"]` 的前 5 行，把每行里不是 `None` 的值转成字符串，全部用空格连接；这段文本非空时，用一个空格接在检索词后面。
- 最终结果超过 500 个字符时截断到 500。

**`rag_agent_node`**（`tid` 指 `payload["task"]["id"]`，`task` 指 `payload["task"]`）：
1. `version = runtime.context.index_version`。为 `None` 时返回失败：`make_task_result(task, "failed", error_kind="index_error", error="知识库还没有已发布的版本")`，步骤 `make_step(f"{tid}/retrieve", "code", "知识库还没有已发布的版本", None)`。
2. `query = build_query(task["goal"], payload["upstream"])`。
3. `out = _RETRIEVAL_GRAPH.invoke({"query": query, "index_version": version})`。**不传 `context`**，传给子图的只有这两个字段。`_RETRIEVAL_GRAPH` 是模块级的 `build_retrieval_graph()`。
4. `out["error"]` 有值 → 失败：`error_kind="index_error"`，`error="知识库检索失败"`，步骤摘要 `f"检索失败: {out['error']}"`，`detail` 是 `{"query": query, "index_version": version, "error": out["error"]}`。
5. 否则成功：`data = {"index_version": version, "query": query, "chunks": out["chunks"]}`，结果 `make_task_result(task, "ok", data=data)`，步骤 `make_step(f"{tid}/retrieve", "code", f"索引版本 {version}，命中 {len(chunks)} 个片段", data)`。
6. 返回 `{"task_results": {tid: 结果}, "steps": [步骤]}`。

## 第三步：`tests/test_retrieval.py`

测试不调用真实的 Embedding 服务和索引，用 `monkeypatch` 替换 `app.rag.retrieval` 模块里的名字：
- `embed_query` → 假函数：把收到的文本记下来，返回 `[0.1] * 8`。
- `IndexStore` → 假类：`check_runtime(version, cfg)` 和 `query(version, embedding, top_k)` 都记录收到的参数；`query` 返回两条固定的片段 `[{"doc": "policies/return_policy.md", "section": "Return Windows", "text": "30 days", "score": 0.9}, {"doc": "policies/warranty_guide.md", "section": "Coverage", "text": "1 year", "score": 0.8}]`。假类上不定义任何别的方法。

`runtime` 用 `types.SimpleNamespace(context=AppContext(customer_id="CUST-001", index_version="v3"))`。任务用 `{"id": "t2", "agent": "rag", "goal": "退货政策是什么", "depends_on": []}`。

每条一个测试函数：

1. 子图只有一个节点 `retrieve`；`retrieve` 的参数列表恰好是 `["state"]`。
2. 正常路径：返回值的键恰好是 `{"task_results", "steps"}`；`task_results["t2"]["status"] == "ok"`；`data["index_version"] == "v3"`、`data["chunks"]` 是那两条片段；只有一个步骤，名字 `t2/retrieve`、`kind` 是 `code`、摘要是 `索引版本 v3，命中 2 个片段`。
3. 检索用的是 Context 里的版本：假 `IndexStore.query` 收到的 `version` 是 `"v3"`、`top_k` 是 `config.RAG_TOP_K`；`check_runtime` 被调用了 1 次。
4. 没有已发布的版本（`index_version=None`）：`status == "failed"`、`error_kind == "index_error"`；假 `embed_query` 没有被调用。
5. 配置不一致：假 `check_runtime` 抛 `IndexConfigMismatch("x")` → `status == "failed"`、`error_kind == "index_error"`；假 `query` 没有被调用。
6. Embedding 服务出错：假 `embed_query` 抛 `ConnectionError("down")` → `status == "failed"`，步骤摘要以 `检索失败: ConnectionError` 开头。
7. 上游结果进入检索词：`upstream` 里有一个 `ok` 的 `sql` 结果，`rows` 是 `[{"products.name": "Dell UltraSharp 27", "orders.order_id": "ORD-1", "x": None}]`，任务目标是 `查这个商品的保修期` → 假 `embed_query` 收到的文本是 `查这个商品的保修期 Dell UltraSharp 27 ORD-1`。
8. `build_query` 的其他情况：`upstream` 为空时返回 `goal` 本身；`failed` 的结果和 `agent` 为 `rag` 的结果被忽略；结果超过 500 字符时长度恰好是 500。
9. 子图拿不到客户 ID：正常路径下，假 `embed_query` 收到的文本和假 `IndexStore` 收到的所有参数里都不含 `CUST-001`。
10. 只读：`app/rag/retrieval.py` 的源码文本里不含 `.build(`、`.publish(`、`.rollback(`、`.gc(`、`get_model`、`bind_tools`、`app.db`（用 `pathlib` 读文件检查）。
11. 步骤的 `detail` 能被 `json.dumps(..., ensure_ascii=False)` 序列化。

```sh
uv run pytest tests/test_retrieval.py -q
```

应输出 `11 passed`。

## 最后一步：收尾检查

用真实的 Embedding 服务和已发布的索引跑一次：

```sh
uv run python -c "
from types import SimpleNamespace
from app.context import AppContext
from app.rag.index import IndexStore
from app.rag.retrieval import rag_agent_node
version = IndexStore().read_pointer()['current']
task = {'id': 't1', 'agent': 'rag', 'goal': '退货政策是什么？', 'depends_on': []}
out = rag_agent_node({'task': task, 'question': task['goal'], 'history': '', 'upstream': {}}, SimpleNamespace(context=AppContext(index_version=version)))
r = out['task_results']['t1']
print(r['status'], len(r['data']['chunks']), r['data']['chunks'][0]['doc'])"
```

通过的标准：这条命令输出 `ok 6 policies/return_policy.md`；`uv run pytest tests/test_retrieval.py -q` 输出 `11 passed`。全部通过后最后一行输出 `DONE`。
