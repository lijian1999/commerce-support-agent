"""知识库切块、增量构建、版本指针的单元测试。

不调用真实的 Embedding 服务：用假的 embed_fn 把文本确定性地映射成 8 维向量，
并记录每次被调用时收到的文本列表。
"""
import hashlib
from pathlib import Path

import chromadb
import pytest

from app.rag.chunking import chunk_markdown, normalize, sha256_text
from app.rag.index import IndexConfig, IndexConfigMismatch, IndexStore


def _doc(title: str, intro: str, sections: list[tuple[str, str]]) -> str:
    parts = [f"# {title}\n\n{intro}\n"]
    for name, body in sections:
        parts.append(f"\n## {name}\n{body}\n")
    return "".join(parts)


DOCS = {
    "products/a.md": _doc(
        "产品A", "这是产品A的引言。", [("安装", "先开箱，再接电源。"), ("保养", "保持干燥，避免阳光直射。")]
    ),
    "products/b.md": _doc(
        "产品B", "这是产品B的引言。", [("安装", "插上电源就能用。"), ("保养", "每月清洁一次滤网。")]
    ),
    "policies/c.md": _doc(
        "政策C", "这是政策C的引言。", [("退货", "签收后 7 天内可以退货。"), ("保修", "整机保修一年。")]
    ),
}


