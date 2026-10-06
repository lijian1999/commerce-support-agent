# commerce-support-agent · 工作约定

你是执行者。任务说明由编排者（Claude）给出，完成后由编排者独立验收，所以：
- 严格按任务说明做，不要做说明之外的功能，不要改说明没提到的文件。
- 说明里写的接口、字段名、文件路径必须一字不差。
- 说明有歧义时，按 `docs/contracts.md` 判断；还判断不了再看 `docs/design.md` 的对应小节。
- 完成前必须亲自运行说明里列出的检查命令，全部通过才算完成。不能跳过测试、不能为了通过而删改测试断言。
- 最后一行只输出 `DONE` 或 `FAILED: <原因>`。

## 工作方式

- 任务说明里写了“第一步”“第二步”的，按顺序做，每一步做完就运行那一步给出的命令。
- 说明里有歧义或自相矛盾的地方：按最直接的理解做，继续往下，最后在结果里写明你是怎么理解的。
- 一项检查试了两次仍然通不过、又判断不了是代码的问题还是说明的问题：停下来，最后一行写 `FAILED: <卡在哪里>`，由编排者处理。

## 环境

- 代码和程序都在这台 Mac 上，项目根目录就是当前目录。
- Python 3.13，依赖用 uv 管理。所有 Python 命令都用 `uv run` 执行，例如 `uv run pytest tests/test_x.py -q`、`uv run python scripts/x.py`。
- 依赖已经装好：langgraph 1.2、langchain 1.4、langchain-openai、langsmith、chromadb 1.5、pymysql、pydantic 2、python-dotenv、streamlit、pytest。不要安装或升级任何依赖；觉得缺依赖时在结果里说明。
- MySQL 8.4 跑在 Docker 里，地址 `127.0.0.1:3307`，库名 `techhub`。连接参数从 `app/config.py` 取。
- 模型和 Embedding 服务在局域网的另一台机器上，地址从 `app/config.py` 取。

## 目录

| 路径 | 内容 |
|---|---|
| `app/` | 图的全部逻辑 |
| `app/config.py`、`app/context.py`、`app/state.py` | 配置、Runtime Context、状态和数据结构。**只读**，由编排者维护 |
| `cli.py`、`ui.py` | 命令行入口、Streamlit 界面 |
| `scripts/` | 导入 MySQL、构建和切换知识库索引 |
| `tests/` | 单元测试（pytest） |
| `data/structured/`、`data/documents/` | 数据源文件，**只读** |
| `data/index/` | 知识库索引，由脚本生成 |
| `docs/design.md` | 设计文档，**只读** |
| `docs/contracts.md` | 模块之间的接口约定，**只读** |
| `docs/tasks/` | 任务说明，**只读** |

## 技术约束

- 配置只从 `app.config` 取，不要在别处读环境变量，不要在代码里写死地址和密码。
- 数据结构（`Step`、`Task`、`TaskResult`、`TaskPayload`、`State`）只用 `app/state.py` 里定义的，不要另外定义同样用途的结构。
- 每个图节点返回的更新里都要带 `steps`：一个只含一条 `make_step(...)` 记录的列表。
- 模型只通过 `app.models.get_model` 调用。不给任何模型绑定工具（不使用 `bind_tools`）。
- SQL 一律参数化（`%s` 占位符加参数列表），不用 f-string 或字符串拼接把值放进 SQL。
- 单元测试不调用模型，不连 LangSmith。需要数据库的测试在说明里会写明。

## 绝对不能做的事

- 不要动名为 `mysql` 的 Docker 容器和 3306 端口，那是别的项目的。本项目的容器叫 `csa-mysql`，端口 3307。
- 不要执行 `git` 的写操作（add、commit、checkout、reset、stash 等），版本管理由编排者负责。
- 不要修改 `.env`、`pyproject.toml`、`uv.lock`。
- 不要删除或改写标为只读的文件。需要改它们时在结果里说明，由编排者处理。

## 风格

- 注释、文档字符串、打印给用户看的文字用中文；标识符、字段名用英文。
- 代码保持简短直白：这是一个学习型项目，不需要为假想的扩展预留抽象层。
- 函数写类型标注。
