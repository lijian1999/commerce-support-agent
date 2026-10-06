"""app.models 的单元测试：不调用真实模型，用假的可调用对象模拟成功、失败和并发。"""
import threading
import time

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from app import config
from app.models import (
    ModelCallError,
    ModelError,
    ModelUnavailable,
    RoleModel,
    is_transient,
    resolve_targets,
)


class _Fake:
    """假的可调用对象：按结果序列依次抛出异常或返回结果，并记录调用次数。"""

    def __init__(self, *results: object):
        self.results = list(results)
        self.calls = 0

    def invoke(self, messages: list) -> object:
        item = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        if isinstance(item, BaseException):
            raise item
        return item


class _Sleep:
    """记录退避时长的假 sleep，测试里不真的等待。"""

    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, delay: float) -> None:
        self.delays.append(delay)


class _Answer(BaseModel):
    text: str


def _model(primary: _Fake, fallback: _Fake | None = None,
           sleep: _Sleep | None = None) -> RoleModel:
    targets: list[tuple[str, object]] = [("主模型", primary)]
    if fallback is not None:
        targets.append(("备用模型", fallback))
    return RoleModel("router", targets, sleep=sleep or _Sleep())


def test_first_try_success():
    answer = object()
    sleep = _Sleep()
    model = _model(_Fake(answer), sleep=sleep)

    result = model.invoke([("user", "你好")])

    assert result.value is answer
    assert result.notes == []
    assert sleep.delays == []


def test_value_extraction():
    assert _model(_Fake(AIMessage(content="你好"))).invoke([]).value == "你好"

    answer = _Answer(text="你好")
    assert _model(_Fake(answer)).invoke([]).value is answer


def test_one_retry_then_success():
    sleep = _Sleep()
    primary, fallback = _Fake(ConnectionError("x"), "主模型的回答"), _Fake("备用模型的回答")
    result = _model(primary, fallback, sleep).invoke([])

    assert result.value == "主模型的回答"
    assert result.notes == [
        "主模型第 1 次调用失败（ConnectionError），0.5 秒后重试",
        "主模型调用成功",
    ]
    assert sleep.delays == [0.5]
    assert fallback.calls == 0


def test_primary_exhausted_then_fallback():
    sleep = _Sleep()
    primary, fallback = _Fake(ConnectionError("x")), _Fake("备用模型的回答")
    result = _model(primary, fallback, sleep).invoke([])

    assert result.notes == [
        "主模型第 1 次调用失败（ConnectionError），0.5 秒后重试",
        "主模型第 2 次调用失败（ConnectionError），1.0 秒后重试",
        "主模型第 3 次调用失败（ConnectionError），重试 2 次后放弃",
        "切换到备用模型",
        "备用模型调用成功",
    ]
    assert primary.calls == 3
    assert fallback.calls == 1
    assert sleep.delays == [0.5, 1.0]


def test_all_targets_exhausted():
    primary, fallback = _Fake(ConnectionError("x")), _Fake(ConnectionError("x"))
    with pytest.raises(ModelUnavailable) as exc_info:
        _model(primary, fallback).invoke([])

    assert primary.calls == 3
    assert fallback.calls == 3
    assert len(exc_info.value.notes) == 7
    assert exc_info.value.notes[-1] == "备用模型第 3 次调用失败（ConnectionError），重试 2 次后放弃"


def test_non_transient_raises_immediately():
    sleep = _Sleep()
    primary, fallback = _Fake(ValueError("x")), _Fake("备用模型的回答")
    with pytest.raises(ModelCallError) as exc_info:
        _model(primary, fallback, sleep).invoke([])

    assert primary.calls == 1
    assert fallback.calls == 0
    assert sleep.delays == []
    assert exc_info.value.notes == ["主模型调用出错（ValueError），这类错误不重试"]


def test_single_target_exhausted():
    primary = _Fake(ConnectionError("x"))
    with pytest.raises(ModelUnavailable) as exc_info:
        _model(primary).invoke([])

    assert primary.calls == 3
    assert not [n for n in exc_info.value.notes if "切换到" in n]


def test_exception_hierarchy():
    assert issubclass(ModelUnavailable, ModelError)
    assert issubclass(ModelCallError, ModelError)


def test_is_transient():
    assert is_transient(TimeoutError()) is True
    assert is_transient(ConnectionError()) is True
    assert is_transient(ValueError()) is False
    assert is_transient(KeyError()) is False
    request = httpx.Request("POST", "http://x")
    assert is_transient(openai.APIConnectionError(request=request)) is True


def test_resolve_targets_off():
    targets = resolve_targets("router", "off")

    assert targets[0][0] == "主模型"
    assert targets[0][1].base_url == config.role_target("router").base_url


def test_resolve_targets_primary_down():
    targets = resolve_targets("router", "primary_down")

    assert targets[0][1].base_url == config.UNREACHABLE_BASE_URL
    assert targets[1][1].base_url != config.UNREACHABLE_BASE_URL


def test_resolve_targets_all_down():
    targets = resolve_targets("router", "all_down")

    assert len(targets) >= 1
    for _, target in targets:
        assert target.base_url == config.UNREACHABLE_BASE_URL


def test_resolve_targets_bad_args():
    with pytest.raises(ValueError):
        resolve_targets("nobody")
    with pytest.raises(ValueError):
        resolve_targets("router", "bad")


def test_concurrency_limit():
    lock = threading.Lock()
    seen = {"current": 0, "max": 0}

    class _Slow:
        def invoke(self, messages: list) -> str:
            with lock:
                seen["current"] += 1
                seen["max"] = max(seen["max"], seen["current"])
            time.sleep(0.05)
            with lock:
                seen["current"] -= 1
            return "ok"

    slow = _Slow()
    model = RoleModel("router", [("主模型", slow), ("备用模型", slow)])
    threads = [threading.Thread(target=model.invoke, args=([("user", "你好")],))
               for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert seen["max"] <= config.MODEL_MAX_CONCURRENCY
    assert seen["max"] > 1
