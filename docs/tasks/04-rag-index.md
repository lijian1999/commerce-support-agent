# 任务 04：知识库的切块、增量构建、版本指针

先读 `AGENTS.md`，再读 `docs/contracts.md` 的第 7.1 到 7.3 节。本任务新建这些文件：`app/rag/chunking.py`、`app/rag/embedding.py`、`app/rag/index.py`、`scripts/build_index.py`、`scripts/index.py`、`tests/test_rag_index.py`。不要改别的文件。

## 背景

知识库的源文件是 `data/documents/` 下的 30 篇 Markdown（`products/` 25 篇、`policies/` 5 篇）。要把它们切成片段、生成向量、存进 Chroma。三个要点：

- 一个向量由「索引配置」和「片段文本」两样东西完全决定，所以用这两样的 hash 判断能不能复用旧向量，只对新增或修改的片段调用 Embedding 模型。
- 每次构建产出一个完整的新版本（一个独立的 Chroma 集合），旧版本不动。
- 线上用哪个版本由一个指针文件决定，发布和回滚都只是改指针。

## 第一步：`app/rag/chunking.py`

```python
@dataclass
class Chunk:
    doc: str
    section: str
    text: str
    content_hash: str

def normalize(text: str) -> str
def sha256_text(text: str) -> str
def chunk_markdown(doc: str, text: str, max_chars: int = config.CHUNK_MAX_CHARS) -> list[Chunk]
```

- `normalize`：把 `\r\n` 和单独的 `\r` 换成 `\n`；去掉每一行行尾的空白；去掉整段文本开头和结尾的空行。结果不以换行符结尾。
- `sha256_text`：`hashlib.sha256(text.encode("utf-8")).hexdigest()`。
- `chunk_markdown(doc, text, max_chars)`：
  1. 先 `normalize(text)`。
  2. 文档标题：第一个以 `# ` 开头的行去掉 `# ` 后的内容；没有这样的行时用 `doc` 的文件名（不带目录和 `.md`）。标题行本身不进入任何片段。
  3. 以 `## ` 开头的行是小节的起点（`### ` 及更深的标题不是，它们留在所属小节的正文里）。小节标题是这一行去掉 `## ` 后的内容；小节正文是下一行起到下一个 `## ` 行之前的内容，去掉首尾空白。
  4. 第一个 `## ` 之前的内容（不含标题行）是引言小节，小节标题固定为 `Introduction`。
  5. 正文为空的小节不产生片段。
  6. 正文长度不超过 `max_chars` 的小节产生一个片段。超过的按段落拆：用 `\n\n` 把正文分成段落，从前往后贪心地合并，合并后的长度（段落之间用 `\n\n` 连接）不超过 `max_chars` 就继续加，否则另起一块；单个段落本身超过 `max_chars` 时自成一块，不再细拆。每一块产生一个片段，`section` 都是这个小节的标题。
  7. 片段的 `text` 是 `f"{文档标题}\n{小节标题}\n\n{这一块的正文}"`，`content_hash` 是 `sha256_text(text)`，`doc` 是传入的 `doc`。
  8. 片段按在文档里出现的顺序返回。片段之间不重叠。

```sh
uv run python -c "
from pathlib import Path
from app.rag.chunking import chunk_markdown
p = Path('data/documents/policies/return_policy.md')
cs = chunk_markdown('policies/return_policy.md', p.read_text(encoding='utf-8'))
print(len(cs), cs[0].section, '|', cs[1].section, '|', cs[1].text.split(chr(10))[0], '|', len(cs[1].content_hash))"
```

应输出 `10 Introduction | Return Windows | Return Policy | 64`。

## 第二步：`app/rag/embedding.py`

```python
def get_embeddings() -> OpenAIEmbeddings
def embed_texts(texts: list[str]) -> list[list[float]]
def embed_query(text: str) -> list[float]
```

- `get_embeddings()`：`OpenAIEmbeddings(base_url=config.EMBED_BASE_URL, api_key="EMPTY", model=config.EMBED_MODEL, check_embedding_ctx_length=False)`（`OpenAIEmbeddings` 来自 `langchain_openai`）。`check_embedding_ctx_length=False` 不能省：省掉后它会在本地分词并发送 token 数组，非 OpenAI 的服务不认。
- `embed_texts(texts)`：每 32 条一批调用 `embed_documents`，结果按原顺序拼起来；空列表直接返回空列表。
- `embed_query(text)`：调用 `embed_query`。

```sh
uv run python -c "from app.rag.embedding import embed_texts, embed_query; v = embed_texts(['a', 'b']); print(len(v), len(v[0]), len(embed_query('退货政策')))"
```

应输出 `2 1024 1024`。

