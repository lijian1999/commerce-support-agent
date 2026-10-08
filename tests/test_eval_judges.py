"""评测里模型判分的测试：用假的判分模型，不调真实模型。"""
import copy
import json
from pathlib import Path

import pytest

from app.models import ModelResult, ModelUnavailable
from evals import calibrate as calibrate_module
from evals import judges
from evals.calibrate import calibrate
from evals.judges import (Claim, FactsResult, GroundedResult, RuleResult, judge_facts, judge_grounded,
                          judge_rules, judge_turn)
from evals.schema import TurnExpect

RECORDS = json.loads((Path(__file__).parent / "fixtures" / "eval_records.json").read_text(encoding="utf-8"))


class FakeModel:
    """按顺序吐出队列里预置的结果（异常对象则抛出它），并记下每次收到的消息。"""

    def __init__(self, queue: list, log: list):
        self.queue, self.log = list(queue), log

    def invoke(self, messages):
        self.log.append(list(messages))
        item = self.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return ModelResult(value=item, notes=[])


class FakeFactory:
    """替换 evals.judges.get_model：只关心 judge 角色，调用次数靠 log 看。"""

    def __init__(self, queue: list):
        self.sent: list[list] = []
        self._model = FakeModel(queue, self.sent)

    def __call__(self, role: str, schema=None, fault: str = "off"):
        assert role == "judge"
        return self._model

    @property
    def invoked(self) -> int:
        return len(self.sent)

    def user(self, index: int = 0) -> str:
        return next(text for role, text in self.sent[index] if role == "user")


@pytest.fixture(autouse=True)
def no_real_model(monkeypatch):
    """默认：任何一次调用都当作模型不可用，测试用 install() 换成自己的队列。"""
    return install(monkeypatch, [])


def install(monkeypatch, queue: list) -> FakeFactory:
    factory = FakeFactory(queue)
    monkeypatch.setattr(judges, "get_model", factory)
    return factory


def facts_ballot(verdicts: list[str]) -> FactsResult:
    return FactsResult(checks=[judges.FactCheck(fact=f"事实{i + 1}", evidence="略", verdict=v)
                               for i, v in enumerate(verdicts)])


def rules_ballot(violated: bool) -> RuleResult:
    return RuleResult(evidence="略", violated=violated)


def grounded_ballot(claims: list[tuple[str, bool]]) -> GroundedResult:
    return GroundedResult(claims=[Claim(claim=text, support="略" if ok else "无", supported=ok)
                                  for text, ok in claims])


def record_of(case_id: str) -> dict:
    return copy.deepcopy(RECORDS[case_id])


def test_facts_all_present(monkeypatch):
    factory = install(monkeypatch, [facts_ballot(["present", "present"])] * 3)
    result = judge_facts("回答", ["事实1", "事实2"], votes=3)
    assert result["passed"] is True
    assert result["check"] == "answer_facts"
    assert factory.invoked == 3


def test_facts_majority(monkeypatch):
    install(monkeypatch, [facts_ballot(["present"]), facts_ballot(["absent"]), facts_ballot(["absent"])])
    result = judge_facts("回答", ["事实1"], votes=3)
    assert result["passed"] is False
    assert result["data"]["verdicts"]["事实1"] == "absent"


def test_facts_tie_takes_stricter(monkeypatch):
    install(monkeypatch, [facts_ballot(["present"]), facts_ballot(["absent"]), facts_ballot(["contradicted"])])
    result = judge_facts("回答", ["事实1"], votes=3)
    assert result["data"]["verdicts"]["事实1"] == "contradicted"
    assert result["passed"] is False


def test_facts_wrong_length_vote_dropped(monkeypatch):
    factory = install(monkeypatch, [facts_ballot(["present"]),
                                    facts_ballot(["present", "present"]),
                                    facts_ballot(["present", "present"])])
    result = judge_facts("回答", ["事实1", "事实2"], votes=3)
    assert result["passed"] is True
    assert len(result["data"]["votes"]) == 2
    assert factory.invoked == 3


def test_facts_model_error(monkeypatch):
    install(monkeypatch, [ModelUnavailable("x", [])] * 3)
    result = judge_facts("回答", ["事实1"], votes=3)
    assert result["passed"] is None
    assert result["detail"] == "判分模型不可用"
    assert result["data"] == {"verdicts": {}, "votes": []}


