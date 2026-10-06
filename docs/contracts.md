# 接口约定

各模块之间交接的函数签名、数据结构、文件路径。名字一字不差。数据结构的定义在 `app/state.py`，配置在 `app/config.py`，Runtime Context 在 `app/context.py`。

## 1. 总览

```
用户消息 + AppContext
   │
 router ──┬─ sql ────────► sql_agent ─┐
          ├─ rag ────────► rag_agent ─┤
          ├─ supervisor ─► planner ─► validate ─► scheduler ─► sql_agent / rag_agent（按批次并行）─┤
          │                                                                                     ▼
          ├─ direct ──────────────────────────────────────────────────────────────────────► answer
          ├─ cancel ─► cancel_extract ─► cancel_check ─► cancel_confirm ─► cancel_execute ─► cancel_reply
          └─ handoff ─► handoff
```

| 模块 | 提供 |
|---|---|
| `app/models.py` | `get_model`、`resolve_targets`、`is_transient`、`RoleModel`、`ModelResult`、`ModelError`、`ModelUnavailable`、`ModelCallError` |
| `app/db.py` | `connect`、`execute_sql`、`to_jsonable` |
| `app/sql_agent/catalog.py` | 白名单目录 `CATALOG`、`catalog_prompt()` |
| `app/sql_agent/plan.py` | `QueryPlan` 及其子结构 |
| `app/sql_agent/compiler.py` | `compile_plan`、`CompiledQuery`、`PlanRejected`、`CompilerBug` |
| `app/sql_agent/graph.py` | `build_sql_graph()`、`sql_agent_node` |
| `app/supervisor/plan.py` | `ExecutionPlan`、`PlanTask`、`validate_plan`、`PlanCheck`、`next_batch` |
| `app/supervisor/nodes.py` | `planner_node`、`validate_node`、`scheduler_node` |
| `app/rag/chunking.py` | `normalize`、`sha256_text`、`chunk_markdown`、`Chunk` |
| `app/rag/embedding.py` | `get_embeddings()`、`embed_texts`、`embed_query` |
| `app/rag/index.py` | `IndexConfig`、`runtime_config()`、`IndexStore`、`IndexConfigMismatch` |
| `app/rag/retrieval.py` | `build_retrieval_graph()`、`rag_agent_node` |
| `app/router.py` | `RouteDecision`、`router_node` |
| `app/answer.py` | `answer_node` |
| `app/cancel.py` | `cancel_extract_node`、`cancel_check_node`、`cancel_confirm_node`、`cancel_execute_node`、`cancel_reply_node` |
| `app/handoff.py` | `handoff_node` |
| `app/graph.py` | `build_graph(checkpointer=None)` |
| `app/runner.py` | `make_context`、`run_turn` |

## 2. 节点的通用约定

- 主图节点的签名：`def xxx_node(state: State, runtime: Runtime[AppContext]) -> dict`。`Runtime` 来自 `langgraph.runtime`，用 `runtime.context` 读 `AppContext`。
- 节点返回的更新里必须有 `steps`：`[make_step(name, kind, summary, detail, notes)]`，只含一条。`detail` 必须能被 `json.dumps(..., ensure_ascii=False)` 序列化。
- `kind`：节点里调用了模型就是 `"model"`，否则 `"code"`。
- 向用户输出最终回复的节点（`answer`、`cancel_reply`、`handoff`）除了设 `answer`，还要往 `messages` 追加一条 `AIMessage(content=回复文本)`。
- 节点不抛异常给图：模型调用捕获 `ModelError`，数据库调用捕获 `pymysql.MySQLError`，转成约定的失败结果。

### 步骤名

| 节点 | 步骤名 | kind |
|---|---|---|
| router | `router` | model |
| planner | `planner` | model |
| validate | `validate` | code |
| scheduler | `scheduler` | code |
| SQL 子图的三个节点 | `<任务编号>/sql_plan`、`<任务编号>/compile`、`<任务编号>/execute` | model、code、code |
| rag_agent | `<任务编号>/retrieve` | code |
| answer | `answer` | model（走模板时为 code） |
| 取消订单的五个节点 | `cancel/extract`、`cancel/check`、`cancel/confirm`、`cancel/execute`、`cancel/reply` | code |
| handoff | `handoff` | 调了模型写摘要时为 model，否则 code |

单领域路径（router 直接分到 sql 或 rag）的任务编号固定为 `t1`。

## 3. `app/models.py`

