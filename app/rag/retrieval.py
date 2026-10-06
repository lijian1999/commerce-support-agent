"""检索子图和主图里的 rag_agent 节点。

知识库的内容不可信（文档里可能夹着诱导模型的话），所以检索做成一个隔离的子图：
里面没有模型，只有一个确定性节点，状态只装检索词、索引版本和片段，
拿不到客户 ID，也看不到对话，对知识库只读。
"""
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime

from app import config
from app.context import AppContext
from app.rag.embedding import embed_query
from app.rag.index import IndexStore, runtime_config
from app.state import TaskPayload, TaskResult, make_step, make_task_result

QUERY_MAX_CHARS = 500           # 检索词的上限
SQL_ROWS_IN_QUERY = 5           # 每个上游 SQL 结果最多带几行进检索词


class RetrievalState(TypedDict, total=False):
    query: str                  # 检索词
    index_version: str          # 用哪个版本的索引
    chunks: list[dict]          # 检索到的片段
    error: str | None           # 检索过程中的错误，正常时是 None


def retrieve(state: RetrievalState) -> dict:
    """检索节点：校验索引配置 → 检索词转向量 → 查索引。

    参数只有 state：这个节点读不到 Runtime Context，也就拿不到客户 ID。
    全程确定性代码，任何异常都变成 error 字段返回，不往外抛。
    """
    try:
        store = IndexStore()
        store.check_runtime(state["index_version"], runtime_config())
        embedding = embed_query(state["query"])
        chunks = store.query(state["index_version"], embedding, config.RAG_TOP_K)
    except Exception as e:  # 索引、Embedding 服务的任何故障都在这里收敛成失败
        return {"chunks": [], "error": f"{type(e).__name__}: {e}"}
    return {"chunks": chunks, "error": None}


def build_retrieval_graph() -> CompiledStateGraph:
    """只有一个节点的检索子图：START → retrieve → END，不设 context_schema。"""
    g = StateGraph(RetrievalState)
    g.add_node("retrieve", retrieve)
    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", END)
    return g.compile()


_RETRIEVAL_GRAPH = build_retrieval_graph()


def build_query(goal: str, upstream: dict[str, TaskResult]) -> str:
    """拼检索词：任务目标，后面接上所依赖的 SQL 任务查到的值（不调模型）。"""
    query = goal
    for task_id in sorted(upstream):
        result = upstream[task_id]
        if result["status"] != "ok" or result["agent"] != "sql":
            continue
        rows = (result["data"] or {}).get("rows") or []
        values = [
            str(value)
            for row in rows[:SQL_ROWS_IN_QUERY]
            for value in row.values()
            if value is not None
        ]
        text = " ".join(values)
        if text:
            query = f"{query} {text}"
    return query[:QUERY_MAX_CHARS]


def rag_agent_node(payload: TaskPayload, runtime: Runtime[AppContext]) -> dict:
    """主图里的 RAG 节点：调用检索子图，把片段作为任务结果带出去。"""
    task = payload["task"]
    tid = task["id"]
    version = runtime.context.index_version

    if version is None:
        result = make_task_result(
            task, "failed", error_kind="index_error", error="知识库还没有已发布的版本")
        step = make_step(f"{tid}/retrieve", "code", "知识库还没有已发布的版本", None)
        return {"task_results": {tid: result}, "steps": [step]}

    query = build_query(task["goal"], payload["upstream"])
    # 只传检索词和索引版本，不传 context：子图看不到客户 ID 和对话。
    out = _RETRIEVAL_GRAPH.invoke({"query": query, "index_version": version})

    error = out.get("error")
    if error:
        detail = {"query": query, "index_version": version, "error": error}
        result = make_task_result(task, "failed", error_kind="index_error", error="知识库检索失败")
        step = make_step(f"{tid}/retrieve", "code", f"检索失败: {error}", detail)
        return {"task_results": {tid: result}, "steps": [step]}

    chunks = out["chunks"]
    data = {"index_version": version, "query": query, "chunks": chunks}
    result = make_task_result(task, "ok", data=data)
    step = make_step(f"{tid}/retrieve", "code", f"索引版本 {version}，命中 {len(chunks)} 个片段", data)
    return {"task_results": {tid: result}, "steps": [step]}
