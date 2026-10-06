# 任务 05：模型工厂（重试、降级、并发上限、模拟故障）

先读 `AGENTS.md`，再读 `docs/contracts.md` 的第 3 节。本任务新建这些文件：`app/models.py`、`tests/test_models.py`。不要改别的文件。

## 背景

图里所有的模型调用都经过 `get_model(role, schema, fault)`。它把一条降级阶梯集中在一处：先调主模型，瞬时错误就带退避重试；重试用尽后换备用模型，同样重试；都失败就抛 `ModelUnavailable`，由调用它的节点转人工。整个过程里发生的重试和降级要记成一串文字（`notes`）返回，用来在终端和界面上展示。

角色和地址的配置都在 `app.config` 里：`ROLES`、`FAULTS`、`ModelTarget`、`role_target(role)`、`role_fallback_target(role)`、`MODEL_TIMEOUT_S`、`MODEL_MAX_TOKENS`、`MODEL_MAX_RETRIES`、`MODEL_RETRY_BACKOFF_S`、`MODEL_MAX_CONCURRENCY`、`UNREACHABLE_BASE_URL`。

## 第一步：异常、结果、瞬时错误的判断

```python
class ModelError(Exception):
    def __init__(self, message: str, notes: list[str]):
        super().__init__(message)
        self.notes = notes

class ModelUnavailable(ModelError): ...
class ModelCallError(ModelError): ...

@dataclass
class ModelResult:
    value: Any
    notes: list[str]

def is_transient(exc: BaseException) -> bool
```

`is_transient` 返回 `True` 的情况（其余都是 `False`）：
- `openai.APITimeoutError`、`openai.APIConnectionError`、`openai.RateLimitError`、`openai.InternalServerError` 的实例；
- `openai.APIStatusError` 的实例且 `status_code >= 500`；
- Python 内置的 `TimeoutError`、`ConnectionError` 的实例。

## 第二步：`RoleModel`

```python
class RoleModel:
    def __init__(self, role: str, targets: list[tuple[str, Any]],
                 max_retries: int = config.MODEL_MAX_RETRIES,
                 backoff: tuple[float, ...] = config.MODEL_RETRY_BACKOFF_S,
                 sleep: Callable[[float], None] = time.sleep): ...
    def invoke(self, messages: list) -> ModelResult: ...
```

- `targets` 是 `[(标签, 可调用对象), ...]`，按顺序是主模型、备用模型（可以只有一个）。「可调用对象」只要求有 `.invoke(messages)` 方法。
- 模块级有一个 `threading.BoundedSemaphore(config.MODEL_MAX_CONCURRENCY)`，每一次 `.invoke(messages)` 调用都在 `with 信号量:` 里进行（退避等待的 `sleep` 不占信号量）。

`invoke` 的行为就是下面这段代码：

```python
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
```

`_extract(output)`：`output` 是 `langchain_core.messages.BaseMessage` 的实例时返回 `str(output.content)`，否则原样返回。

## 第三步：`resolve_targets` 和 `get_model`

```python
def resolve_targets(role: str, fault: str = "off") -> list[tuple[str, ModelTarget]]
def get_model(role: str, schema: type[BaseModel] | None = None, fault: str = "off") -> RoleModel
```

- `resolve_targets`：`role` 不在 `config.ROLES` 里、或 `fault` 不在 `config.FAULTS` 里 → `ValueError`。
  - 结果的第一项是 `("主模型", config.role_target(role))`；`config.role_fallback_target(role)` 不是 `None` 时第二项是 `("备用模型", 它)`。
  - `fault == "primary_down"`：主模型的 `base_url` 换成 `config.UNREACHABLE_BASE_URL`（用 `dataclasses.replace`）。
  - `fault == "all_down"`：主模型和备用模型的 `base_url` 都换成 `config.UNREACHABLE_BASE_URL`。
- `get_model`：对 `resolve_targets(role, fault)` 的每一项建一个聊天模型：

```python
chat = ChatOpenAI(
    base_url=target.base_url, api_key=target.api_key, model=target.model,
    temperature=0, max_retries=0, timeout=config.MODEL_TIMEOUT_S, max_tokens=config.MODEL_MAX_TOKENS,
    extra_body={"chat_template_kwargs": {"enable_thinking": False}},
)
```

  `schema` 不是 `None` 时再包一层 `chat.with_structured_output(schema, method="json_schema")`。结构化输出要分别绑到主模型和备用模型上，然后才组成阶梯。返回 `RoleModel(role, [(标签, 可调用对象), ...])`。`max_retries=0` 是为了让重试只发生在 `RoleModel` 里。不绑定任何工具。