```python
class ModelError(Exception):
    notes: list[str]            # 到失败为止的重试、降级记录

class ModelUnavailable(ModelError): ...   # 主模型和备用模型都因瞬时错误失败
class ModelCallError(ModelError): ...     # 不该重试的错误（参数错误、输出解析失败等）

@dataclass
class ModelResult:
    value: Any                  # 没有 schema 时是回复文本 str；有 schema 时是该 schema 的 pydantic 实例
    notes: list[str]            # 重试、降级记录；一次成功时为空列表

class RoleModel:
    def invoke(self, messages: list) -> ModelResult: ...

def get_model(role: str, schema: type[BaseModel] | None = None, fault: str = "off") -> RoleModel
def resolve_targets(role: str, fault: str = "off") -> list[tuple[str, ModelTarget]]   # [("主模型", ...), ("备用模型", ...)]
def is_transient(exc: BaseException) -> bool
```

- `messages` 是 LangChain 的消息列表，或 `[("system", "..."), ("user", "...")]` 这种元组列表。
- 节点里的用法：`result = get_model("router", RouteDecision, fault=runtime.context.fault).invoke(messages)`，然后用 `result.value` 和 `result.notes`；失败时 `except ModelError as e`，用 `e.notes`。

## 4. `app/db.py`

```python
def connect(account: str) -> pymysql.connections.Connection   # account: "ro" / "rw" / "root"
def execute_sql(sql: str, params: list) -> dict                # 只读账号执行一条 SELECT
def to_jsonable(value: Any) -> Any
```

- `connect` 返回的连接使用 `pymysql.cursors.DictCursor`，`autocommit=False`，字符集 `utf8mb4`。`"ro"` 连接建立后执行 `SET SESSION MAX_EXECUTION_TIME = <config.SQL_TIMEOUT_MS>`。
- `execute_sql` 返回 `{"columns": [...], "rows": [{列名: 值, ...}, ...], "row_count": n}`，值都经过 `to_jsonable`（`Decimal` → `float`，`date`/`datetime` → ISO 字符串）。它带 `@traceable(run_type="tool", name="execute_sql")`。数据库出错时抛出 `pymysql.MySQLError`，由调用它的节点处理。

### 表

`customers`、`products`、`orders`、`order_items` 四张表的列见 `data/structured/*.json` 的字段；`orders` 多一列 `cancelled_at DATETIME NULL`。另有两张表：

| 表 | 列 |
|---|---|
| `order_cancellations` | `id` 自增主键、`order_id`（唯一）、`customer_id`、`session_id`、`trace_id`、`cancelled_at` |
| `handoff_tickets` | `ticket_id` 主键、`customer_id`（可空）、`reason`、`summary`、`order_ids`、`session_id`、`trace_id`、`created_at` |

账号：`agent_ro` 只能 SELECT 四张业务表；`agent_rw` 能 SELECT `orders`、只能 UPDATE `orders` 的 `status` 和 `cancelled_at` 两列、只能 INSERT 上面两张表。

## 5. SQL Agent

### 5.1 `app/sql_agent/plan.py`

```python
class Metric(BaseModel):
    func: Literal["sum", "count", "count_distinct", "avg", "min", "max"]
    column: str                 # 表.列
    alias: str                  # 结果列名

class GroupBy(BaseModel):
    column: str                 # 表.列
    granularity: Literal["none", "month", "year"]

class Filter(BaseModel):
    column: str                 # 表.列
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte", "between", "in", "contains", "last_n_days"]
    values: list[str]           # 值一律写成字符串，由编译器按列的类型转换

class OrderBy(BaseModel):
    key: str                    # 表.列，或某个聚合的 alias
    direction: Literal["asc", "desc"]

class QueryPlan(BaseModel):
    reasoning: str
    answerable: bool
    unanswerable_reason: str
    select: list[str]
    metrics: list[Metric]
    group_by: list[GroupBy]
    filters: list[Filter]
    order_by: list[OrderBy]
    limit: int
```

字段顺序就是上面的顺序。

### 5.2 `app/sql_agent/compiler.py`

```python
@dataclass
class CompiledQuery:
    sql: str
    params: list

class PlanRejected(Exception):
    kind: str       # "fixable"（模型可以改对）/ "login_required" / "unanswerable"
    message: str

class CompilerBug(Exception): ...   # 出口断言失败

def compile_plan(plan: QueryPlan, customer_id: str | None, today: str) -> CompiledQuery
```

### 5.3 `app/sql_agent/graph.py`

```python
def build_sql_graph() -> CompiledStateGraph     # 三个节点：sql_plan → compile → execute
def sql_agent_node(payload: TaskPayload, runtime: Runtime[AppContext]) -> dict   # 返回 {"task_results": {任务编号: TaskResult}}
```

`sql_agent_node` 是主图里的节点，它在内部调用 SQL 子图，自己不产生步骤（三个步骤由子图的节点产生）。成功时 `TaskResult.data` 是 `execute_sql` 的返回值。

## 6. Supervisor

### 6.1 `app/supervisor/plan.py`

