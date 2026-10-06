"""知识库的版本状态、发布和回滚。"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.rag.index import IndexStore, runtime_config

_VERSION_RE = re.compile(r"^v(\d+)$")


def _number(version: str) -> int:
    match = _VERSION_RE.match(version)
    return int(match.group(1)) if match else -1


def _or_none(value: str | None) -> str:
    return value or "无"


def _status(store: IndexStore) -> None:
    pointer = store.read_pointer()
    current = pointer.get("current") if pointer else None
    previous = pointer.get("previous") if pointer else None
    versions = store.list_versions()
    if current:
        pending = [version for version in versions if _number(version) > _number(current)]
    else:
        pending = versions
    print(
        f"线上: {_or_none(current)}    上一个: {_or_none(previous)}"
        f"    已构建未发布: {'，'.join(pending) if pending else '无'}"
    )


def _publish(store: IndexStore, version: str) -> int:
    pointer = store.read_pointer()
    old = pointer.get("current") if pointer else None
    old_chunks: int | None = None
    if old:
        try:
            old_chunks = store.read_manifest(old)["stats"]["chunks"]
        except ValueError:
            old_chunks = None
    try:
        _, new = store.publish(version, runtime_config())
    except Exception as exc:                    # 配置不一致、片段数太少、版本不存在等
        print(f"发布失败: {exc}")
        return 1
    chunks = store.read_manifest(new)["stats"]["chunks"]
    if old_chunks is None:
        print(f"检查通过: 配置 hash 一致，片段数 {chunks}")
    else:
        print(f"检查通过: 配置 hash 一致，片段数 {chunks}（{old} 为 {old_chunks}）")
    print(f"线上: {_or_none(old)} → {new}")
    return 0


def _rollback(store: IndexStore) -> int:
    try:
        old, new = store.rollback()
    except Exception as exc:
        print(f"回滚失败: {exc}")
        return 1
    print(f"线上: {old} → {new}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="知识库索引的版本管理")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="查看线上版本和已构建未发布的版本")
    publish = sub.add_parser("publish", help="把某个版本设为线上版本")
    publish.add_argument("version", help="版本号，如 v2")
    sub.add_parser("rollback", help="回滚到上一个版本")
    args = parser.parse_args()

    store = IndexStore()
    if args.command == "status":
        _status(store)
        return
    if args.command == "publish":
        raise SystemExit(_publish(store, args.version))
    raise SystemExit(_rollback(store))


if __name__ == "__main__":
    main()
