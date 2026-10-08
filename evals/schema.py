"""评测的数据结构：Case 文件的格式、一次运行的记录、一项检查的结果。评测的所有模块共用，字段名以这里为准。"""
from pathlib import Path
from typing import Any, Literal, TypedDict

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.config import DEFAULT_TODAY, ROOT

EVALS_DIR = ROOT / "evals"
CASES_DIR = EVALS_DIR / "cases"
RUNS_DIR = EVALS_DIR / "runs"                 # 原始运行记录，不进 Git
REPORTS_DIR = EVALS_DIR / "reports"
MANIFEST_PATH = EVALS_DIR / "manifest.json"
POISON_DOCS_DIR = EVALS_DIR / "poison_docs"
POISONED_INDEX_DIR = EVALS_DIR / "index_poisoned"   # 带诱导文档的索引，不进 Git

SESSION_PREFIX = "eval-"                      # 评测会话编号的前缀，清理工单和取消记录时按它找
RETRIEVAL_K = 6                               # 检索只看前 6 条，等于 app.config.RAG_TOP_K

ROUTES = ("sql", "rag", "supervisor", "cancel", "handoff", "direct")
# 一轮的结局，由代码从运行记录推出来（见 docs/eval-contracts.md 第 4 节）
OUTCOMES = ("answered", "partial", "login_required", "declined", "handoff",
            "cancel:cancelled", "cancel:declined", "cancel:need_order_id", "cancel:not_found",
            "cancel:not_cancellable", "cancel:already_cancelled", "cancel:login_required",
            "cancel:mismatch", "cancel:error", "error")
# 出错环节的先后顺序：一轮失败时，取最靠前的那个作为「第一处出错的环节」
STAGES = ("router", "plan", "sql", "retrieval", "answer", "turn")


class Strict(BaseModel):
    """Case 文件里写错字段名时直接报错，不悄悄忽略。"""
    model_config = ConfigDict(extra="forbid")


class RouteExpect(Strict):
    prefer: str                                   # 首选路由
    accept: list[str] = Field(default_factory=list)   # 其他可以接受的路由；prefer 本身总是可接受的


class PlanExpect(Strict):
    agents: list[str] = Field(default_factory=list)       # 计划里至少要有的任务类型，可重复，如 ["sql", "rag"]
    depends: list[list[str]] = Field(default_factory=list)  # 必须存在的依赖，如 [["rag", "sql"]] 表示有一个 rag 任务依赖一个 sql 任务
    max_tasks: int | None = None                  # 任务数上限


class SqlExpect(Strict):
    # 本轮查库任务的结局：ok（全部成功）/ login_required / unanswerable / failed；写列表表示都接受
    result: str | list[str] = "ok"
    rows_contain: list[str | float | int] = Field(default_factory=list)   # 结果行里必须出现的值
    rows_exclude: list[str] = Field(default_factory=list)                 # 结果行里不能出现的值
    row_count: int | None = None                  # 结果行数（本轮所有成功的查库任务合计）
    reference_sql: str | None = None              # 参考 SQL，只用于检查标注有没有过期


class Relevant(Strict):
    doc: str
    section: str | None = None                    # None 表示这篇文档的任意小节


class RetrievalExpect(Strict):
    mode: Literal["any", "all"] = "any"           # 命中任意一个算对 / 要全部命中
    relevant: list[Relevant]
    precondition: bool = False                    # True：这是 Case 成立的前提（诱导片段必须被检索到）


class Fact(Strict):
    text: str                                     # 要在回答里出现的事实，用中文写
    quote: str | None = None                      # 文档原文的摘录，只用于检查标注有没有过期


class AnswerExpect(Strict):
    literals: list[str] = Field(default_factory=list)            # 回答里必须字面出现的值（订单号、金额、运单号）
    literals_exclude: list[str] = Field(default_factory=list)    # 回答里不能字面出现的值
    redline_literals: list[str] = Field(default_factory=list)    # 同上，但出现就是红线（别人的数据）
    facts: list[Fact | str] = Field(default_factory=list)        # 关键事实，模型判分
    must_not: list[str] = Field(default_factory=list)            # 禁止的行为，模型判分
    redline: list[str] = Field(default_factory=list)             # 同上，但违反就是红线


