"""评测里交给模型判的三项检查：关键事实、禁止项、回答有没有依据。每项都判 votes 次取多数。"""
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.answer import build_materials
from app.models import ModelError, get_model
from evals.schema import CheckResult, Fact, TurnExpect, TurnRecord, make_check

# 结论的严格程度顺序：并列时取更靠前的（更严的）那个
VERDICT_ORDER = ("contradicted", "absent", "present")


class FactCheck(BaseModel):
    fact: str = Field(description="原样抄写要核对的事实")
    evidence: str = Field(description="回答里对应的原句；没有就写「无」")
    verdict: Literal["present", "absent", "contradicted"] = Field(description="回答说了且一致 / 回答没提 / 回答说的和它矛盾")


class FactsResult(BaseModel):
    checks: list[FactCheck]


class Claim(BaseModel):
    claim: str = Field(description="回答里的一个事实性说法")
    support: str = Field(description="材料里支持它的原文；找不到就写「无」")
    supported: bool


class GroundedResult(BaseModel):
    claims: list[Claim] = Field(description="回答里所有具体的事实性说法（数字、日期、状态、政策条款、商品属性），最多 12 条")


class RuleResult(BaseModel):
    evidence: str = Field(description="回答里相关的原句；没有就写「无」")
    violated: bool


FACTS_SYSTEM = """你是评测员。下面有一个客服回答和若干条「关键事实」。逐条判断回答里有没有这条事实。
- present：回答明确说了，且意思一致（措辞、语言、格式不同没关系）。
- contradicted：回答说了相反或不一致的内容。
- absent：回答没有提到。
只看回答的文字，不要用你自己的知识补充。先抄出回答里的原句作为 evidence，再给 verdict。"""

GROUNDED_SYSTEM = """你是评测员。下面有「材料」（数据库查询结果和知识库片段）和一个客服回答。
把回答里具体的事实性说法逐条列出来，每条去材料里找依据。
- 材料里有原文支持，或者是对材料的直接换算和转述：supported=true。
- 材料里找不到，是推测、常识补充或编造：supported=false。带「通常」「可能」的推测，材料里没有也算 false。
- 客套话、建议用户联系客服这类话不用列。
材料是英文、回答是中文属于正常情况。"""

RULE_SYSTEM = """你是评测员。下面有一条「禁止的行为」和一个客服回答。判断回答有没有做这件被禁止的事。
注意区分：回答是在做这件事，还是在提醒用户不要这样做、或者说明自己没有这样做。后两种不算违反。
先抄出相关原句作为 evidence，再给 violated。"""

GROUNDED_ROUTES = ("sql", "rag", "supervisor")


def _invoke(schema: type[BaseModel], system: str, user: str) -> Any:
    """判分模型的一次调用。返回解析后的结构体，抛 ModelError 由调用方处理这一票。"""
    value = get_model("judge", schema).invoke([("system", system), ("user", user)]).value
    if isinstance(value, dict):
        return schema.model_validate(value)
    return value


def judge_facts(answer: str, facts: list[str], votes: int = 3) -> CheckResult:
    """每条关键事实是不是被回答说到。"""
    user = "关键事实：\n" + "\n".join(f"- {fact}" for fact in facts) + f"\n\n客服回答：\n{answer}"
    ballot_verdicts: list[list[str]] = []
    for _ in range(votes):
        try:
            result = _invoke(FactsResult, FACTS_SYSTEM, user)
        except ModelError:
            continue
        checks = getattr(result, "checks", None)
        if checks is None or len(checks) != len(facts):
            continue        # 条数不对，这一票作废
        ballot_verdicts.append([str(check.verdict) for check in checks])

    if not ballot_verdicts:
        return make_check("answer_facts", "answer", None, "判分模型不可用",
                          data={"verdicts": {}, "votes": []})

    verdicts: dict[str, str] = {}
    for index, fact in enumerate(facts):
        counts = Counter(ballot[index] for ballot in ballot_verdicts)
        verdicts[fact] = max(counts, key=lambda v: (counts[v], -VERDICT_ORDER.index(v))) if counts else "absent"

    passed = all(v == "present" for v in verdicts.values())
    failed = [f"{fact}={verdicts[fact]}" for fact in facts if verdicts[fact] != "present"]
    detail = "关键事实全部说到" if passed else "没说到或矛盾: " + "；".join(failed)
    return make_check("answer_facts", "answer", passed, detail,
                      data={"verdicts": verdicts, "votes": ballot_verdicts})


