"""统一的运行入口：拼 Runtime Context、带上元数据跑一轮，把事件逐个交出去。"""
from pathlib import Path
import subprocess
from typing import Any, Iterator

from langgraph.types import Command

from app import config
from app.context import AppContext
from app.rag.index import IndexStore
from app.state import new_turn_input


def make_context(customer_id: str | None, today: str | None = None, fault: str = "off",
                 session_id: str = "", index_version: str | None = None,
                 index_dir: str | None = None) -> AppContext:
    """请求开始时读一次索引指针，这次请求里所有的检索都用这一个版本。
    给了 index_version 就用它（可以是还没发布的版本），不再读指针。
    index_dir 非空时指针从那个目录里读，None 表示默认目录。"""
    if index_version is None:
        store = IndexStore(Path(index_dir)) if index_dir else IndexStore()
        pointer = store.read_pointer()
        index_version = pointer["current"] if pointer else None
    return AppContext(
        customer_id=customer_id or None,
        today=today or config.DEFAULT_TODAY,
        fault=fault,
        index_version=index_version,
        index_dir=index_dir,
        session_id=session_id,
    )


def git_version() -> str:
    """当前代码的版本标识：短哈希，工作区不干净时加 -dirty。"""
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=config.ROOT).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, cwd=config.ROOT).stdout.strip()
        return (sha or "unknown") + ("-dirty" if dirty else "")
    except OSError:
        return "unknown"


def run_metadata(ctx: AppContext) -> dict:
    """随每次运行上报到 LangSmith 的元数据。"""
    return {
        "git_version": git_version(),
        "models": {role: config.role_target(role).model for role in config.ROLES},
        "index_version": ctx.index_version,
        "eval_set_version": None,
        "today": ctx.today,
        "fault": ctx.fault,
        "session_id": ctx.session_id,
    }


def run_turn(graph, ctx: AppContext, thread_id: str, text: str | None = None, resume: Any = None) -> Iterator[tuple[str, Any]]:
    """跑一轮对话（给 text），或者从中断处恢复（给 resume）。逐个产出事件：
    ("step", Step)、("interrupt", 预览字典)、("final", 这一轮结束时的状态字典)。"""
    run_config = {"configurable": {"thread_id": thread_id}, "metadata": run_metadata(ctx), "run_name": "support_turn"}
    graph_input = Command(resume=resume) if resume is not None else new_turn_input(text)
    for _namespace, chunk in graph.stream(graph_input, run_config, context=ctx, stream_mode="updates", subgraphs=True):
        for node, update in chunk.items():
            if node == "__interrupt__":
                yield ("interrupt", update[0].value)
            elif isinstance(update, dict):
                for step in update.get("steps") or []:
                    yield ("step", step)
    yield ("final", graph.get_state(run_config).values)