## 第三步：`app/rag/index.py` 的配置和存储布局

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
```

- `config_hash()`：`sha256_text(json.dumps(dataclasses.asdict(self), sort_keys=True, ensure_ascii=False, separators=(",", ":")))`。
- `runtime_config()`：`IndexConfig(embedding_model=config.EMBED_MODEL, embedding_dim=config.EMBED_DIM)`。

`IndexStore(index_dir)` 管理 `index_dir` 下的这些东西（目录不存在时在构造函数里创建）：

| 路径 | 内容 |
|---|---|
| `chroma/` | `chromadb.PersistentClient(path=str(index_dir / "chroma"))` 的数据目录 |
| `manifests/v<N>.json` | 每个版本一份清单 |
| `current.json` | 指针文件 |
| `publish_log.jsonl` | 发布和回滚的记录，每次追加一行 JSON |

- 版本号是 `v1`、`v2`…。版本 `vN` 对应的 Chroma 集合名是 `kb_vN`。
- 建集合：`client.create_collection(name, metadata={"hnsw:space": "cosine"}, embedding_function=None)`。取集合：`client.get_collection(name, embedding_function=None)`。删集合：`client.delete_collection(name)`。
- `list_versions()`：`manifests/` 下已有的版本，按版本号的数值升序。
- `read_manifest(version)`：读清单并返回字典；不存在时抛 `ValueError`。
- `read_pointer()`：读 `current.json` 返回字典；文件不存在时返回 `None`。
- 所有 JSON 文件都用 `ensure_ascii=False`、UTF-8 编码写。

## 第四步：`IndexStore.build`

```python
def build(self, docs_dir: Path, cfg: IndexConfig, embed_fn: Callable[[list[str]], list[list[float]]]) -> dict
```

1. 文档列表：`docs_dir.rglob("*.md")`，按相对路径排序；`doc` 是相对 `docs_dir` 的 POSIX 路径（如 `policies/return_policy.md`）。每篇文档的 hash 是 `sha256_text(normalize(文件内容))`。
2. 对每篇文档 `chunk_markdown(doc, 内容, cfg.max_chars)`，得到全部片段。片段在集合里的 id 是 `f"{doc}#{序号}"`，序号是它在这篇文档里的下标（从 0 开始）。
3. 基准版本：指针里的 `current`（没有指针时没有基准）。
4. 可复用的向量：只有当基准版本存在、并且它清单里的 `config_hash` 等于 `cfg.config_hash()` 时，才从基准版本的集合里读出全部向量（`collection.get(include=["embeddings", "metadatas"])`），建成「片段的 `content_hash` → 向量」的映射。否则映射为空。
5. 对新版本的每个片段：`content_hash` 在映射里就复用那个向量；不在的，收集它们的文本（相同的文本只算一次），**只调用一次** `embed_fn(待生成的文本列表)`；没有要生成的文本时不调用 `embed_fn`。
6. 新版本号 = 已有最大版本号加 1（没有任何版本时是 `v1`）。新建集合，写入全部片段：`ids`、`embeddings`、`documents`（片段的 `text`）、`metadatas`（`{"doc", "section", "content_hash"}`）。
7. 写清单 `manifests/<版本>.json` 并返回同样的字典：

```python
{
  "version": "v2",
  "built_at": "<ISO 时间>",
  "config": {...},                 # dataclasses.asdict(cfg)
  "config_hash": "...",
  "based_on": "v1",                # 基准版本；没有时为 None
  "reuse_enabled": True,           # 第 4 步里基准版本的配置 hash 是否和 cfg 相同（没有基准时为 False）
  "docs": {"policies/return_policy.md": "<文档 hash>", ...},
  "chunks": [{"id": "...", "doc": "...", "section": "...", "content_hash": "..."}, ...],
  "doc_changes": {"unchanged": 29, "modified": ["policies/return_policy.md"], "added": [], "removed": []},
  "stats": {"docs": 30, "chunks": 250, "reused": 249, "embedded": 1, "dropped": 1}
}
```

   - `doc_changes` 是和基准版本清单里的 `docs` 相比：hash 相同算 `unchanged`（只记数量），hash 不同进 `modified`，基准里没有的进 `added`，基准里有而现在没有的进 `removed`；三个列表都排序。没有基准版本时全部文档都在 `added`。
   - `stats.reused` 是复用了向量的片段数；`stats.embedded` 是新生成向量的片段数（两者之和等于 `chunks`）；`stats.dropped` 是基准版本清单的片段里，`content_hash` 没有出现在新版本任何片段里的数量（没有基准时为 0）。
8. `build` 不修改指针，也不修改任何旧版本的集合和清单。

## 第五步：`IndexStore` 的发布、回滚、清理、查询

- `publish(version, cfg) -> tuple[str | None, str]`，依次检查，不通过就抛异常且不改指针：
  1. `version` 的清单不存在 → `ValueError`。
  2. `version` 已经是指针里的 `current` → `ValueError`。
  3. 清单的 `config_hash` 不等于 `cfg.config_hash()` → `IndexConfigMismatch`。
  4. 清单的 `stats.chunks` 为 0 → `ValueError`。
  5. 已有线上版本时，`stats.chunks` 小于线上版本 `stats.chunks` 的一半 → `ValueError`。
  
  通过后写指针 `{"current": version, "previous": 原来的 current（没有则 None）, "config_hash": 清单的 config_hash, "published_at": "<ISO 时间>"}`，往 `publish_log.jsonl` 追加一行 `{"time": "<ISO 时间>", "action": "publish", "from": 原来的 current, "to": version}`，返回 `(原来的 current, version)`。
- **写指针必须是原子的**：先把内容写到同目录下的临时文件 `current.json.tmp`，再 `os.replace(临时文件, current.json)`。写完后目录里不留临时文件。
- `rollback() -> tuple[str, str]`：没有指针，或指针的 `previous` 是 `None` → `ValueError`。否则把指针改成 `{"current": 原 previous, "previous": 原 current, "config_hash": 原 previous 那个版本清单的 config_hash, "published_at": "<ISO 时间>"}`（同样原子写入），追加一行 `action` 为 `"rollback"` 的记录，返回 `(原 current, 原 previous)`。
- `gc(keep=3) -> list[str]`：保留版本号最大的 `keep` 个版本，另外指针里的 `current` 和 `previous` 永远保留；其余版本删除集合和清单。返回被删除的版本号列表（升序）。
- `check_runtime(version, cfg)`：`version` 的清单的 `config_hash` 不等于 `cfg.config_hash()` 时抛 `IndexConfigMismatch`，错误信息里写出两个 hash 的前 8 位。
- `query(version, embedding, top_k) -> list[dict]`：`collection.query(query_embeddings=[embedding], n_results=top_k, include=["documents", "metadatas", "distances"])`，转成 `[{"doc", "section", "text", "score"}, ...]`，`score = 1 - 距离`，按 `score` 降序。

## 第六步：`tests/test_rag_index.py`

测试不调用真实的 Embedding 服务：用一个假的 `embed_fn`，把每段文本确定性地映射成 8 维向量（例如用文本的 SHA-256 的前 8 个字节各除以 255），并记录每次被调用时收到的文本列表。索引目录用 pytest 的 `tmp_path`，文档目录也在 `tmp_path` 下自己写几篇小的 Markdown（每篇有标题、引言、两三个 `## ` 小节）。测试用的 `IndexConfig` 用 `IndexConfig(embedding_model="fake", embedding_dim=8)`。

