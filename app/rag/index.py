"""知识库索引的版本管理：构建、发布、回滚、清理、查询。

一个向量由「索引配置」和「片段文本」两样东西完全决定，所以构建时优先复用旧向量，
只对新增或修改的片段调用 Embedding。每个版本是一个独立的 Chroma 集合，线上用哪个
版本由指针文件决定，发布和回滚都只是改指针。
"""
from dataclasses import asdict, dataclass
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import chromadb

from app import config
from app.rag.chunking import Chunk, chunk_markdown, normalize, sha256_text

_VERSION_RE = re.compile(r"^v(\d+)$")


@dataclass(frozen=True)
class IndexConfig:
    embedding_model: str
    embedding_dim: int
    chunker: str = "md-h2"
    max_chars: int = config.CHUNK_MAX_CHARS
    normalize_version: int = 1

    def config_hash(self) -> str:
        payload = json.dumps(
            asdict(self), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return sha256_text(payload)


def runtime_config() -> IndexConfig:
    return IndexConfig(embedding_model=config.EMBED_MODEL, embedding_dim=config.EMBED_DIM)


class IndexConfigMismatch(Exception):
    """索引配置和已构建版本不一致。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _version_number(version: str) -> int:
    match = _VERSION_RE.match(version)
    if not match:
        raise ValueError(f"非法的版本号: {version}")
    return int(match.group(1))


class IndexStore:
    def __init__(self, index_dir: Path = config.INDEX_DIR):
        self.index_dir = Path(index_dir)
        self.chroma_dir = self.index_dir / "chroma"
        self.manifest_dir = self.index_dir / "manifests"
        self.pointer_path = self.index_dir / "current.json"
        self.log_path = self.index_dir / "publish_log.jsonl"
        self.chroma_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(self.chroma_dir))

    # ---- 通用读写 ----

    def _collection_name(self, version: str) -> str:
        return f"kb_{version}"

    def _get_collection(self, version: str):
        return self._client.get_collection(self._collection_name(version), embedding_function=None)

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _write_json_atomic(path: Path, data: dict) -> None:
        """先写同目录的临时文件，再 os.replace，保证指针不会写一半。"""
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def _append_log(self, action: str, from_version: str | None, to_version: str) -> None:
        record = {"time": _now_iso(), "action": action, "from": from_version, "to": to_version}
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ---- 清单和指针 ----

    def list_versions(self) -> list[str]:
        versions = [
            path.stem for path in self.manifest_dir.glob("*.json") if _VERSION_RE.match(path.stem)
        ]
        return sorted(versions, key=_version_number)

    def read_manifest(self, version: str) -> dict:
        path = self.manifest_dir / f"{version}.json"
        if not path.exists():
            raise ValueError(f"版本 {version} 的清单不存在")
        return json.loads(path.read_text(encoding="utf-8"))

    def _read_manifest_or_none(self, version: str | None) -> dict | None:
        if not version:
            return None
        path = self.manifest_dir / f"{version}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def read_pointer(self) -> dict | None:
        if not self.pointer_path.exists():
            return None
        return json.loads(self.pointer_path.read_text(encoding="utf-8"))

    # ---- 构建 ----

    def build(
        self,
        docs_dir: Path,
        cfg: IndexConfig,
        embed_fn: Callable[[list[str]], list[list[float]]],
    ) -> dict:
        docs_dir = Path(docs_dir)
        files = sorted(docs_dir.rglob("*.md"), key=lambda p: p.relative_to(docs_dir).as_posix())
        docs: dict[str, str] = {}
        entries: list[tuple[str, Chunk]] = []       # (集合里的 id, 片段)
        for path in files:
            doc = path.relative_to(docs_dir).as_posix()
            raw = path.read_text(encoding="utf-8")
            docs[doc] = sha256_text(normalize(raw))
            for index, chunk in enumerate(chunk_markdown(doc, raw, cfg.max_chars)):
                entries.append((f"{doc}#{index}", chunk))

        pointer = self.read_pointer()
        based_on = pointer.get("current") if pointer else None
        base_manifest = self._read_manifest_or_none(based_on)
        if base_manifest is None:
            based_on = None

        # 只有基准版本的配置和这次完全一致时，才复用它的向量。
        reuse_enabled = base_manifest is not None and base_manifest.get("config_hash") == cfg.config_hash()
        reuse_map: dict[str, list[float]] = {}
        if reuse_enabled:
            collected = self._collected_vectors(based_on)
            for meta, emb in zip(collected["metadatas"], collected["embeddings"]):
                if meta and meta.get("content_hash"):
                    reuse_map[meta["content_hash"]] = [float(x) for x in emb]

        pending: list[str] = []
        seen: set[str] = set()
        for _, chunk in entries:
            if chunk.content_hash in reuse_map:
                continue
            if chunk.text not in seen:
                seen.add(chunk.text)
                pending.append(chunk.text)
        text2vec: dict[str, list[float]] = {}
        if pending:
            for text, vec in zip(pending, embed_fn(pending)):
                text2vec[text] = [float(x) for x in vec]

        vectors: list[list[float]] = []
        reused = 0
        for _, chunk in entries:
            if chunk.content_hash in reuse_map:
                reused += 1
                vectors.append(reuse_map[chunk.content_hash])
            else:
                vectors.append(text2vec[chunk.text])

        versions = self.list_versions()
        version = f"v{max(_version_number(v) for v in versions) + 1}" if versions else "v1"
        collection = self._client.create_collection(
            self._collection_name(version), metadata={"hnsw:space": "cosine"}, embedding_function=None
        )
        if entries:
            collection.add(
                ids=[entry_id for entry_id, _ in entries],
                embeddings=vectors,
                documents=[chunk.text for _, chunk in entries],
                metadatas=[
                    {"doc": chunk.doc, "section": chunk.section, "content_hash": chunk.content_hash}
                    for _, chunk in entries
                ],
            )

        base_docs = base_manifest.get("docs", {}) if base_manifest else {}
        unchanged = 0
        modified: list[str] = []
        added: list[str] = []
        for doc, doc_hash in docs.items():
            if doc not in base_docs:
                added.append(doc)
            elif base_docs[doc] == doc_hash:
                unchanged += 1
            else:
                modified.append(doc)
        removed = [doc for doc in base_docs if doc not in docs]

        new_hashes = {chunk.content_hash for _, chunk in entries}
        base_chunks = base_manifest.get("chunks", []) if base_manifest else []
        dropped = sum(1 for item in base_chunks if item.get("content_hash") not in new_hashes)

        manifest = {
            "version": version,
            "built_at": _now_iso(),
            "config": asdict(cfg),
            "config_hash": cfg.config_hash(),
            "based_on": based_on,
            "reuse_enabled": bool(reuse_enabled),
            "docs": docs,
            "chunks": [
                {"id": entry_id, "doc": chunk.doc, "section": chunk.section, "content_hash": chunk.content_hash}
                for entry_id, chunk in entries
            ],
            "doc_changes": {
                "unchanged": unchanged,
                "modified": sorted(modified),
                "added": sorted(added),
                "removed": sorted(removed),
            },
            "stats": {
                "docs": len(docs),
                "chunks": len(entries),
                "reused": reused,
                "embedded": len(entries) - reused,
                "dropped": dropped,
            },
        }
        self._write_json(self.manifest_dir / f"{version}.json", manifest)
        return manifest

    def _collected_vectors(self, version: str | None) -> dict[str, Any]:
        """读出某个版本集合里的全部向量和元数据，读不到就当成空的。"""
        empty = {"embeddings": [], "metadatas": []}
        if not version:
            return empty
        try:
            collected = self._get_collection(version).get(include=["embeddings", "metadatas"])
        except Exception:
            return empty
        embeddings = collected.get("embeddings")
        metadatas = collected.get("metadatas")
        if embeddings is None or metadatas is None:
            return empty
        return {
            "embeddings": [list(x) for x in embeddings],
            "metadatas": [meta if meta else {} for meta in metadatas],
        }

    # ---- 发布 / 回滚 ----

    def publish(self, version: str, cfg: IndexConfig) -> tuple[str | None, str]:
        manifest = self.read_manifest(version)
        pointer = self.read_pointer()
        current = pointer.get("current") if pointer else None
        if version == current:
            raise ValueError(f"版本 {version} 已经线上")
        if manifest.get("config_hash") != cfg.config_hash():
            raise IndexConfigMismatch(
                f"配置 hash 不一致: 清单 {str(manifest.get('config_hash'))[:8]} != 当前 {cfg.config_hash()[:8]}"
            )
        chunks = manifest.get("stats", {}).get("chunks", 0)
        if not chunks:
            raise ValueError(f"版本 {version} 的片段数为 0")
        current_manifest = self._read_manifest_or_none(current)
        if current_manifest:
            limit = current_manifest.get("stats", {}).get("chunks", 0) / 2
            if chunks < limit:
                raise ValueError(f"版本 {version} 的片段数 {chunks} 不到线上版本的一半（{limit:g}）")

        self._write_json_atomic(
            self.pointer_path,
            {
                "current": version,
                "previous": current,
                "config_hash": manifest.get("config_hash"),
                "published_at": _now_iso(),
            },
        )
        self._append_log("publish", current, version)
        return current, version

    def rollback(self) -> tuple[str, str]:
        pointer = self.read_pointer()
        current = pointer.get("current") if pointer else None
        previous = pointer.get("previous") if pointer else None
        if not current or not previous:
            raise ValueError("没有可回滚的版本")
        manifest = self.read_manifest(previous)
        self._write_json_atomic(
            self.pointer_path,
            {
                "current": previous,
                "previous": current,
                "config_hash": manifest.get("config_hash"),
                "published_at": _now_iso(),
            },
        )
        self._append_log("rollback", current, previous)
        return current, previous

    def gc(self, keep: int = 3) -> list[str]:
        versions = self.list_versions()
        if not versions:
            return []
        keep = max(1, int(keep))
        kept = set(versions[-keep:])
        pointer = self.read_pointer()
        if pointer:
            for version in (pointer.get("current"), pointer.get("previous")):
                if version in versions:
                    kept.add(version)
        removed: list[str] = []
        for version in versions:
            if version in kept:
                continue
            try:
                self._client.delete_collection(self._collection_name(version))
            except Exception:
                pass
            path = self.manifest_dir / f"{version}.json"
            if path.exists():
                path.unlink()
            removed.append(version)
        return sorted(removed, key=_version_number)

    def check_runtime(self, version: str, cfg: IndexConfig) -> None:
        manifest = self.read_manifest(version)
        if manifest.get("config_hash") != cfg.config_hash():
            raise IndexConfigMismatch(
                f"线上版本 {version} 的配置 hash {str(manifest.get('config_hash'))[:8]} "
                f"和当前配置 {cfg.config_hash()[:8]} 不一致"
            )

    def query(self, version: str, embedding: list[float], top_k: int) -> list[dict]:
        collection = self._get_collection(version)
        hits = collection.query(
            query_embeddings=[embedding], n_results=top_k, include=["documents", "metadatas", "distances"]
        )
        metadatas = (hits.get("metadatas") or [[]])[0]
        documents = (hits.get("documents") or [[]])[0]
        distances = (hits.get("distances") or [[]])[0]
        results: list[dict] = []
        for meta, text, distance in zip(metadatas, documents, distances):
            meta = meta or {}
            results.append(
                {
                    "doc": meta.get("doc", ""),
                    "section": meta.get("section", ""),
                    "text": text,
                    "score": float(1 - distance),
                }
            )
        results.sort(key=lambda item: item["score"], reverse=True)
        return results
