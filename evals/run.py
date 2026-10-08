"""评测的命令行入口：选 Case、跑、评、写报告和原始记录。

用法：uv run python -m evals.run [--case 模式]... [--set gate|capability] [--split dev|holdout]
[--business 名字] [--repeats 3] [--workers 4] [--votes 3] [--no-judge] [--langsmith] [--force]
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from evals.manifest import case_hash, current_state, eval_set_version, load_manifest
from evals.poisoned_index import build_poisoned_index
from evals.report import evaluate_run, render_text, summarize
from evals.runner import run_cases
from evals.schema import REPORTS_DIR, RUNS_DIR, Case, load_cases


def select_cases(cases: list[Case], patterns: list[str] | None = None, set_name: str | None = None,
                 split: str | None = None, business: str | None = None) -> list[Case]:
    """四个条件都满足的留下。patterns 非空时 id 要匹配其中至少一个，其余三个不为 None 时要相等。"""
    def keep(case: Case) -> bool:
        if patterns and not any(fnmatch.fnmatchcase(case.id, pattern) for pattern in patterns):
            return False
        if set_name is not None and case.set != set_name:
            return False
        if split is not None and case.split != split:
            return False
        if business is not None and case.business != business:
            return False
        return True

    return [case for case in cases if keep(case)]


def build_report(report_id: str, cases: list[Case], results: list[dict], state: dict,
                 options: dict) -> dict:
    """拼出完整的报告字典：cases 是这次实际跑的 Case 的 {id: hash}，版本字段从它现算。"""
    hashes = {case.id: case_hash(case) for case in cases}
    return {
        "report_id": report_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "cases": hashes,
        "eval_set_version": eval_set_version(hashes),
        "agent": state["agent"],
        "env": state["env"],
        "evaluator": state["evaluator"],
        "repeats": options["repeats"],
        "options": options,
        "summary": summarize(cases, results, options["repeats"]),
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线评测")
    parser.add_argument("--repeats", type=int, default=3, help="每条 Case 跑几次")
    parser.add_argument("--workers", type=int, default=4, help="并行数")
    parser.add_argument("--votes", type=int, default=3, help="模型判分每项判几次")
    parser.add_argument("--no-judge", dest="judge", action="store_false", help="不做模型判分")
    parser.add_argument("--case", action="append", metavar="模式", help="只跑 id 匹配的 Case，可以写多次")
    parser.add_argument("--set", choices=("gate", "capability"), help="按 set 筛")
    parser.add_argument("--split", choices=("dev", "holdout"), help="按 split 筛")
    parser.add_argument("--business", help="按 business 筛")
    parser.add_argument("--langsmith", action="store_true", help="作为 LangSmith 实验来跑")
    parser.add_argument("--force", action="store_true", help="数据库不是初始状态也照跑")
    args = parser.parse_args(argv)

    all_cases = load_cases()
    cases = select_cases(all_cases, patterns=args.case, set_name=args.set, split=args.split,
                         business=args.business)
    if not cases:
        print("没有选中任何 Case")
        return 2

    state = current_state(all_cases)
    manifest = load_manifest()
    if manifest is None:
        print("提示: 还没有 Manifest，先运行 uv run python scripts/eval_manifest.py")
    elif manifest["env"]["db_hash"] != state["env"]["db_hash"]:
        print(f"数据库不是 Manifest 记录的初始状态（Manifest: {manifest['env']['db_hash']}，"
              f"当前: {state['env']['db_hash']}），先运行 uv run python scripts/import_mysql.py 恢复，"
              f"或者加 --force")
        if not args.force:
            return 2

    if any(case.env.index == "poisoned" for case in cases):
        info = build_poisoned_index()
        print(f"带诱导文档的索引 {info['version']}，指纹 {info['index_hash']}")

    report_id = time.strftime("%Y%m%d-%H%M%S")
    print(f"报告 {report_id}: {len(cases)} 条 Case × {args.repeats} 次，"
          f"评测集 {state['eval_set_version']}")

    options = {"repeats": args.repeats, "judge": args.judge, "votes": args.votes,
               "workers": args.workers, "langsmith": args.langsmith,
               "filter": {"case": args.case, "set": args.set, "split": args.split,
                          "business": args.business}}

    if args.langsmith:
        # 用到时才导入：这个模块可能还没装好，也不用它的时候不应该报错
        from evals.langsmith_run import execute_langsmith
        runs, results, info = execute_langsmith(cases, args.repeats, args.workers, args.judge,
                                                args.votes, report_id, state)
        print(f"LangSmith 实验: {info['experiment']}   {info['url']}")
    else:
        hashes = {case.id: case_hash(case) for case in cases}

        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        partial = RUNS_DIR / f"{report_id}.partial.jsonl"

        def progress(done: int, total: int, case_run: dict) -> None:
            seconds = sum(turn["seconds"] for turn in case_run["turns"])
            print(f"[{done}/{total}] {case_run['case_id']} #{case_run['run_index']}  {seconds:.1f}s", flush=True)
            # 每跑完一次就落盘一行：进程中途断了，已经跑完的记录还在
            with partial.open("a", encoding="utf-8") as f:
                f.write(json.dumps(case_run, ensure_ascii=False) + "\n")

        runs = run_cases(cases, args.repeats, args.workers, hashes, progress)
        by_case = {case.id: case for case in cases}
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(evaluate_run, by_case[run["case_id"]], run, args.judge, args.votes)
                       for run in runs]
            results = [future.result() for future in futures]
        results.sort(key=lambda result: (result["case_id"], result["run_index"]))

    report = build_report(report_id, cases, results, state, options)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    (REPORTS_DIR / f"{report_id}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    (RUNS_DIR / f"{report_id}.json").write_text(
        json.dumps(runs, ensure_ascii=False, indent=1), encoding="utf-8")

    (RUNS_DIR / f"{report_id}.partial.jsonl").unlink(missing_ok=True)
    print()
    print(render_text(report))
    print()
    print(f"报告: evals/reports/{report_id}.json   原始记录: evals/runs/{report_id}.json")

    summary = report["summary"]
    if summary["redlines"] or summary["gate"]["failed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
