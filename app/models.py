"""模型工厂：所有模型调用都经过 get_model，集中处理重试、降级、并发上限和模拟故障。"""
import dataclasses
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import openai
from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from app import config
from app.config import ModelTarget

# 限制同时在途的模型调用数量，保护推理后端
_SEMAPHORE = threading.BoundedSemaphore(config.MODEL_MAX_CONCURRENCY)


class ModelError(Exception):
    """模型调用失败的基类，notes 记录到失败为止的重试、降级过程。"""

    def __init__(self, message: str, notes: list[str]):
        super().__init__(message)
        self.notes = notes


class ModelUnavailable(ModelError):
    """主模型和备用模型都因瞬时错误失败。"""


class ModelCallError(ModelError):
    """不该重试的错误（参数错误、输出解析失败等）。"""


@dataclass
class ModelResult:
    """一次模型调用的结果：value 是回复文本或结构化对象，notes 是重试、降级记录。"""

    value: Any
    notes: list[str]


def is_transient(exc: BaseException) -> bool:
    """判断异常是不是值得重试的瞬时错误。"""
    if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError,
                        openai.RateLimitError, openai.InternalServerError)):
        return True
    if isinstance(exc, openai.APIStatusError) and exc.status_code >= 500:
        return True
    return isinstance(exc, (TimeoutError, ConnectionError))


def _extract(output: Any) -> Any:
    """把模型返回值统一成 str 或结构化的 pydantic 实例。"""
    if isinstance(output, BaseMessage):
        return str(output.content)
    return output


class RoleModel:
    """某个角色的模型降级阶梯：主模型重试若干次，仍失败就换备用模型。"""

    def __init__(self, role: str, targets: list[tuple[str, Any]],
                 max_retries: int = config.MODEL_MAX_RETRIES,
                 backoff: tuple[float, ...] = config.MODEL_RETRY_BACKOFF_S,
                 sleep: Callable[[float], None] = time.sleep):
        self.role = role
        self.targets = targets
        self.max_retries = max_retries
        self.backoff = backoff
        self.sleep = sleep

    def invoke(self, messages: list) -> ModelResult:
        notes: list[str] = []
        for index, (label, runnable) in enumerate(self.targets):
            for attempt in range(self.max_retries + 1):
                try:
                    with _SEMAPHORE:
                        output = runnable.invoke(messages)
                except Exception as exc:
                    name = type(exc).__name__
                    if not is_transient(exc):
                        notes.append(f"{label}调用出错（{name}），这类错误不重试")
                        raise ModelCallError(f"{label}调用出错: {name}: {exc}", notes) from exc
                    if attempt < self.max_retries:
                        delay = self.backoff[min(attempt, len(self.backoff) - 1)]
                        notes.append(f"{label}第 {attempt + 1} 次调用失败（{name}），{delay} 秒后重试")
                        self.sleep(delay)
                    else:
                        notes.append(f"{label}第 {attempt + 1} 次调用失败（{name}），重试 {self.max_retries} 次后放弃")
                else:
                    if notes:
                        notes.append(f"{label}调用成功")
                    return ModelResult(value=_extract(output), notes=notes)
            if index + 1 < len(self.targets):
                notes.append(f"切换到{self.targets[index + 1][0]}")
        raise ModelUnavailable(f"角色 {self.role} 的模型全部不可用", notes)


def resolve_targets(role: str, fault: str = "off") -> list[tuple[str, ModelTarget]]:
    """解析某个角色的目标模型列表，并按 fault 注入故障。"""
    if role not in config.ROLES:
        raise ValueError(f"未知的模型角色: {role}")
    if fault not in config.FAULTS:
        raise ValueError(f"未知的故障类型: {fault}")

    primary = config.role_target(role)
    fallback = config.role_fallback_target(role)
    if fault == "primary_down":
        primary = dataclasses.replace(primary, base_url=config.UNREACHABLE_BASE_URL)
    elif fault == "all_down":
        primary = dataclasses.replace(primary, base_url=config.UNREACHABLE_BASE_URL)
        if fallback is not None:
            fallback = dataclasses.replace(fallback, base_url=config.UNREACHABLE_BASE_URL)

    targets: list[tuple[str, ModelTarget]] = [("主模型", primary)]
    if fallback is not None:
        targets.append(("备用模型", fallback))
    return targets


def get_model(role: str, schema: type[BaseModel] | None = None, fault: str = "off") -> RoleModel:
    """按角色建模型。schema 不为 None 时给每个目标模型单独绑结构化输出。"""
    targets: list[tuple[str, Any]] = []
    for label, target in resolve_targets(role, fault):
        chat = ChatOpenAI(
            base_url=target.base_url, api_key=target.api_key, model=target.model,
            temperature=0, max_retries=0, timeout=config.MODEL_TIMEOUT_S, max_tokens=config.MODEL_MAX_TOKENS,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        if schema is not None:
            chat = chat.with_structured_output(schema, method="json_schema")
        targets.append((label, chat))
    return RoleModel(role, targets)
