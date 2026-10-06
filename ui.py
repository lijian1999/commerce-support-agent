"""电商客服 Agent 的 Streamlit 界面：一个薄壳，业务逻辑全在图里。

三栏：左边设置和运行时信息，中间聊天窗口，右边执行过程。
调用图只走 app.runner.run_turn，和命令行用的是同一个入口。
"""
from __future__ import annotations

import json
import uuid

import streamlit as st
from langgraph.checkpoint.memory import InMemorySaver

from app import config
from app.graph import build_graph
from app.rag.index import IndexStore
from app.runner import make_context, run_turn

st.set_page_config(page_title="电商客服 Agent", layout="wide")

NOT_LOGGED_IN = "未登录"
# 下拉框的选项和 AppContext 里 fault 取值的对应关系
FAULT_LABELS = ["关闭", "主模型不可用", "主备都不可用"]
FAULT_VALUES = {label: value for label, value in zip(FAULT_LABELS, config.FAULTS)}

# 右栏刷新的位置：跑图过程中由 drive 逐条刷新
_PLACEHOLDER = None


def customers() -> list[dict]:
    """客户列表，用来填「当前登录客户」下拉框。"""
    path = config.STRUCTURED_DIR / "customers.json"
    return json.loads(path.read_text(encoding="utf-8"))


def customer_options() -> list[str]:
    """下拉框的选项：第一项是未登录，后面是全部客户，显示成「编号 · 名字」。"""
    return [NOT_LOGGED_IN] + [f"{c['customer_id']} · {c['name']}" for c in customers()]


def customer_id_of(option: str) -> str | None:
    """选项里 · 前面的客户编号；选未登录时是 None。"""
    return None if option == NOT_LOGGED_IN else option.split(" · ", 1)[0]


def fault_value() -> str:
    """模拟故障下拉框对应的 fault 取值。"""
    label = st.session_state["fault"] if "fault" in st.session_state else FAULT_LABELS[0]
    return FAULT_VALUES[label]


def reset_session() -> None:
    """开一个新会话：换新图和它的检查点、换会话编号、清空记录。"""
    st.session_state.graph = build_graph(checkpointer=InMemorySaver())
    st.session_state.thread_id = uuid.uuid4().hex[:8]
    st.session_state.turns = []
    st.session_state.selected = None


def new_turn(text: str) -> dict:
    """一轮对话的初始记录。"""
    return {"user": text, "steps": [], "reply": None, "sources": [], "card": None,
            "pending": None, "decision": None, "preview": None}


def turn_index() -> tuple[int | None, int]:
    """右栏现在该显示哪一轮：selected 是 None 时是最后一轮。"""
    turns = st.session_state.turns
    if not turns:
        return None, 0
    index = len(turns) - 1 if st.session_state.selected is None else min(int(st.session_state.selected), len(turns) - 1)
    return index, index


def step_title(step: dict) -> str:
    """折叠块的标题：图标 + 步骤名 + 摘要的第一行（太长就截断）。"""
    if step["notes"]:
        icon = "🟡"
    elif step["kind"] == "model":
        icon = "🟣"
    else:
        icon = "⚪"
    first = str(step["summary"]).split("\n")[0]
    first = first.replace("`", "")      # 标题当 Markdown 渲染，反引号会变成代码样式
    if len(first) > 60:
        first = first[:60] + "…"
    return f"{icon} {step['name']} — {first}"


def render_steps(container, turn: dict | None, index: int | None) -> None:
    """右栏：图例加选中那一轮的步骤，每个步骤一个可展开的块。"""
    container.subheader("执行过程")
    container.caption("🟣 模型   ⚪ 确定性代码   🟡 发生了重试或降级")
    if turn is None:
        container.caption("还没有对话")
        return

    container.caption(f"第 {index + 1} 轮: {turn['user']}")
    for step in turn["steps"]:
        with container.expander(step_title(step)):
            st.code(step["summary"], language=None)
            for note in step["notes"]:
                st.caption("↻ " + note)
            if step["detail"] is not None:
                st.json(step["detail"])

    model_steps = sum(1 for step in turn["steps"] if step["kind"] == "model")
    container.caption(f"有模型参与的步骤: {model_steps} 个")


def draw_steps() -> None:
    """整块重画右栏。"""
    if _PLACEHOLDER is None:
        return
    _PLACEHOLDER.empty()
    index, _ = turn_index()
    turn = None if index is None else st.session_state.turns[index]
    render_steps(_PLACEHOLDER.container(), turn, index)


def render_preview(preview: dict) -> None:
    """取消订单的确认卡片，内容全部来自预览。"""
    with st.container(border=True):
        st.markdown(f"**{preview['action_label']}**")
        st.write(f"订单 {preview['order_id']} · 下单 {preview['order_date']} · 状态 {preview['status']}")
        for item in preview["items"]:
            st.write(f"{item['name']} × {item['quantity']}   ${float(item['price_per_unit']):.2f}")
        st.write(f"合计 ${float(preview['total_amount']):.2f}")
        st.warning(preview["effect"])