class TraceExpect(Strict):
    max_sql: int | None = None                    # execute_sql 的次数上限
    max_model_steps: int | None = None            # 有模型参与的步骤数上限
    forbid_steps: list[str] = Field(default_factory=list)    # 不能出现的步骤名，支持 * 通配，如 "cancel/*"
    require_steps: list[str] = Field(default_factory=list)   # 必须出现的步骤名


class TurnExpect(Strict):
    route: RouteExpect | None = None
    outcome: str | list[str] | None = None        # OUTCOMES 里的值；写列表表示都接受
    handoff_reason: str | None = None             # user_request / out_of_scope / system_failure
    plan: PlanExpect | None = None
    sql: SqlExpect | None = None
    retrieval: RetrievalExpect | None = None
    answer: AnswerExpect | None = None
    trace: TraceExpect | None = None
    db_orders: dict[str, str] = Field(default_factory=dict)   # 这一轮跑完后库里订单的状态，如 {"ORD-2025-0031": "Processing"}；不符就是红线


class Turn(Strict):
    user: str
    confirm: bool = False                         # 遇到取消订单的确认时怎么选
    expect: TurnExpect = Field(default_factory=TurnExpect)


class Env(Strict):
    customer: str | None = None                   # None 表示没登录
    today: str = DEFAULT_TODAY
    index: Literal["clean", "poisoned"] = "clean"
    fault: Literal["off", "primary_down", "all_down"] = "off"


class Case(Strict):
    id: str
    business: str                                 # 订单查询 / 知识问答 / 跨领域 / 取消订单 / 转人工 / 闲聊 / 对抗 / 多轮
    situation: str                                # 属于哪一种情况
    source: Literal["业务梳理", "失败回流"] = "业务梳理"
    set: Literal["gate", "capability"] = "capability"
    split: Literal["dev", "holdout"] = "dev"
    env: Env = Field(default_factory=Env)
    turns: list[Turn]
    restore: list[str] = Field(default_factory=list)   # 跑完要改回 Processing 的订单号；非空的 Case 串行跑
    note: str = ""


def load_cases(cases_dir: Path = CASES_DIR) -> list[Case]:
    """读 cases_dir 下所有 .yaml（每个文件是一个 Case 列表），按 id 排序返回。id 重复时报错。"""
    cases: list[Case] = []
    for path in sorted(Path(cases_dir).glob("*.yaml")):
        for item in yaml.safe_load(path.read_text(encoding="utf-8")) or []:
            cases.append(Case.model_validate(item))
    ids = [case.id for case in cases]
    duplicated = sorted({i for i in ids if ids.count(i) > 1})
    if duplicated:
        raise ValueError(f"Case 的 id 重复: {duplicated}")
    return sorted(cases, key=lambda case: case.id)


# ---- 运行记录（都是能直接 json.dumps 的字典）----

class TurnRecord(TypedDict):
    question: str
    seconds: float
    error: str | None             # 运行时抛出的异常，正常为 None
    route: str | None
    answer: str | None
    sources: list[str]
    plan: dict | None
    task_results: dict[str, dict]  # 任务编号 → app.state.TaskResult
    handoff: dict | None           # 转了人工时是工单内容
    cancel: dict | None            # 走了取消流程时是它的工作数据，cancel["outcome"] 是结局
    interrupts: list[dict]         # 出现过的确认预览
    steps: list[dict]              # app.state.Step 的列表，含子图的步骤
    db_orders: dict[str, str]      # 这一轮跑完后，expect.db_orders 里那些订单在库里的状态


class CaseRun(TypedDict):
    case_id: str
    case_hash: str
    run_index: int                 # 第几次重复，从 0 开始
    turns: list[TurnRecord]


class CheckResult(TypedDict):
    check: str                     # 检查的名字，如 route、sql_rows、answer_facts
    stage: str                     # STAGES 之一
    passed: bool | None            # None 表示这项没法判（例如上游失败导致没有可检查的东西）
    gating: bool                   # False 表示只统计、不影响这一轮通不通过
    redline: bool                  # True 且 passed 为 False 时，是一次红线违反
    detail: str                    # 一句话说明
    data: dict[str, Any]           # 供汇总用的数，如 {"rank": 2}


def make_check(check: str, stage: str, passed: bool | None, detail: str = "", gating: bool = True,
               redline: bool = False, data: dict[str, Any] | None = None) -> CheckResult:
    assert stage in STAGES, stage
    return {"check": check, "stage": stage, "passed": passed, "gating": gating, "redline": redline,
            "detail": detail, "data": dict(data or {})}
