"""判分校准：把判分模型的结论和人工结论摆在一起比，看判分可信不可信。"""
import argparse
from pathlib import Path
from typing import Any

import yaml

from evals.judges import judge_facts, judge_grounded, judge_rules
from evals.schema import EVALS_DIR

ITEMS_PATH = EVALS_DIR / "calibration" / "items.yaml"


def load_items(path: Path = ITEMS_PATH) -> list[dict]:
    """读校准集。"""
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []


def judge_item(item: dict) -> Any:
    """按人工结论的形式给一个校准项判一次票（votes=1）。判不了返回 None。"""
    kind = item["kind"]
    if kind == "facts":
        facts = list(item["facts"])
        result = judge_facts(item["answer"], facts, votes=1)
        if result["passed"] is None:
            return None
        return [result["data"]["verdicts"].get(fact) for fact in facts]
    if kind == "rule":
        result = judge_rules(item["answer"], [item["rule"]], votes=1)
        if result["passed"] is None:
            return None
        return result["passed"] is False
    result = judge_grounded(item["answer"], item["materials"], votes=1)
    return result["passed"]


def calibrate(items: list[dict], rounds: int = 3) -> dict:
    """每项判 rounds 次，数和人工结论一致的次数。"""
    rows: list[dict] = []
    by_kind: dict[str, dict[str, int]] = {}
    agree_total = total = 0
    for item in items:
        kind = item["kind"]
        judged = [judge_item(item) for _ in range(rounds)]
        agree = sum(1 for verdict in judged if verdict == item["human"])
        rows.append({"id": item["id"], "kind": kind, "human": item["human"], "judged": judged, "agree": agree})
        bucket = by_kind.setdefault(kind, {"agree": 0, "total": 0})
        bucket["agree"] += agree
        bucket["total"] += rounds
        agree_total += agree
        total += rounds
    return {"items": rows, "by_kind": by_kind, "agree": agree_total, "total": total}


def main() -> None:
    parser = argparse.ArgumentParser(description="判分模型和人工结论的一致性")
    parser.add_argument("--rounds", type=int, default=3, help="每个校准项判几次")
    args = parser.parse_args()

    items = load_items()
    report = calibrate(items, args.rounds)
    for item, row in zip(items, report["items"], strict=True):
        all_agree = row["agree"] == len(row["judged"])
        print(f"[{row['kind']}] {row['id']}（{item['from']}）"
              f"人工: {row['human']}  判分: {row['judged']}  {'一致' if all_agree else '不一致'}")
    for kind, bucket in report["by_kind"].items():
        print(f"{kind}: 一致 {bucket['agree']}/{bucket['total']}")
    print(f"合计一致: {report['agree']}/{report['total']}")


if __name__ == "__main__":
    main()
