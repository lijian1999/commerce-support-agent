"""检查 Case 标注有没有过期、只评检索这两个模块的单元测试：取数全是测试里构造的假数据。"""
from typing import Any

from evals.check_cases import check_quotes, check_reference_sql, check_relevant
from evals.retrieval_eval import evaluate_retrieval
from evals.schema import Case


def make_case(expects: list[dict[str, Any]]) -> Case:
    """构造一条 Case，每个元素是一轮的期望。"""
    return Case.model_validate({
        "id": "c1", "business": "订单查询", "situation": "样例",
        "turns": [{"user": "问一句", "expect": expect} for expect in expects],
    })


# ---- check_reference_sql ----

_SQL_EXPECT: dict[str, Any] = {
    "sql": {"reference_sql": "SELECT name, price FROM products WHERE name LIKE '%think%'",
            "rows_contain": ["ThinkPad", 237], "rows_exclude": ["HP"], "row_count": 2},
}


def test_reference_sql_ok() -> None:
    rows = [{"name": "Lenovo ThinkPad X1"}, {"s": 237.0}]
    assert check_reference_sql(make_case([_SQL_EXPECT]), lambda sql: rows) == []


def test_reference_sql_problems() -> None:
    problems = check_reference_sql(make_case([_SQL_EXPECT]), lambda sql: [{"name": "HP Pavilion"}])
    assert len(problems) == 4
    assert all(p.startswith("c1 第 1 轮: ") for p in problems)
    assert "参考 SQL 的结果里没有 'ThinkPad'" in problems[0]
    assert "参考 SQL 的结果里没有 237" in problems[1]
    assert "参考 SQL 的结果里出现了不该有的 'HP'" in problems[2]
    assert "参考 SQL 返回 1 行，标注是 2 行" in problems[3]


def test_reference_sql_error_and_skip() -> None:
    def boom(sql: str) -> list[dict]:
        raise RuntimeError("bad")

    problems = check_reference_sql(make_case([{"sql": {"reference_sql": "SELECT 1",
                                                      "rows_contain": ["x"]}}]), boom)
    assert len(problems) == 1 and "参考 SQL 执行失败" in problems[0]
    assert "bad" in problems[0]

    called = False

    def spy(sql: str) -> list[dict]:
        nonlocal called
        called = True
        return []

    # 没有参考 SQL 的期望：什么都不查
    assert check_reference_sql(make_case([{"sql": {"rows_contain": ["x"], "row_count": 0}}]), spy) == []
    assert not called


# ---- check_quotes ----

def test_quotes() -> None:
    case = make_case([{"answer": {"facts": [
        {"text": "拆封后 15 天内可退", "quote": "Returns are accepted within 15 days."},
        {"text": "整机保修一年", "quote": "整机保修一年，永不坏"},
        "这条是模型判分的自由发挥",
    ]}}])
    corpus = "前言\nReturns are accepted within 15 days.\n后记"
    problems = check_quotes(case, corpus)
    assert len(problems) == 1
    assert problems[0].startswith("c1 第 1 轮: ")
    assert "整机保修一年，永不坏" in problems[0]


# ---- check_relevant ----

def test_relevant() -> None:
    case = make_case([{"retrieval": {"relevant": [
        {"doc": "a.md", "section": "S1"},
        {"doc": "a.md"},
        {"doc": "a.md", "section": "S2"},
        {"doc": "b.md"},
    ]}}])
    problems = check_relevant(case, {("a.md", "S1")})
    assert len(problems) == 2
    assert problems[0].endswith("索引里没有 a.md § S2")
    assert problems[1].endswith("索引里没有 b.md")
    assert all(p.startswith("c1 第 1 轮: ") for p in problems)


# ---- evaluate_retrieval ----

def test_retrieval_any_all_none() -> None:
    chunks: dict[str, list[dict]] = {
        "q-any": [{"doc": "x.md", "section": "S", "score": 0.9},
                  {"doc": "a.md", "section": "S1", "score": 0.8},
                  {"doc": "y.md", "section": "S", "score": 0.7}],
        "q-all": [{"doc": "b.md", "section": "X", "score": 0.9},
                  {"doc": "z.md", "section": "S", "score": 0.6}],
        "q-none": [{"doc": "w.md", "section": "S", "score": 0.5}],
    }
    items = [{"query": "q-any", "mode": "any", "relevant": [{"doc": "a.md", "section": "S1"}]},
             {"query": "q-all", "mode": "all",
              "relevant": [{"doc": "b.md", "section": "X"}, {"doc": "c.md", "section": "Y"}]},
             {"query": "q-none", "mode": "none", "relevant": []}]
    got = evaluate_retrieval(items, lambda query: chunks[query])

    by_query = {item["query"]: item for item in got["items"]}
    assert [by_query[q]["rank"] for q in ("q-any", "q-all", "q-none")] == [2, None, None]
    assert [by_query[q]["recall"] for q in ("q-any", "q-all", "q-none")] == [1.0, 0.5, None]
    assert got["labelled"] == 2
    assert got["hit_at_1"] == 0.0
    assert got["hit_at_3"] == 0.5
    assert got["hit_at_k"] == 0.5
    assert got["recall"] == 0.75
    assert got["mrr"] == 0.25
    assert len(got["none_top_scores"]) == 1
    assert got["labelled_min_top_score"] == 0.9


def test_retrieval_rank_beyond_k() -> None:
    chunks = [{"doc": f"d{i}.md", "section": "S", "score": 0.9} for i in range(9)]
    chunks[7] = {"doc": "target.md", "section": "S", "score": 0.6}   # 排在第 8
    got = evaluate_retrieval([{"query": "q", "mode": "any", "relevant": [{"doc": "target.md"}]}],
                             lambda query: chunks)
    assert got["items"][0]["rank"] == 8
    assert got["items"][0]["recall"] == 0.0
    assert got["hit_at_k"] == 0.0


def test_retrieval_empty() -> None:
    items = [{"query": "q", "mode": "none", "relevant": []}]
    got = evaluate_retrieval(items, lambda query: [{"doc": "x.md", "section": "S", "score": 0.5}])
    assert got["labelled"] == 0
    assert got["hit_at_1"] is None