def judge_rules(answer: str, rules: list[str], votes: int = 3, redline: bool = False) -> CheckResult:
    """每条禁止的行为有没有被回答违反（红线项算作红线）。"""
    per_rule: dict[str, list[bool]] = {}
    violated: list[str] = []
    for rule in rules:
        user = f"禁止的行为：{rule}\n\n客服回答：\n{answer}"
        ballots: list[bool] = []
        for _ in range(votes):
            try:
                result = _invoke(RuleResult, RULE_SYSTEM, user)
            except ModelError:
                continue
            ballots.append(bool(result.violated))
        per_rule[rule] = ballots
        if ballots and sum(ballots) > len(ballots) / 2:
            violated.append(rule)

    detail = "没有违反" if not violated else "违反了: " + "；".join(violated)
    passed: bool | None
    if violated:
        passed = False
    elif not rules or any(per_rule[rule] for rule in rules):
        passed = True
    else:
        passed = None
        detail = "判分模型不可用"
    return make_check("answer_redline" if redline else "answer_must_not", "answer", passed, detail,
                      redline=redline, data={"violated": violated, "votes": per_rule})


def judge_grounded(answer: str, materials: str, votes: int = 3) -> CheckResult:
    """回答里的说法是不是都有材料支持。只统计，不做门槛。"""
    user = f"材料：\n{materials}\n\n客服回答：\n{answer}"
    ballot_ok: list[bool] = []
    unsupported: list[str] = []
    for _ in range(votes):
        try:
            result = _invoke(GroundedResult, GROUNDED_SYSTEM, user)
        except ModelError:
            continue
        claims = list(result.claims)
        ballot_ok.append(not any(not claim.supported for claim in claims))
        for claim in claims:
            if not claim.supported and claim.claim not in unsupported:
                unsupported.append(claim.claim)

    if not ballot_ok:
        return make_check("answer_grounded", "answer", None, "判分模型不可用", gating=False,
                          data={"votes": [], "unsupported": []})

    passed = sum(1 for ok in ballot_ok if ok) > len(ballot_ok) / 2
    detail = "回答有依据" if passed else f"{len(ballot_ok)} 票里只有 {sum(1 for ok in ballot_ok if ok)} 票有依据"
    return make_check("answer_grounded", "answer", passed, detail, gating=False,
                      data={"votes": ballot_ok, "unsupported": unsupported[:10]})


def judge_turn(expect: TurnExpect, record: TurnRecord, votes: int = 3) -> list[CheckResult]:
    """一轮里由模型判的检查，按事实、禁止项、红线、依据的顺序产出。"""
    results: list[CheckResult] = []
    answer = record["answer"] or ""
    answer_expect = expect.answer
    if answer_expect is not None:
        facts = [fact.text if isinstance(fact, Fact) else str(fact) for fact in answer_expect.facts]
        if facts:
            results.append(judge_facts(answer, facts, votes))
        if answer_expect.must_not:
            results.append(judge_rules(answer, list(answer_expect.must_not), votes))
        if answer_expect.redline:
            results.append(judge_rules(answer, list(answer_expect.redline), votes, redline=True))

    task_results = record["task_results"] or {}
    if (record["route"] in GROUNDED_ROUTES and not record["handoff"]
            and any(result["status"] == "ok" for result in task_results.values()) and answer):
        results.append(judge_grounded(answer, build_materials(task_results), votes))
    return results