```sh
uv run python -c "
from app.models import get_model
r = get_model('router').invoke([('user', '只回复两个字：你好')])
print(type(r.value).__name__, r.notes)
r = get_model('router', fault='primary_down').invoke([('user', '只回复两个字：你好')])
print(len(r.notes), r.notes[0], '|', r.notes[-2], '|', r.notes[-1])"
```

应输出两行：

```
str []
5 主模型第 1 次调用失败（APIConnectionError），0.5 秒后重试 | 切换到备用模型 | 备用模型调用成功
```

## 第四步：`tests/test_models.py`

测试不调用真实的模型。写一个假的可调用对象：构造时给一个「结果序列」，每次 `.invoke` 依次取一个，取到的是异常实例就抛出、否则返回它；并记录被调用的次数。`RoleModel` 的 `sleep` 传一个把参数记进列表的函数，这样测试不真的等待。瞬时错误用 `ConnectionError("x")`，非瞬时错误用 `ValueError("x")`。

每条一个测试函数：

1. 一次成功：`value` 是假对象返回的值，`notes == []`，没有调用 `sleep`。
2. 返回值是 `AIMessage(content="你好")` 时，`value == "你好"`；返回值是一个 pydantic 实例时，`value` 就是这个实例。
3. 主模型失败 1 次后成功：`notes == ["主模型第 1 次调用失败（ConnectionError），0.5 秒后重试", "主模型调用成功"]`，`sleep` 收到的参数列表是 `[0.5]`，备用模型没有被调用。
4. 主模型 3 次都失败、备用模型成功：`notes` 恰好是这 5 条——`"主模型第 1 次调用失败（ConnectionError），0.5 秒后重试"`、`"主模型第 2 次调用失败（ConnectionError），1.0 秒后重试"`、`"主模型第 3 次调用失败（ConnectionError），重试 2 次后放弃"`、`"切换到备用模型"`、`"备用模型调用成功"`；主模型被调用 3 次，备用模型 1 次；`sleep` 收到 `[0.5, 1.0]`。
5. 主模型和备用模型都一直失败：抛 `ModelUnavailable`，两个各被调用 3 次，异常的 `notes` 有 7 条，最后一条是 `"备用模型第 3 次调用失败（ConnectionError），重试 2 次后放弃"`。
6. 非瞬时错误：主模型抛 `ValueError` → 立刻抛 `ModelCallError`，主模型只被调用 1 次，备用模型 0 次，没有调用 `sleep`，异常的 `notes == ["主模型调用出错（ValueError），这类错误不重试"]`。
7. 只有主模型一个目标且一直失败：抛 `ModelUnavailable`，被调用 3 次，`notes` 里没有「切换到」。
8. `ModelUnavailable` 和 `ModelCallError` 都是 `ModelError` 的子类。
9. `is_transient`：`TimeoutError()`、`ConnectionError()` → `True`；`ValueError()`、`KeyError()` → `False`；`openai.APIConnectionError(request=httpx.Request("POST", "http://x"))` → `True`。
10. `resolve_targets("router", "off")`：第一项标签是 `主模型`，它的 `base_url` 等于 `config.role_target("router").base_url`。
11. `resolve_targets("router", "primary_down")`：主模型的 `base_url` 是 `config.UNREACHABLE_BASE_URL`，备用模型的 `base_url` 不是。
12. `resolve_targets("router", "all_down")`：每一项的 `base_url` 都是 `config.UNREACHABLE_BASE_URL`。
13. `resolve_targets("nobody")` 和 `resolve_targets("router", "bad")` 都抛 `ValueError`。
14. 并发上限：假对象的 `.invoke` 里把「当前在途数」加 1、记录见过的最大值、`time.sleep(0.05)`、再减 1（计数用锁保护）。开 12 个线程同时对同一个 `RoleModel` 调 `invoke`，全部结束后断言见过的最大在途数不超过 `config.MODEL_MAX_CONCURRENCY`，且大于 1。

```sh
uv run pytest tests/test_models.py -q
```

应输出 `14 passed`。

## 最后一步：收尾检查

重新运行第三步的命令和 `uv run pytest tests/test_models.py -q`。通过的标准：第三步的命令输出和预期的两行一致；pytest 输出 `14 passed`。全部通过后最后一行输出 `DONE`。
