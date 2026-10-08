"""带诱导文档的知识库索引：在正常文档之外加上诱导文档，单独建一份，不碰线上的索引。"""
import shutil
import tempfile
from pathlib import Path

from app import config
from app.rag.embedding import embed_texts
from app.rag.index import IndexStore, runtime_config
from evals import env
from evals.schema import POISONED_INDEX_DIR, POISON_DOCS_DIR


def build_poisoned_index() -> dict:
    """把线上文档和诱导文档合到临时目录里建一次索引，发布到带诱导文档的索引目录。"""
    staging = Path(tempfile.mkdtemp(prefix="csa-poison-"))
    try:
        shutil.copytree(config.DOCS_DIR, staging / "documents")
        docs_dir = staging / "documents"
        (docs_dir / "products").mkdir(parents=True, exist_ok=True)
        (docs_dir / "policies").mkdir(parents=True, exist_ok=True)
        for path in sorted(POISON_DOCS_DIR.glob("*.md")):
            target = "products" if path.name.startswith("TECH-") else "policies"
            shutil.copy2(path, docs_dir / target / path.name)

        store = IndexStore(POISONED_INDEX_DIR)
        manifest = store.build(docs_dir, runtime_config(), embed_texts)
        store.publish(manifest["version"], runtime_config())
        store.gc(keep=1)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    stats = manifest["stats"]
    return {
        "version": manifest["version"],
        "docs": stats["docs"],
        "chunks": stats["chunks"],
        "embedded": stats["embedded"],
        "index_hash": env.index_hash(manifest["version"], POISONED_INDEX_DIR),
    }


if __name__ == "__main__":
    info = build_poisoned_index()
    print(f"带诱导文档的索引 {info['version']}：{info['docs']} 篇文档，{info['chunks']} 个片段，"
          f"这次新生成 Embedding {info['embedded']} 个，指纹 {info['index_hash']}")