每条一个测试函数：

1. `normalize`：`"a  \r\nb\r\n\r\n"` → `"a\nb"`。
2. 切块：一篇有标题、引言、两个小节的文档切成 3 个片段，`section` 依次是 `Introduction` 和两个小节标题；每个片段的 `text` 以 `"<文档标题>\n<小节标题>\n\n"` 开头。
3. `### ` 标题不拆分：它留在所属小节的正文里。
4. 超长小节按段落拆：正文是 3 个各 60 字符的段落、`max_chars=130` 时，拆成 2 个片段（前两段合在一起，第三段单独），两个片段的 `section` 相同。
5. 片段边界稳定：改某一个小节的正文，只有这个小节的片段 `content_hash` 变化；在引言里加一句话，只有引言片段的 `content_hash` 变化。
6. `config_hash`：两个相同的 `IndexConfig` 的 hash 相同；改 `max_chars` 后 hash 不同；改 `embedding_model` 后 hash 不同。
7. 首次构建：`stats.embedded` 等于 `stats.chunks`，`stats.reused` 为 0，`based_on` 为 `None`，`embed_fn` 恰好被调用 1 次；返回的版本是 `v1`；`read_pointer()` 仍是 `None`。
8. 增量构建：构建 v1 并发布，改一篇文档的一个小节，再构建 → `stats.embedded == 1`、`stats.reused == stats.chunks - 1`、`stats.dropped == 1`，`embed_fn` 这一次收到的文本列表恰好只有被修改的那个片段的文本；`doc_changes.modified` 恰好是那一篇文档。
9. 没有变化时再构建：`stats.embedded == 0`，`embed_fn` 没有被再次调用。
10. 配置变化时全量重建：v1 发布后，用 `max_chars` 不同的配置构建 → `reuse_enabled` 为 `False`，`stats.reused == 0`，`stats.embedded == stats.chunks`。
11. 构建不影响旧版本：构建 v2 之后，v1 的清单内容和 v1 集合里的片段数都没有变，指针仍指向 v1。
12. 发布：`publish("v1", cfg)` 返回 `(None, "v1")`；指针的 `current` 是 `v1`、`previous` 是 `None`；索引目录下不存在 `current.json.tmp`；`publish_log.jsonl` 有 1 行。
13. 发布检查：配置不一致的 `cfg` 发布时抛 `IndexConfigMismatch`，指针不变；发布不存在的版本抛 `ValueError`；重复发布当前版本抛 `ValueError`。
14. 片段数检查：v1 发布后，把文档删到只剩不足一半片段再构建 v2，`publish("v2", cfg)` 抛 `ValueError`，指针仍指向 v1。
15. 回滚：v1、v2 依次发布后 `rollback()` 返回 `("v2", "v1")`，指针 `current` 是 `v1`、`previous` 是 `v2`；再 `rollback()` 一次回到 `current` 为 `v2`。只发布过 v1 时 `rollback()` 抛 `ValueError`。
16. 清理：构建并依次发布 v1 到 v5 后回滚一次（此时 `current` 是 `v4`、`previous` 是 `v5`），再构建 v6（不发布），`gc(keep=1)` 返回 `["v1", "v2", "v3"]`，`list_versions()` 是 `["v4", "v5", "v6"]`。
17. 查询：用某个片段文本经 `embed_fn` 得到的向量查询，返回的第一条就是这个片段，`score` 大于 0.99，结果按 `score` 降序，每条都有 `doc`、`section`、`text`、`score` 四个键。
18. `check_runtime`：配置一致时不抛异常；不一致时抛 `IndexConfigMismatch`。
19. 删除的文档：v1 发布后删掉一篇文档再构建，新版本清单的 `docs` 里没有它，`doc_changes.removed` 是那一篇，新版本的集合里查不到 `doc` 为它的片段。

