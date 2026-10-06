"""集中读取 .env 的配置。其他模块只从这里取配置，不直接读环境变量。"""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# 没有填 LangSmith 的 API Key 时关闭追踪，避免每次调用都报认证错误
if not os.getenv("LANGSMITH_API_KEY"):
    os.environ["LANGSMITH_TRACING"] = "false"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value else default


# ---- 模型 ----
ROLES = ("router", "planner", "sql_planner", "answer", "judge")


@dataclass(frozen=True)
class ModelTarget:
    base_url: str
    model: str
    api_key: str


def role_target(role: str) -> ModelTarget:
    """某个角色的主模型。没有单独配置时回落到 DEFAULT_*。"""
    prefix = role.upper()
    return ModelTarget(
        base_url=_env(f"{prefix}_BASE_URL", _env("DEFAULT_BASE_URL")),
        model=_env(f"{prefix}_MODEL", _env("DEFAULT_MODEL")),
        api_key=_env("DEFAULT_API_KEY", "EMPTY"),
    )


def role_fallback_target(role: str) -> ModelTarget | None:
    """某个角色的备用模型。完全没有配置时返回 None。"""
    prefix = role.upper()
    base_url = _env(f"{prefix}_FALLBACK_BASE_URL", _env("DEFAULT_FALLBACK_BASE_URL"))
    model = _env(f"{prefix}_FALLBACK_MODEL", _env("DEFAULT_FALLBACK_MODEL"))
    if not base_url or not model:
        return None
    return ModelTarget(base_url=base_url, model=model, api_key=_env("DEFAULT_API_KEY", "EMPTY"))


MODEL_TIMEOUT_S = 60.0          # 单次模型调用的超时
MODEL_MAX_TOKENS = 2000
MODEL_MAX_RETRIES = 2           # 每个模型在首次调用之外最多重试几次
MODEL_RETRY_BACKOFF_S = (0.5, 1.0)
MODEL_MAX_CONCURRENCY = 4       # 同时在途的模型调用上限（保护后端）
UNREACHABLE_BASE_URL = "http://127.0.0.1:9/v1"   # 模拟故障时用的连不上的地址
FAULTS = ("off", "primary_down", "all_down")

# ---- Embedding ----
EMBED_BASE_URL = _env("EMBED_BASE_URL")
EMBED_MODEL = _env("EMBED_MODEL", "bge-m3")
EMBED_DIM = int(_env("EMBED_DIM", "1024"))

# ---- MySQL ----
MYSQL_HOST = _env("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(_env("MYSQL_PORT", "3307"))
MYSQL_DATABASE = _env("MYSQL_DATABASE", "techhub")
MYSQL_ROOT_PASSWORD = _env("MYSQL_ROOT_PASSWORD")
MYSQL_RO_USER = _env("MYSQL_RO_USER", "agent_ro")
MYSQL_RO_PASSWORD = _env("MYSQL_RO_PASSWORD")
MYSQL_RW_USER = _env("MYSQL_RW_USER", "agent_rw")
MYSQL_RW_PASSWORD = _env("MYSQL_RW_PASSWORD")
SQL_TIMEOUT_MS = 5000           # 只读查询的超时
SQL_ROW_LIMIT = 50              # 编译器强制的 LIMIT 上限

# ---- 数据与知识库 ----
STRUCTURED_DIR = ROOT / "data" / "structured"
DOCS_DIR = ROOT / "data" / "documents"
INDEX_DIR = ROOT / "data" / "index"
RAG_TOP_K = 4
CHUNK_MAX_CHARS = 2000

# ---- 运行时 ----
DEFAULT_TODAY = "2025-10-20"    # 演示用的固定「今天」（订单数据截止到这一天）
HISTORY_MESSAGES = 6            # 带给模型的最近几条消息
