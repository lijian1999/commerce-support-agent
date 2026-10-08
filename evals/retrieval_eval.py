"""只评检索：拿一批标好相关片段的检索词，看相关片段排第几，不用跑整个 Agent。

`evaluate_retrieval` 是纯函数，检索靠传进来的 `search`；真实环境的检索在文件后半部分。
"""
import argparse
from collections.abc import Callable

import yaml

from app.rag.embedding import embed_query
from app.rag.index import IndexStore
from evals.env import current_index_version
from evals.schema import EVALS_DIR, RETRIEVAL_K

RETRIEVAL_SET_PATH = EVALS_DIR / "retrieval_set.yaml"
# 检索取这么深，排在它后面的都算找不到
FETCH_DEPTH = 10


def _ranks(item: dict, chunks: list[dict]) -> list[int | None]:
    """每个相关片段第一次出现的名次（从 1 数），没出现在列表里记 None。"""
    ranks: list[int | None] = []
    for rel in item.get("relevant") or []:
        rank: int | None = None
        for position, chunk in enumerate(chunks, start=1):
            if chunk.get("doc") == rel["doc"] and (
                    not rel.get("section") or chunk.get("section") == rel["section"]):
                rank = position
                break
        ranks.append(rank)
    return ranks


def evaluate_retrieval(items: list[dict], search: Callable[[str], list[dict]],
                       k: int = RETRIEVAL_K) -> dict:
    """逐项检索并汇总：命中@1/3/k、前 k 召回、MRR，另外记下无答案那几条的最高分。"""
    results: list[dict] = []
    for item in items:
        mode = item.get("mode", "any")
        chunks = search(item["query"]) or []
        entry = {"query": item["query"], "mode": mode, "rank": None, "recall": None,
                 "top_score": chunks[0].get("score") if chunks else None}
        if mode != "none":
            ranks = _ranks(item, chunks)
            if mode == "all":
                # 要全部命中：名次取最靠后的那个，有一项找不到就算没找到
                entry["rank"] = max(ranks) if ranks and all(r is not None for r in ranks) else None
                entry["recall"] = (sum(1 for r in ranks if r is not None and r <= k) / len(ranks)
                                   if ranks else None)
            else:
                hit = [r for r in ranks if r is not None]
                entry["rank"] = min(hit) if hit else None
                entry["recall"] = 1.0 if entry["rank"] is not None and entry["rank"] <= k else 0.0
        results.append(entry)

    labelled = [r for r in results if r["mode"] != "none"]
    summary = {"items": results, "labelled": len(labelled),
               "hit_at_1": None, "hit_at_3": None, "hit_at_k": None, "recall": None, "mrr": None,
               "none_top_scores": [r["top_score"] for r in results if r["mode"] == "none"],
               "labelled_min_top_score": min((r["top_score"] for r in labelled
                                              if r["top_score"] is not None), default=None)}
    if labelled:
        total = len(labelled)
        for name, limit in (("hit_at_1", 1), ("hit_at_3", 3), ("hit_at_k", k)):
            summary[name] = round(sum(1 for r in labelled if r["rank"] is not None
                                      and r["rank"] <= limit) / total, 4)
        summary["recall"] = round(sum(r["recall"] or 0.0 for r in labelled) / total, 4)
        summary["mrr"] = round(sum(1 / r["rank"] if r["rank"] else 0.0 for r in labelled) / total, 4)
    return summary


def make_search(index_version: str | None = None) -> Callable[[str], list[dict]]:
    """真实环境的检索：查询词先算 Embedding，再到指定版本（默认线上版本）的索引里查。"""
    version = index_version or current_index_version()
    store = IndexStore()

    def search(query: str) -> list[dict]:
        return store.query(version, embed_query(query), FETCH_DEPTH)

    return search


def _fmt(value: float | None, digits: int) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只评检索：看相关片段排在第几位")
    parser.add_argument("--index-version", default=None, help="默认用线上版本")
    args = parser.parse_args(argv)
    items = yaml.safe_load(RETRIEVAL_SET_PATH.read_text(encoding="utf-8")) or []
    got = evaluate_retrieval(items, make_search(args.index_version), RETRIEVAL_K)

    for item in got["items"]:
        if item["mode"] == "none":
            rank = "-"
        elif item["rank"] is None:
            rank = f">{FETCH_DEPTH}"
        else:
            rank = str(item["rank"])
        recall = "" if item["recall"] is None else f"{item['recall']:.2f}"
        print(f"{rank}  {recall}  {_fmt(item['top_score'], 3)}  {item['query']}")

    print(f"有标注的 {got['labelled']} 条: 命中@1 {_fmt(got['hit_at_1'], 2)}  "
          f"命中@3 {_fmt(got['hit_at_3'], 2)}  命中@{RETRIEVAL_K} {_fmt(got['hit_at_k'], 2)}  "
          f"前{RETRIEVAL_K}召回 {_fmt(got['recall'], 2)}  MRR {_fmt(got['mrr'], 2)}")
    scores = [round(v, 3) for v in got["none_top_scores"] if v is not None]
    print(f"无答案的 {len(got['none_top_scores'])} 条最高分 {scores}，"
          f"有答案的最低最高分 {_fmt(got['labelled_min_top_score'], 3)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
