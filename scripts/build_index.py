"""构建知识库的新版本：增量生成 Embedding 并写入新的 Chroma 集合，不发布。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config
from app.rag.embedding import embed_texts
from app.rag.index import IndexStore, runtime_config


def _names(items: list[str]) -> str:
    return f"（{'、'.join(items)}）"


def main() -> None:
    store = IndexStore()
    cfg = runtime_config()
    pointer = store.read_pointer()
    base = pointer.get("current") if pointer else None

    manifest = store.build(config.DOCS_DIR, cfg, embed_texts)
    stats = manifest["stats"]
    changes = manifest["doc_changes"]

    if not base:
        note = "尚无线上版本，全量构建"
    elif manifest["reuse_enabled"]:
        note = f"与线上版本 {base} 相同，可以复用向量"
    else:
        note = f"与线上版本 {base} 不同，全量重建"

    modified = changes["modified"]
    added = changes["added"]
    removed = changes["removed"]
    print(f"配置 hash:  {manifest['config_hash'][:8]}   {note}")
    print(
        "文档:       {docs} 篇    未变 {unchanged}   修改 {n_mod}{mod_list}   新增 {n_add}{add_list}   删除 {n_rm}{rm_list}".format(
            docs=stats["docs"],
            unchanged=changes["unchanged"],
            n_mod=len(modified),
            mod_list=_names(modified) if modified else "",
            n_add=len(added),
            add_list=_names(added) if added and len(added) <= 5 else "",
            n_rm=len(removed),
            rm_list=_names(removed) if removed else "",
        )
    )
    print(
        f"片段:       共 {stats['chunks']}   复用 {stats['reused']}"
        f"   重新生成 Embedding {stats['embedded']}   丢弃 {stats['dropped']}"
    )
    print(f"新版本 {manifest['version']} 已构建，尚未发布")


if __name__ == "__main__":
    main()
