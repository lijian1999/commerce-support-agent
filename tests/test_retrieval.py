"""Retrieval 子图与 rag_agent_node 的单元测试。

不调用真实的 Embedding 服务和索引：用 monkeypatch 把模块里的
embed_query 和 IndexStore 换成记录参数的假实现。
"""
import inspect
import json
from pathlib import Path
import types

from app import config
from app.context import AppContext
from app.rag import retrieval
from app.rag.index import IndexConfigMismatch

TASK: dict = {"id": "t2", "agent": "rag", "goal": "退货政策是什么", "depends_on": []}
CHUNKS: list[dict] = [
    {"doc": "policies/return_policy.md", "section": "Return Windows", "text": "30 days", "score": 0.9},
    {"doc": "policies/warranty_guide.md", "section": "Coverage", "text": "1 year", "score": 0.8},
]


def install_fakes(monkeypatch, *, check_error: Exception | None = None,
                  embed_error: Exception | None = None) -> dict:
    """替换模块里的 embed_query 和 IndexStore，返回记录到参数里的字典。"""
    rec: dict = {"embed_texts": [], "check": [], "query": []}

    def fake_embed(text: str) -> list[float]:
        rec["embed_texts"].append(text)
        if embed_error is not None:
            raise embed_error
        return [0.1] * 8

    class FakeIndexStore:
        """只有 check_runtime 和 query 两个方法，其他一概没有。"""

        def check_runtime(self, version: str, cfg) -> None:
            rec["check"].append((version, cfg))
            if check_error is not None:
                raise check_error

        def query(self, version: str, embedding: list[float], top_k: int) -> list[dict]:
            rec["query"].append((version, embedding, top_k))
            return [dict(chunk) for chunk in CHUNKS]

    monkeypatch.setattr(retrieval, "embed_query", fake_embed)
    monkeypatch.setattr(retrieval, "IndexStore", FakeIndexStore)
    return rec


def make_runtime(index_version: str | None = "v3"):
    return types.SimpleNamespace(context=AppContext(customer_id="CUST-001", index_version=index_version))


def make_payload(goal: str = TASK["goal"], upstream: dict | None = None) -> dict:
    task = {**TASK, "goal": goal}
    return {"task": task, "question": goal, "history": "", "upstream": upstream or {}}


def sql_result(rows: list[dict], status: str = "ok") -> dict:
    return {
        "task_id": "t1", "agent": "sql", "goal": "查商品", "status": status,
        "error_kind": None, "error": None,
        "data": {"columns": list(rows[0]) if rows else [], "rows": rows, "row_count": len(rows)},
    }


def test_子图只有一个节点且retrieve只接收state():
    graph = retrieval.build_retrieval_graph().get_graph()
    assert sorted(n for n in graph.nodes if not n.startswith("__")) == ["retrieve"]
    assert list(inspect.signature(retrieval.retrieve).parameters) == ["state"]


def test_正常路径(monkeypatch):
    install_fakes(monkeypatch)
    out = retrieval.rag_agent_node(make_payload(), make_runtime())
    assert set(out) == {"task_results", "steps"}
    result = out["task_results"]["t2"]
    assert result["status"] == "ok"
    assert result["data"]["index_version"] == "v3"
    assert result["data"]["chunks"] == CHUNKS
    assert len(out["steps"]) == 1
    step = out["steps"][0]
    assert step["name"] == "t2/retrieve"
    assert step["kind"] == "code"
    assert step["summary"] == "索引版本 v3，命中 2 个片段"


def test_检索用context里的版本(monkeypatch):
    rec = install_fakes(monkeypatch)
    retrieval.rag_agent_node(make_payload(), make_runtime())
    assert len(rec["query"]) == 1
    version, _, top_k = rec["query"][0]
    assert version == "v3"
    assert top_k == config.RAG_TOP_K
    assert len(rec["check"]) == 1


def test_没有已发布的版本(monkeypatch):
    rec = install_fakes(monkeypatch)
    out = retrieval.rag_agent_node(make_payload(), make_runtime(index_version=None))
    result = out["task_results"]["t2"]
    assert result["status"] == "failed"
    assert result["error_kind"] == "index_error"
    assert rec["embed_texts"] == []


def test_配置不一致(monkeypatch):
    rec = install_fakes(monkeypatch, check_error=IndexConfigMismatch("x"))
    out = retrieval.rag_agent_node(make_payload(), make_runtime())
    result = out["task_results"]["t2"]
    assert result["status"] == "failed"
    assert result["error_kind"] == "index_error"
    assert rec["query"] == []


def test_embedding服务出错(monkeypatch):
    install_fakes(monkeypatch, embed_error=ConnectionError("down"))
    out = retrieval.rag_agent_node(make_payload(), make_runtime())
    result = out["task_results"]["t2"]
    assert result["status"] == "failed"
    assert result["error_kind"] == "index_error"
    assert out["steps"][0]["summary"].startswith("检索失败: ConnectionError")


def test_上游结果进入检索词(monkeypatch):
    rec = install_fakes(monkeypatch)
    upstream = {"t1": sql_result([
        {"products.name": "Dell UltraSharp 27", "orders.order_id": "ORD-1", "x": None}]
    )}
    retrieval.rag_agent_node(make_payload(goal="查这个商品的保修期", upstream=upstream), make_runtime())
    assert rec["embed_texts"] == ["查这个商品的保修期 Dell UltraSharp 27 ORD-1"]


def test_build_query的其他情况():
    assert retrieval.build_query("退货政策是什么", {}) == "退货政策是什么"
    ignored = {
        "t1": sql_result([{"a": "不该出现"}], status="failed"),
        "t2": {"task_id": "t2", "agent": "rag", "goal": "g", "status": "ok",
               "error_kind": None, "error": None, "data": {"index_version": "v3", "chunks": CHUNKS}},
    }
    assert retrieval.build_query("退货政策是什么", ignored) == "退货政策是什么"
    long_rows = [{"big": "x" * 600}]
    assert len(retrieval.build_query("目标", {"t1": sql_result(long_rows)})) == 500


def test_子图拿不到客户ID(monkeypatch):
    rec = install_fakes(monkeypatch)
    retrieval.rag_agent_node(make_payload(), make_runtime())
    for text in rec["embed_texts"]:
        assert "CUST-001" not in text
    for args in rec["check"] + rec["query"]:
        assert all("CUST-001" not in str(arg) for arg in args)


def test_检索子图只读知识库():
    source = Path(retrieval.__file__).read_text(encoding="utf-8")
    for banned in (".build(", ".publish(", ".rollback(", ".gc(", "get_model", "bind_tools", "app.db"):
        assert banned not in source, banned


def test_步骤的detail可以序列化(monkeypatch):
    install_fakes(monkeypatch)
    out = retrieval.rag_agent_node(make_payload(), make_runtime())
    json.dumps(out["steps"][0]["detail"], ensure_ascii=False)
    install_fakes(monkeypatch, embed_error=ConnectionError("down"))
    failed = retrieval.rag_agent_node(make_payload(), make_runtime())
    json.dumps(failed["steps"][0]["detail"], ensure_ascii=False)
