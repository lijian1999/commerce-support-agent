"""评测环境的标识：数据库内容、知识库索引、模型配置各算一个指纹，写进 Manifest 和报告，用来核对「是不是在同一个环境上跑的」。"""
import hashlib
import json
from pathlib import Path

from app import config
from app.db import connect, to_jsonable
from app.rag.index import IndexStore
from app.runner import git_version

# 参与数据库指纹的表和排序用的主键。取消记录表和工单表不算在内：它们只记录操作，不影响 Agent 的回答。
DB_TABLES = (("customers", "customer_id"), ("products", "product_id"),
             ("orders", "order_id"), ("order_items", "order_item_id"))
AGENT_ROLES = ("router", "planner", "sql_planner", "answer")   # 决定 Agent 行为的角色；judge 属于 Evaluator


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def db_hash() -> str:
    """四张业务表全部内容的 SHA-256（前 12 位）。订单被取消过、数据被改过，这个值都会变。"""
    conn = connect("root")
    try:
        parts: list[str] = []
        with conn.cursor() as cur:
            for table, key in DB_TABLES:
                cur.execute(f"SELECT * FROM `{table}` ORDER BY `{key}`")
                parts.append(json.dumps(to_jsonable(cur.fetchall()), sort_keys=True, ensure_ascii=False))
    finally:
        conn.close()
    return _sha("\n".join(parts))[:12]


def index_hash(version: str, index_dir: Path | str | None = None) -> str:
    """一个索引版本的内容指纹：索引配置的 hash 加上全部片段的 hash（前 12 位）。和版本号叫什么无关。"""
    store = IndexStore(Path(index_dir)) if index_dir else IndexStore()
    manifest = store.read_manifest(version)
    chunk_hashes = sorted(chunk["content_hash"] for chunk in manifest["chunks"])
    return _sha(manifest["config_hash"] + "\n" + "\n".join(chunk_hashes))[:12]


def current_index_version() -> str | None:
    pointer = IndexStore().read_pointer()
    return pointer["current"] if pointer else None


def agent_version(index_version: str | None = None) -> dict:
    """候选 Agent 版本：代码的 Git 版本、各角色的模型、知识库索引。"""
    version = index_version or current_index_version()
    models = {role: config.role_target(role).model for role in AGENT_ROLES}
    return {
        "git": git_version(),
        "models": models,
        "models_hash": _sha(json.dumps(models, sort_keys=True))[:8],
        "index_version": version,
        "index_hash": index_hash(version) if version else None,
    }


def env_fingerprint() -> dict:
    """评测环境：数据库内容和 Runtime 的「今天」。"""
    return {"db_hash": db_hash(), "today": config.DEFAULT_TODAY}