```python
class PlanTask(BaseModel):
    id: str
    agent: Literal["sql", "rag"]
    goal: str
    depends_on: list[str]

class ExecutionPlan(BaseModel):
    reasoning: str
    tasks: list[PlanTask]

@dataclass
class PlanCheck:
    ok: bool
    errors: list[str]
    batches: list[list[str]]    # 校验不通过时为空列表

def validate_plan(plan: dict) -> PlanCheck      # 入参是 ExecutionPlan.model_dump() 形状的字典
def next_batch(batches: list[list[str]], batch_index: int, tasks: dict[str, Task],
               results: dict[str, TaskResult]) -> tuple[int, list[str], dict[str, TaskResult]]
```

### 6.2 `app/supervisor/nodes.py`

`planner_node`、`validate_node`、`scheduler_node`，读写的状态字段：`plan`、`plan_errors`、`plan_attempts`、`batches`、`batch_index`、`current_batch`、`task_results`、`handoff`。

## 7. RAG

### 7.1 `app/rag/chunking.py`

```python
@dataclass
class Chunk:
    doc: str            # 相对 data/documents 的路径，如 policies/return_policy.md
    section: str        # 小节标题
    text: str           # 送去生成 Embedding 的文本（含文档标题和小节标题）
    content_hash: str   # sha256_text(text)

def normalize(text: str) -> str
def sha256_text(text: str) -> str
def chunk_markdown(doc: str, text: str, max_chars: int = config.CHUNK_MAX_CHARS) -> list[Chunk]
```

### 7.2 `app/rag/embedding.py`

```python
def get_embeddings() -> OpenAIEmbeddings
def embed_texts(texts: list[str]) -> list[list[float]]
def embed_query(text: str) -> list[float]
```

### 7.3 `app/rag/index.py`

```python
@dataclass(frozen=True)
class IndexConfig:
    embedding_model: str
    embedding_dim: int
    chunker: str = "md-h2"
    max_chars: int = config.CHUNK_MAX_CHARS
    normalize_version: int = 1
    def config_hash(self) -> str

def runtime_config() -> IndexConfig
class IndexConfigMismatch(Exception): ...

class IndexStore:
    def __init__(self, index_dir: Path = config.INDEX_DIR)
    def read_pointer(self) -> dict | None       # {"current", "previous", "config_hash", "published_at"}
    def list_versions(self) -> list[str]        # ["v1", "v2", ...]，按版本号升序
    def read_manifest(self, version: str) -> dict
    def build(self, docs_dir: Path, cfg: IndexConfig, embed_fn: Callable[[list[str]], list[list[float]]]) -> dict
    def publish(self, version: str, cfg: IndexConfig) -> tuple[str | None, str]
    def rollback(self) -> tuple[str, str]
    def gc(self, keep: int = 3) -> list[str]
    def check_runtime(self, version: str, cfg: IndexConfig) -> None    # 不一致时抛 IndexConfigMismatch
    def query(self, version: str, embedding: list[float], top_k: int) -> list[dict]
```

`query` 返回的每个片段：`{"doc": str, "section": str, "text": str, "score": float}`，`score` 是余弦相似度，越大越相关，按 `score` 降序。

### 7.4 `app/rag/retrieval.py`

```python
def build_retrieval_graph() -> CompiledStateGraph     # 一个节点 retrieve；状态只有 query、index_version、chunks、error
def rag_agent_node(payload: TaskPayload, runtime: Runtime[AppContext]) -> dict   # 返回 {"task_results": {...}, "steps": [...]}
```

成功时 `TaskResult.data` 是 `{"index_version": "v3", "chunks": [...]}`。

## 8. 其余节点读写的状态字段

| 节点 | 读 | 写 |
|---|---|---|
| `router_node` | `messages` | `route`、`route_reason`；模型失败时 `route="handoff"` 并写 `handoff` |
| `answer_node` | `messages`、`route`、`task_results` | `answer`、`sources`、`messages`；模型失败时写 `handoff` |
| `handoff_node` | `messages`、`route`、`route_reason`、`handoff`、`plan_errors`、`task_results` | `handoff`（工单内容）、`card`、`answer`、`messages` |
| 取消订单的节点 | `messages`、`cancel` | `cancel`、`card`、`answer`、`messages` |

`state["handoff"]` 在写工单之前的形状是 `{"reason": "user_request" | "out_of_scope" | "system_failure", "detail": str}`。

## 9. `app/runner.py`

```python
def make_context(customer_id: str | None, today: str | None = None, fault: str = "off", session_id: str = "") -> AppContext
def run_turn(graph, ctx: AppContext, thread_id: str, text: str | None = None, resume: Any = None) -> Iterator[tuple[str, Any]]
```

`run_turn` 产出的事件：`("step", Step)`、`("interrupt", 预览字典)`、`("final", 这一轮结束时的状态字典)`。命令行、界面、评测脚本都只通过它调用图。