def test_facts_user_message(monkeypatch):
    factory = install(monkeypatch, [facts_ballot(["present"])])
    judge_facts("xxx", ["a", "b"], votes=1)
    assert factory.user(0) == "关键事实：\n- a\n- b\n\n客服回答：\nxxx"


def test_rules_majority_violated(monkeypatch):
    install(monkeypatch, [rules_ballot(True), rules_ballot(True), rules_ballot(False)])
    result = judge_rules("回答", ["不许做 X"], votes=3)
    assert result["passed"] is False
    assert result["data"]["violated"] == ["不许做 X"]
    assert result["check"] == "answer_must_not"


def test_rules_minority_not_violated(monkeypatch):
    install(monkeypatch, [rules_ballot(True), rules_ballot(False), rules_ballot(False)])
    result = judge_rules("回答", ["不许做 X"], votes=3)
    assert result["passed"] is True
    assert result["data"]["violated"] == []


def test_rules_redline_flag(monkeypatch):
    install(monkeypatch, [rules_ballot(False)] * 6)
    normal = judge_rules("回答", ["不许做 X"], votes=3)
    redline = judge_rules("回答", ["不许做 X"], votes=3, redline=True)
    assert (normal["check"], normal["redline"]) == ("answer_must_not", False)
    assert (redline["check"], redline["redline"]) == ("answer_redline", True)


def test_rules_each_rule_gets_votes(monkeypatch):
    factory = install(monkeypatch, [rules_ballot(False)] * 6)
    judge_rules("回答", ["不许做 X", "不许做 Y"], votes=3)
    assert factory.invoked == 6


def test_grounded_majority(monkeypatch):
    install(monkeypatch, [grounded_ballot([("价格 100 元", True)]),
                          grounded_ballot([("甲", False)]),
                          grounded_ballot([("乙", False)])])
    result = judge_grounded("回答", "材料", votes=3)
    assert result["passed"] is False
    assert result["gating"] is False
    assert result["data"]["unsupported"] == ["甲", "乙"]
    assert result["data"]["votes"] == [True, False, False]
    assert result["check"] == "answer_grounded"


def test_judge_turn_grounded_only(monkeypatch):
    factory = install(monkeypatch, [grounded_ballot([("237 美元", True)])])
    results = judge_turn(TurnExpect(), record_of("A06"), votes=1)
    assert len(results) == 1
    assert results[0]["check"] == "answer_grounded"
    assert results[0]["passed"] is True
    assert factory.user(0).startswith("材料：\n")


def test_judge_turn_skips_grounded(monkeypatch):
    factory = install(monkeypatch, [])
    for case_id in ("F01", "A03"):
        assert judge_turn(TurnExpect(), record_of(case_id), votes=1) == []
    assert factory.invoked == 0


def test_judge_turn_order(monkeypatch):
    install(monkeypatch, [facts_ballot(["present"]), rules_ballot(False), rules_ballot(False),
                          grounded_ballot([("说法", True)])])
    expect = TurnExpect.model_validate({"answer": {"facts": ["a"], "must_not": ["b"], "redline": ["c"]}})
    results = judge_turn(expect, record_of("A06"), votes=1)
    assert [r["check"] for r in results] == ["answer_facts", "answer_must_not", "answer_redline", "answer_grounded"]
    assert all(r["passed"] for r in results)


def test_judge_turn_accepts_fact_objects(monkeypatch):
    factory = install(monkeypatch, [facts_ballot(["present"]), grounded_ballot([("说法", True)])])
    expect = TurnExpect.model_validate({"answer": {"facts": [{"text": "a", "quote": "q"}]}})
    judge_turn(expect, record_of("A06"), votes=1)
    assert "- a" in factory.user(0)
    assert "q" not in factory.user(0)


def test_calibrate_counts(monkeypatch):
    answers = iter([True, True, True, False])
    monkeypatch.setattr(calibrate_module, "judge_item", lambda item: next(answers))
    items = [{"id": "x1", "kind": "rule", "human": True}, {"id": "x2", "kind": "grounded", "human": True}]
    report = calibrate(items, rounds=2)
    assert report["agree"] == 3
    assert report["total"] == 4
    assert report["by_kind"]["rule"] == {"agree": 2, "total": 2}
    assert report["by_kind"]["grounded"] == {"agree": 1, "total": 2}
