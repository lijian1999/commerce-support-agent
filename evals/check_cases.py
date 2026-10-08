"""检查 Case 的标注有没有过期：参考 SQL、原文摘录、检索标注三样各自对着真实数据核对。

三个检查函数都是纯函数（`check_reference_sql` 靠传进来的 `run_sql` 取数），
真实环境的取数办法放在文件后半部分，命令行跑全部 Case。
"""
from collections.abc import Callable
import sys
from typing import Any

from app import config
from app.db import connect, to_jsonable
from app.rag.index import IndexStore
# 行内容怎么比较，和代码判分走同一套规整规则，避免两处判断不一致
from evals.checks import _normalize
from evals.schema import Fact, POISON_DOCS_DIR, POISONED_INDEX_DIR, Case, load_cases


def _found(cells: list[Any], want: Any) -> bool:
    """结果行里有没有期望的值，规则同 evals.checks 的 sql_rows 检查。"""
    if isinstance(want, bool):
        return any(c is want for c in cells)
    if isinstance(want, (int, float)):
        return any(isinstance(c, (int, float)) and not isinstance(c, bool) and abs(c - want) < 0.005
                   for c in cells)
    needle = _normalize(want)
    return any(isinstance(c, str) and needle in _normalize(c) for c in cells)


def check_reference_sql(case: Case, run_sql: Callable[[str], list[dict]]) -> list[str]:
    """用每条查库期望自带的参考 SQL 核对 rows_contain / rows_exclude / row_count。"""
    problems: list[str] = []
    for turn_index, turn in enumerate(case.turns, start=1):
        sql = turn.expect.sql
        if sql is None or not sql.reference_sql:
            continue
        prefix = f"{case.id} 第 {turn_index} 轮: "
        try:
            rows = run_sql(sql.reference_sql)
        except Exception as exc:
            problems.append(f"{prefix}参考 SQL 执行失败: {exc}")
            continue
        cells = [value for row in rows for value in row.values()]
        for want in sql.rows_contain:
            if not _found(cells, want):
                problems.append(f"{prefix}参考 SQL 的结果里没有 {want!r}")
        for bad in sql.rows_exclude:
            if any(_normalize(str(c)).find(_normalize(bad)) >= 0 for c in cells):
                problems.append(f"{prefix}参考 SQL 的结果里出现了不该有的 {bad!r}")
        if sql.row_count is not None and len(rows) != sql.row_count:
            problems.append(f"{prefix}参考 SQL 返回 {len(rows)} 行，标注是 {sql.row_count} 行")
    return problems


def check_quotes(case: Case, corpus: str) -> list[str]:
    """每条关键事实带的文档原文摘录，得真的在文档里找得到。"""
    problems: list[str] = []
    for turn_index, turn in enumerate(case.turns, start=1):
        answer = turn.expect.answer
        if answer is None:
            continue
        for fact in answer.facts:
            if isinstance(fact, Fact) and fact.quote and fact.quote not in corpus:
                problems.append(f"{case.id} 第 {turn_index} 轮: 文档里找不到原文 {fact.quote!r}")
    return problems


def check_relevant(case: Case, sections: set[tuple[str, str]]) -> list[str]:
    """检索期望标的每个（文档，小节）都得在索引里存在。"""
    problems: list[str] = []
    for turn_index, turn in enumerate(case.turns, start=1):
        retrieval = turn.expect.retrieval
        if retrieval is None:
            continue
        for item in retrieval.relevant:
            if item.section:
                known = (item.doc, item.section) in sections
            else:
                known = any(doc == item.doc for doc, _ in sections)
            if not known:
                suffix = f" § {item.section}" if item.section else ""
                problems.append(f"{case.id} 第 {turn_index} 轮: 索引里没有 {item.doc}{suffix}")
    return problems


# ---- 真实环境的三样数据源 ----

def run_sql(sql: str) -> list[dict]:
    """直接执行一条 SQL，返回 json 友好的行。"""
    conn = connect("root")
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return to_jsonable(cur.fetchall())
    finally:
        conn.close()


def read_corpus() -> str:
    """正常文档和诱导文档的全部正文，用换行连起来。"""
    texts: list[str] = []
    for base in (config.DOCS_DIR, POISON_DOCS_DIR):
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.md")):
            texts.append(path.read_text(encoding="utf-8"))
    return "\n".join(texts)


def index_sections(store: IndexStore) -> set[tuple[str, str]] | None:
    """线上版本的全部（文档，小节）；索引还没有线上版本时返回 None。"""
    pointer = store.read_pointer()
    version = (pointer or {}).get("current")
    if not version:
        return None
    manifest = store.read_manifest(version)
    return {(chunk["doc"], chunk.get("section")) for chunk in manifest["chunks"]}


def _annotation_counts(case: Case, skip_retrieval: bool) -> tuple[int, int, int]:
    """这条 Case 里三类标注各有多少条，和被检查的项要一一对应。"""
    sql = quotes = relevant = 0
    for turn in case.turns:
        expect = turn.expect
        if expect.sql is not None and expect.sql.reference_sql:
            sql += 1
        if expect.answer is not None:
            quotes += sum(1 for f in expect.answer.facts if isinstance(f, Fact) and f.quote)
        if expect.retrieval is not None and not skip_retrieval:
            relevant += len(expect.retrieval.relevant)
    return sql, quotes, relevant


def main() -> int:
    cases = load_cases()
    corpus = read_corpus()
    clean_sections = index_sections(IndexStore())
    poison_sections = index_sections(IndexStore(POISONED_INDEX_DIR))
    counts = [0, 0, 0]
    problems = 0
    for case in cases:
        # 带诱导文档的索引没有构建时，这些 Case 的检索标注跳过不查
        if case.env.index == "poisoned" and poison_sections is None:
            sections = None
        else:
            sections = poison_sections if case.env.index == "poisoned" else clean_sections
        for line in (check_reference_sql(case, run_sql) + check_quotes(case, corpus)
                     + (check_relevant(case, sections) if sections is not None else [])):
            print(line)
            problems += 1
        found = _annotation_counts(case, sections is None)
        counts = [a + b for a, b in zip(counts, found)]
    print(f"检查了 {len(cases)} 条 Case：参考 SQL {counts[0]} 条，"
          f"原文摘录 {counts[1]} 条，检索标注 {counts[2]} 条；{problems} 个问题")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
