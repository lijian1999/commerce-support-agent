"""Streamlit 界面的测试：用 Streamlit 自带的测试工具，会调用真实的模型和数据库。"""
import json
from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
CUSTOMERS = json.loads((ROOT / "data" / "structured" / "customers.json").read_text(encoding="utf-8"))


def new_app():
    """打开界面，返回跑完第一遍的 AppTest。"""
    # AppTest 按调用者所在的目录解析相对路径，这里显式指到项目根目录的 ui.py
    return AppTest.from_file(str(ROOT / "ui.py"), default_timeout=120).run()


def say(at, text: str):
    """在输入框里发一句话。"""
    at.chat_input[0].set_value(text).run()
    return at


def test_page_opens() -> None:
    """页面能打开，侧边栏的设置项齐全。"""
    at = new_app()
    assert not at.exception
    assert len(at.sidebar.selectbox) == 2
    assert str(at.sidebar.selectbox[0].value).startswith("CUST-001")
    assert list(at.sidebar.selectbox[1].options) == ["关闭", "主模型不可用", "主备都不可用"]
    assert list(at.sidebar.selectbox[0].options)[0] == "未登录"
    assert len(at.sidebar.selectbox[0].options) == 51


def test_small_talk() -> None:
    """闲聊：一轮下来有回复，步骤是 router 和 answer。"""
    at = say(new_app(), "你好")
    assert not at.exception
    assert len(at.session_state.turns) == 1
    turn = at.session_state.turns[0]
    assert isinstance(turn["reply"], str) and turn["reply"] != ""
    assert [step["name"] for step in turn["steps"]] == ["router", "answer"]


def test_new_session() -> None:
    """新会话：对话记录清空，会话编号换掉。"""
    at = say(new_app(), "你好")
    thread_id = at.session_state.thread_id
    at.sidebar.button(key="new_session").click().run()
    assert not at.exception
    assert at.session_state.turns == []
    assert at.session_state.thread_id != thread_id


def test_cancel_declined() -> None:
    """取消订单但选择「不取消」：停在确认上，恢复后不执行取消。"""
    name = next(c["name"] for c in CUSTOMERS if c["customer_id"] == "CUST-002")
    at = new_app()
    at.sidebar.selectbox[0].select(f"CUST-002 · {name}").run()
    say(at, "帮我取消订单 ORD-2025-0031")
    assert not at.exception

    turn = at.session_state.turns[-1]
    assert turn["pending"] is not None
    assert turn["pending"]["order_id"] == "ORD-2025-0031"
    assert turn["reply"] is None

    at.button(key="decline_0").click().run()
    assert not at.exception

    turn = at.session_state.turns[-1]
    assert turn["pending"] is None
    assert turn["decision"] == "不取消"
    assert turn["reply"] == "好的，订单 ORD-2025-0031 没有取消。"
    names = [step["name"] for step in turn["steps"]]
    assert names[-5:] == ["router", "cancel/extract", "cancel/check", "cancel/confirm", "cancel/reply"]
    assert "cancel/execute" not in names