class FakeEmbed:
    """把文本映射成 8 维向量，并记录每次调用收到的文本列表。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self.vector(text) for text in texts]

    @staticmethod
    def vector(text: str) -> list[float]:
        return [byte / 255 for byte in hashlib.sha256(text.encode("utf-8")).digest()[:8]]


def _setup(tmp_path: Path) -> tuple[IndexStore, Path, FakeEmbed]:
    docs = tmp_path / "documents"
    for rel, text in DOCS.items():
        path = docs / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return IndexStore(tmp_path / "index"), docs, FakeEmbed()


def _cfg(**kwargs) -> IndexConfig:
    return IndexConfig(embedding_model="fake", embedding_dim=8, **kwargs)


def _chroma(store: IndexStore) -> chromadb.PersistentClient:
    return chromadb.PersistentClient(path=str(store.index_dir / "chroma"))


def _chunk_texts(doc: str, text: str) -> dict[str, str]:
    return {chunk.section: chunk.text for chunk in chunk_markdown(doc, text, _cfg().max_chars)}


# ---- 切块 ----

def test_normalize_strips_line_end_and_blank_edges() -> None:
    assert normalize("a  \r\nb\r\n\r\n") == "a\nb"


def test_chunk_by_h2_sections() -> None:
    chunks = chunk_markdown("products/a.md", DOCS["products/a.md"], _cfg().max_chars)
    assert [c.section for c in chunks] == ["Introduction", "安装", "保养"]
    for chunk in chunks:
        assert chunk.text.startswith(f"产品A\n{chunk.section}\n\n")
        assert chunk.doc == "products/a.md"
        assert len(chunk.content_hash) == 64


def test_h3_headings_stay_in_section_body() -> None:
    text = "# 产品A\n\n引言。\n\n## 安装\n第一步。\n\n### 注意事项\n别拔错线。\n\n## 保养\n擦一擦。\n"
    chunks = chunk_markdown("products/a.md", text, _cfg().max_chars)
    assert [c.section for c in chunks] == ["Introduction", "安装", "保养"]
    install = [c for c in chunks if c.section == "安装"]
    assert len(install) == 1
    assert "### 注意事项" in install[0].text and "别拔错线。" in install[0].text


def test_long_section_split_by_paragraph() -> None:
    body = "\n\n".join(["x" * 60 for _ in range(3)])
    text = f"# 长文\n\n引言。\n\n## 小节\n{body}\n"
    chunks = chunk_markdown("docs/long.md", text, max_chars=130)
    section_chunks = [c for c in chunks if c.section == "小节"]
    assert len(section_chunks) == 2
    assert section_chunks[0].text.startswith("长文\n小节\n\n")
    assert len(section_chunks[0].text.split("\n\n", 1)[1]) <= 130
    assert len(section_chunks[1].text.split("\n\n", 1)[1]) <= 130


def test_chunk_boundary_stability() -> None:
    def section_hashes(text: str) -> dict[str, str]:
        chunks = chunk_markdown("products/a.md", text, _cfg().max_chars)
        return {c.section: c.content_hash for c in chunks}

    original = DOCS["products/a.md"]
    base = section_hashes(original)
    changed = section_hashes(original.replace("保持干燥，避免阳光直射。", "保持阴凉干燥。"))
    assert changed["保养"] != base["保养"]
    assert changed["Introduction"] == base["Introduction"] and changed["安装"] == base["安装"]

    intro_only = section_hashes(original.replace("这是产品A的引言。", "这是产品A的引言。补充一句话。"))
    assert intro_only["Introduction"] != base["Introduction"]
    assert intro_only["安装"] == base["安装"] and intro_only["保养"] == base["保养"]


def test_config_hash() -> None:
    assert IndexConfig(embedding_model="fake", embedding_dim=8).config_hash() == _cfg().config_hash()
    assert IndexConfig(embedding_model="fake", embedding_dim=8, max_chars=1000).config_hash() != _cfg().config_hash()
    assert IndexConfig(embedding_model="other", embedding_dim=8).config_hash() != _cfg().config_hash()


# ---- 构建 ----

def test_first_build_is_full_rebuild(tmp_path: Path) -> None:
    store, docs, embed = _setup(tmp_path)
    manifest = store.build(docs, _cfg(), embed)
    assert manifest["version"] == "v1"
    assert manifest["based_on"] is None
    assert manifest["stats"]["embedded"] == manifest["stats"]["chunks"]
    assert manifest["stats"]["reused"] == 0
    assert len(embed.calls) == 1
    assert store.read_pointer() is None


def test_incremental_build_only_embeds_changed_chunk(tmp_path: Path) -> None:
    store, docs, embed = _setup(tmp_path)
    store.build(docs, _cfg(), embed)
    store.publish("v1", _cfg())
    path = docs / "policies" / "c.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("整机保修一年。", "整机保修两年，人为损坏不在保修范围内。"),
        encoding="utf-8",
    )
    manifest = store.build(docs, _cfg(), embed)
    assert manifest["version"] == "v2"
    assert manifest["based_on"] == "v1" and manifest["reuse_enabled"] is True
    assert manifest["stats"]["embedded"] == 1
    assert manifest["stats"]["reused"] == manifest["stats"]["chunks"] - 1
    assert manifest["stats"]["dropped"] == 1
    assert manifest["doc_changes"]["modified"] == ["policies/c.md"]
    assert len(embed.calls) == 2
    assert embed.calls[1] == [_chunk_texts("policies/c.md", path.read_text(encoding="utf-8"))["保修"]]


def test_rebuild_without_changes_embeds_nothing(tmp_path: Path) -> None:
    store, docs, embed = _setup(tmp_path)
    store.build(docs, _cfg(), embed)
    store.publish("v1", _cfg())
    calls = len(embed.calls)
    manifest = store.build(docs, _cfg(), embed)
    assert manifest["stats"]["embedded"] == 0
    assert manifest["stats"]["reused"] == manifest["stats"]["chunks"]
    assert len(embed.calls) == calls


def test_config_change_forces_full_rebuild(tmp_path: Path) -> None:
    store, docs, embed = _setup(tmp_path)
    store.build(docs, _cfg(), embed)
    store.publish("v1", _cfg())
    manifest = store.build(docs, _cfg(max_chars=999), embed)
    assert manifest["reuse_enabled"] is False
    assert manifest["stats"]["reused"] == 0
    assert manifest["stats"]["embedded"] == manifest["stats"]["chunks"]


def test_build_does_not_touch_old_version(tmp_path: Path) -> None:
    store, docs, embed = _setup(tmp_path)
    store.build(docs, _cfg(), embed)
    store.publish("v1", _cfg())
    total = sum(1 for _ in chunk_markdown("products/a.md", DOCS["products/a.md"], _cfg().max_chars)) * 3
    before = store.read_manifest("v1")
    assert _chroma(store).get_collection("kb_v1").count() == total
    store.build(docs, _cfg(), embed)
    assert store.read_manifest("v1") == before
    assert _chroma(store).get_collection("kb_v1").count() == total
    assert store.read_pointer()["current"] == "v1"


# ---- 发布 / 回滚 ----

def test_publish_writes_pointer_atomically(tmp_path: Path) -> None:
    store, docs, embed = _setup(tmp_path)
    store.build(docs, _cfg(), embed)
    assert store.publish("v1", _cfg()) == (None, "v1")
    pointer = store.read_pointer()
    assert pointer["current"] == "v1" and pointer["previous"] is None
    assert not (store.index_dir / "current.json.tmp").exists()
    lines = (store.index_dir / "publish_log.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1


def test_publish_checks(tmp_path: Path) -> None:
    store, docs, embed = _setup(tmp_path)
    store.build(docs, _cfg(), embed)
    store.publish("v1", _cfg())
    store.build(docs, _cfg(max_chars=999), embed)      # v2 的配置不同
    with pytest.raises(IndexConfigMismatch):
        store.publish("v2", _cfg())
    assert store.read_pointer()["current"] == "v1"
    with pytest.raises(ValueError):
        store.publish("v9", _cfg())
    with pytest.raises(ValueError):
        store.publish("v1", _cfg())


def test_publish_rejects_too_few_chunks(tmp_path: Path) -> None:
    store, docs, embed = _setup(tmp_path)
    store.build(docs, _cfg(), embed)
    store.publish("v1", _cfg())
    (docs / "products" / "a.md").unlink()
    (docs / "products" / "b.md").unlink()
    manifest = store.build(docs, _cfg(), embed)
    assert manifest["stats"]["chunks"] < 9 / 2
    with pytest.raises(ValueError):
        store.publish("v2", _cfg())
    assert store.read_pointer()["current"] == "v1"


def test_rollback_swaps_pointer(tmp_path: Path) -> None:
    store, docs, embed = _setup(tmp_path)
    store.build(docs, _cfg(), embed)
    store.publish("v1", _cfg())
    with pytest.raises(ValueError):
        store.rollback()
    store.build(docs, _cfg(), embed)
    store.publish("v2", _cfg())
    assert store.rollback() == ("v2", "v1")
    pointer = store.read_pointer()
    assert pointer["current"] == "v1" and pointer["previous"] == "v2"
    assert store.rollback() == ("v1", "v2")
    assert store.read_pointer()["current"] == "v2"


def test_gc_keeps_latest_and_pointer_versions(tmp_path: Path) -> None:
    store, docs, embed = _setup(tmp_path)
    store.build(docs, _cfg(), embed)
    store.publish("v1", _cfg())
    for _ in range(4):
        store.build(docs, _cfg(), embed)
        store.publish(store.list_versions()[-1], _cfg())
    store.rollback()
    assert store.read_pointer()["current"] == "v4" and store.read_pointer()["previous"] == "v5"
    store.build(docs, _cfg(), embed)                   # v6，不发布
    assert store.gc(keep=1) == ["v1", "v2", "v3"]
    assert store.list_versions() == ["v4", "v5", "v6"]


# ---- 查询 / 运行时检查 ----

def test_query_returns_matching_chunk_first(tmp_path: Path) -> None:
    store, docs, embed = _setup(tmp_path)
    store.build(docs, _cfg(), embed)
    text = _chunk_texts("policies/c.md", DOCS["policies/c.md"])["退货"]
    hits = store.query("v1", embed([text])[0], 5)
    assert hits[0]["doc"] == "policies/c.md" and hits[0]["section"] == "退货"
    assert hits[0]["text"] == text
    assert hits[0]["score"] > 0.99
    scores = [hit["score"] for hit in hits]
    assert scores == sorted(scores, reverse=True)
    for hit in hits:
        assert set(hit) == {"doc", "section", "text", "score"}


def test_check_runtime(tmp_path: Path) -> None:
    store, docs, embed = _setup(tmp_path)
    store.build(docs, _cfg(), embed)
    store.check_runtime("v1", _cfg())
    with pytest.raises(IndexConfigMismatch):
        store.check_runtime("v1", IndexConfig(embedding_model="other", embedding_dim=8))


def test_removed_doc_gone_from_new_version(tmp_path: Path) -> None:
    store, docs, embed = _setup(tmp_path)
    store.build(docs, _cfg(), embed)
    store.publish("v1", _cfg())
    (docs / "products" / "b.md").unlink()
    manifest = store.build(docs, _cfg(), embed)
    assert "products/b.md" not in manifest["docs"]
    assert manifest["doc_changes"]["removed"] == ["products/b.md"]
    hits = _chroma(store).get_collection("kb_v2").get(where={"doc": "products/b.md"})
    assert hits["ids"] == []


# ---- 脚本里状态和清理的输出 ----

import importlib.util


def load_index_script():
    path = Path(__file__).resolve().parent.parent / "scripts" / "index.py"
    spec = importlib.util.spec_from_file_location("index_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_status_excludes_previous_version(tmp_path, capsys) -> None:
    """回滚之后「上一个」发布过，不算进「已构建未发布」。"""
    script = load_index_script()
    store, docs, embed = _setup(tmp_path)
    store.build(docs, _cfg(), embed)          # v1
    store.build(docs, _cfg(), embed)          # v2
    store.build(docs, _cfg(), embed)          # v3
    store.publish("v1", _cfg())
    store.publish("v3", _cfg())
    assert store.rollback() == ("v3", "v1")   # 线上 v1，上一个 v3
    script._status(store)
    assert capsys.readouterr().out.strip() == "线上: v1    上一个: v3    已构建未发布: v2"


def test_gc_command_prints_removed_and_kept(tmp_path, capsys) -> None:
    """gc 命令把清掉的和留下的版本都打出来，重复调用没有可清理的版本。"""
    script = load_index_script()
    store, docs, embed = _setup(tmp_path)
    for _ in range(5):
        store.build(docs, _cfg(), embed)
        store.publish(store.list_versions()[-1], _cfg())
    assert script._gc(store, 2) == 0
    assert capsys.readouterr().out.strip() == "已清理: v1，v2，v3   保留: v4，v5"
    assert script._gc(store, 2) == 0
    assert capsys.readouterr().out.strip() == "没有需要清理的版本   保留: v4，v5"