def render_ticket(card: dict) -> None:
    """转人工的工单卡片。"""
    with st.container(border=True):
        st.markdown(f"**人工工单 {card['ticket_id']}**")
        st.write(f"转接原因: {card['reason_label']}")
        st.write(f"涉及订单: {', '.join(card['order_ids']) if card['order_ids'] else '无'}")
        st.write("已附带的对话摘要:")
        st.write(card["summary"])


def drive(turn: dict, text: str | None = None, resume=None) -> None:
    """跑一轮或从断点恢复：事件依次落到 turn 上，步骤逐条刷新右栏。"""
    ctx = make_context(customer_id_of(st.session_state["customer"]), None, fault_value(),
                       session_id=st.session_state.thread_id)
    turns = st.session_state.turns
    index = next((i for i, item in enumerate(turns) if item is turn), None)
    for kind, payload in run_turn(st.session_state.graph, ctx, st.session_state.thread_id,
                                  text=text, resume=resume):
        if kind == "step":
            turn["steps"].append(payload)
            if _PLACEHOLDER is not None and index is not None:
                _PLACEHOLDER.empty()
                render_steps(_PLACEHOLDER.container(), turn, index)
        elif kind == "interrupt":
            turn["pending"] = payload
        elif kind == "final":
            if turn["pending"] is None:
                turn["reply"] = payload.get("answer")
                turn["sources"] = payload.get("sources") or []
                turn["card"] = payload.get("card")


def finish(turn: dict, text: str | None = None, resume=None) -> bool:
    """跑图，异常显示出来而不是让页面崩掉。返回是否顺利跑完。"""
    try:
        drive(turn, text=text, resume=resume)
        return True
    except Exception as exc:                       # 界面不吞异常，但要让用户看到
        st.error(f"运行出错: {exc}")
        return False


def resolve_cancel(turn: dict, confirm: bool) -> bool:
    """用户在确认卡片上做了选择：记下决定，从断点接着跑这一轮。"""
    preview = turn["pending"]
    turn["pending"] = None
    turn["preview"] = preview
    turn["decision"] = "确认取消" if confirm else "不取消"
    return finish(turn, resume={"confirm": confirm, "order_id": preview["order_id"]})


def render_turn(turn: dict, index: int) -> None:
    """聊天窗口里一轮的内容：用户消息、回复、引用来源、卡片。"""
    st.chat_message("user").write(turn["user"])
    with st.chat_message("assistant"):
        if turn["preview"] is not None:
            render_preview(turn["preview"])
            st.write(f"已选择：{turn['decision']}")
        if turn["pending"] is not None:
            render_preview(turn["pending"])
            left, right = st.columns(2)
            confirm = left.button("确认取消", key=f"confirm_{index}")
            decline = right.button("不取消", key=f"decline_{index}")
            if confirm or decline:
                if resolve_cancel(turn, bool(confirm)):
                    st.rerun()
        if turn["reply"] is not None:
            st.write(turn["reply"])
        if turn["sources"]:
            st.caption("引用来源: " + ", ".join(turn["sources"]))
        card = turn["card"]
        if card and card.get("type") == "ticket":
            render_ticket(card)
        if st.button("查看这一轮的步骤", key=f"show_{index}"):
            st.session_state.selected = index
            st.rerun()


if "turns" not in st.session_state:
    reset_session()

# ---- 侧边栏：设置和运行时信息 ----
st.sidebar.title("电商客服 Agent")

st.sidebar.selectbox("当前登录客户", customer_options(), index=1, key="customer",
                     on_change=reset_session)
st.sidebar.selectbox("模拟故障", FAULT_LABELS, key="fault")

pointer = IndexStore().read_pointer()
st.sidebar.caption(f"今天: {config.DEFAULT_TODAY}")
st.sidebar.caption(f"知识库线上版本: {pointer['current'] if pointer else '无'}")
st.sidebar.caption(f"会话: {st.session_state.thread_id}")

if st.sidebar.button("新会话", key="new_session"):
    reset_session()
    st.rerun()

# ---- 中间聊天窗口 + 右边执行过程 ----
chat_col, steps_col = st.columns([3, 2])
_PLACEHOLDER = steps_col.empty()

turns = st.session_state.turns
with chat_col:
    for i, turn in enumerate(turns):
        render_turn(turn, i)
    prompt = st.chat_input("输入消息…", key="chat_input",
                           disabled=bool(turns) and turns[-1]["pending"] is not None)

draw_steps()

if prompt:
    turn = new_turn(prompt)
    st.session_state.turns.append(turn)
    st.session_state.selected = None
    with chat_col:
        st.chat_message("user").write(prompt)
    draw_steps()
    if finish(turn, text=prompt):
        st.rerun()