```sh
uv run pytest tests/test_rag_index.py -q
```

应输出 `19 passed`。

## 第七步：两个脚本

两个脚本开头都要让 `import app` 可用：

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```

**`scripts/build_index.py`**：用 `IndexStore()`、`runtime_config()`、`embed_texts`、`config.DOCS_DIR` 构建一个新版本并打印报告，不发布。打印 4 行，格式如下（`<…>` 是实际的值；配置 hash 只打印前 8 位）：

```
配置 hash:  <前 8 位>   <三种情况之一：「尚无线上版本，全量构建」/「与线上版本 v3 相同，可以复用向量」/「与线上版本 v3 不同，全量重建」>
文档:       <总数> 篇    未变 <n>   修改 <n>   新增 <n>   删除 <n>
片段:       共 <总数>   复用 <n>   重新生成 Embedding <n>   丢弃 <n>
新版本 <版本> 已构建，尚未发布
```

「修改」的数量不为 0 时，在数量后面的括号里列出文件名，如 `修改 1（policies/return_policy.md）`；「新增」「删除」同理，但新增超过 5 篇时不列。

**`scripts/index.py`**：三个子命令（用 `argparse`）。

- `status`：打印 `线上: <current 或 无>    上一个: <previous 或 无>    已构建未发布: <版本号大于 current 的版本，逗号分隔；没有则 无>`。没有指针时，所有已有版本都算「已构建未发布」。
- `publish <版本>`：调用 `publish(版本, runtime_config())`。成功时打印两行：`检查通过: 配置 hash 一致，片段数 <n>（<原线上版本> 为 <n>）`（没有原线上版本时括号部分省略）和 `线上: <原版本 或 无> → <新版本>`。失败时打印 `发布失败: <异常信息>` 并以状态码 1 退出。
- `rollback`：成功时打印 `线上: <原版本> → <新版本>`；失败时打印 `回滚失败: <异常信息>` 并以状态码 1 退出。

## 最后一步：收尾检查

先删除已有的索引目录，然后依次运行：

```sh
rm -rf data/index
uv run python scripts/build_index.py
uv run python scripts/index.py publish v1
uv run python scripts/build_index.py
uv run python scripts/index.py status
uv run pytest tests/test_rag_index.py -q
```

通过的标准：
- 第一次构建的第 1 行包含 `尚无线上版本，全量构建`，第 2 行以 `文档:       30 篇` 开头，第 3 行里「复用」是 0、「重新生成 Embedding」等于「共」的数量，第 4 行是 `新版本 v1 已构建，尚未发布`。
- `publish v1` 的最后一行是 `线上: 无 → v1`。
- 第二次构建的第 1 行包含 `与线上版本 v1 相同，可以复用向量`，第 3 行里「重新生成 Embedding」是 0，第 4 行是 `新版本 v2 已构建，尚未发布`。
- `status` 输出 `线上: v1    上一个: 无    已构建未发布: v2`。
- pytest 输出 `19 passed`。

全部通过后最后一行输出 `DONE`。
