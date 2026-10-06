"""命令行：一次请求里每个节点跑完就打印一行，方便看清内部每一步。

不写问题就进入交互模式，同一个会话的所有轮次共享上下文。
"""
import argparse
import json
import sys
import uuid

from langgraph.checkpoint.memory import InMemorySaver

from app import config
from app.graph import build_graph
from app.runner import make_context, run_turn
from app.state import Step

COLUMN = 18                       # 步骤名占据的列宽，之后的摘要从这一列开始

# --verbose：在每个步骤下面再打印 detail 的 JSON
VERBOSE = False

_YELLOW = "\033[33m"
_PURPLE = "\033[35m"
_GRAY = "\033[90m"
_RESET = "\033[0m"


def _label_color(step: Step) -> str:
    """[步骤名] 的颜色：有重试记录用黄色，其余按 kind 区分。"""
    if step["notes"]:
        return _YELLOW
    if step["kind"] == "model":
        return _PURPLE
    if step["kind"] == "code":
        return _GRAY
    return ""


def format_step(step: Step, color: bool = False, verbose: bool = False) -> str:
    """一个步骤的文本表示：第一行是名字加摘要，后面是续行、重试记录和可选的 detail。"""
    label = f"[{step['name']}]"
    pad = " " * (COLUMN - len(label)) if len(label) < COLUMN else "  "
    code = _label_color(step) if color else ""
    head = (f"{code}{label}{_RESET}" if code else label) + pad

    summary = str(step["summary"]).split("\n")
    lines = [head + summary[0]]
    lines += [" " * COLUMN + line for line in summary[1:]]
    for note in step["notes"]:
        lines.append(" " * COLUMN + "↻ " + note)
    if verbose and step["detail"] is not None:
        detail = json.dumps(step["detail"], ensure_ascii=False, indent=2)
        lines += [" " * COLUMN + line for line in detail.split("\n")]
    return "\n".join(lines)


def format_preview(preview: dict) -> str:
    """把取消订单的预览打印成几行，让用户在确认前看清楚要动哪个订单。"""
    lines = [f"操作: {preview['action_label']}   订单: {preview['order_id']}   "
             f"下单: {preview['order_date']}   状态: {preview['status']}"]
    for item in preview["items"]:
        lines.append(f"明细: {item['name']} × {item['quantity']}   ${float(item['price_per_unit']):.2f}")
    lines.append(f"合计: ${float(preview['total_amount']):.2f}   执行后: {preview['effect']}")
    return "\n".join(" " * COLUMN + line for line in lines)


def _ask_confirm() -> bool:
    """问用户要不要取消，只有回答 y / yes 算确认。"""
    return input("确认取消吗？[y/n] ").strip().lower() in ("y", "yes")


def run_and_print(graph, ctx, thread_id: str, text: str | None = None, resume=None,
                  auto_confirm: bool | None = None) -> dict:
    """跑一轮并把每一步打印出来，返回这一轮结束时的状态。遇到确认就中断问用户。"""
    values: dict = {}
    model_steps: list[str] = []
    last_summary: str = ""          # 最后打印过的那一步的摘要
    for kind, payload in run_turn(graph, ctx, thread_id, text, resume):
        if kind == "step":
            print(format_step(payload, color=sys.stdout.isatty(), verbose=VERBOSE))
            last_summary = str(payload["summary"])
            if payload["kind"] == "model":
                model_steps.append(str(payload["name"]))
        elif kind == "interrupt":
            print("[cancel/confirm]".ljust(COLUMN) + "⏸ 等待确认")
            print(format_preview(payload))
            confirm = auto_confirm if auto_confirm is not None else _ask_confirm()
            return run_and_print(graph, ctx, thread_id, None,
                                 {"confirm": confirm, "order_id": payload["order_id"]}, auto_confirm)
        else:
            values = payload

    # 走到这里说明这一轮正常结束，没有停在确认上
    # 回复文本和最后一步的摘要不是同一句时（转人工就是这种）单独再打一行
    answer = values.get("answer")
    if answer and answer != last_summary:
        print(f"客服> {answer}")
    if model_steps:
        print(f"—— 有模型参与的步骤 {len(model_steps)} 个: {', '.join(model_steps)}")
    else:
        print(f"—— 有模型参与的步骤 {len(model_steps)} 个")
    sources = values.get("sources") or []
    if sources:
        print(f"—— 引用来源: {', '.join(sources)}")
    return values


def main(argv: list[str] | None = None) -> int:
    global VERBOSE
    parser = argparse.ArgumentParser(description="TechHub 客服 Agent 命令行")
    parser.add_argument("--customer", default=None, help="当前登录的客户编号，代表网关已验证的身份；不给就是匿名用户")
    parser.add_argument("--today", default=None, help="Runtime 里的「今天」，默认取配置里的 DEFAULT_TODAY")
    parser.add_argument("--fault", default="off", choices=list(config.FAULTS), help="模拟故障")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--yes", action="store_true", help="遇到取消订单的确认时自动回答「是」")
    group.add_argument("--no", action="store_true", help="遇到取消订单的确认时自动回答「否」")
    parser.add_argument("--verbose", action="store_true", help="每个步骤下面再打印 detail 的 JSON")
    parser.add_argument("--index-version", default=None,
                        help="指定知识库的版本，可以是还没发布的版本；不给就用线上版本")
    parser.add_argument("question", nargs="?", default=None, help="要问的问题；不给就进入交互模式")
    args = parser.parse_args(argv)
    VERBOSE = args.verbose
    auto_confirm = True if args.yes else (False if args.no else None)

    graph = build_graph(checkpointer=InMemorySaver())
    thread_id = uuid.uuid4().hex[:8]
    ctx = make_context(args.customer, args.today, args.fault, session_id=thread_id,
                       index_version=args.index_version)
    print(f"会话 {thread_id}   客户: {ctx.customer_id or '未登录'}   今天: {ctx.today}   "
          f"索引版本: {ctx.index_version or '无'}   模拟故障: {ctx.fault}")

    if args.question is not None:
        run_and_print(graph, ctx, thread_id, args.question, auto_confirm=auto_confirm)
        return 0

    # 交互模式：同一个 thread_id，第二轮能接上第一轮的对话
    while True:
        try:
            line = input("你> ")
        except EOFError:
            print()
            break
        text = line.strip()
        if not text:
            continue
        if text in ("exit", "quit"):
            break
        # 每一轮重新读一次索引指针，线上版本换了这一轮就能跟上
        ctx = make_context(args.customer, args.today, args.fault, session_id=thread_id,
                           index_version=args.index_version)
        run_and_print(graph, ctx, thread_id, text, auto_confirm=auto_confirm)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
