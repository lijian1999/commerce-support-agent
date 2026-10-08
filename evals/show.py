"""把一条 Case 在某份报告里的情况打印出来：它的期望、每次运行的每一步、每项检查的结论。

用法: uv run python -m evals.show <case_id> [--report 报告编号] [--run 第几次，默认只展开第一次没通过的]
"""
import argparse
import json

import yaml

from evals.schema import REPORTS_DIR, RUNS_DIR, load_cases

MARK = {True: "✓", False: "✗", None: "–"}
STAGE_LABEL = {"router": "路由", "plan": "计划", "sql": "查库", "retrieval": "检索", "answer": "回答", "turn": "整轮"}


def _step_lines(step: dict) -> list[str]:
    """一个步骤打印成几行：检索列出命中的片段，执行列出查到的行。"""
    detail = step["detail"] if isinstance(step["detail"], dict) else {}
    name, summary = step["name"], str(step["summary"])
    if name.endswith("/retrieve") and detail.get("chunks") is not None:
        lines = [f"[{name}] 检索词: {str(detail.get('query'))[:120]}"]
        lines += [f"      {i}. {c['score']:.3f}  {c['doc']} § {c['section']}" for i, c in enumerate(detail["chunks"], 1)]
        return lines
    if name.endswith("/execute") and "rows" in detail:
        rows = json.dumps(detail["rows"], ensure_ascii=False)
        return [f"[{name}] {detail.get('row_count')} 行  {rows[:600]}{' …' if len(rows) > 600 else ''}"]
    if name == "answer":
        return [f"[{name}] （见下面的回答）"]
    first, *rest = summary.split("\n")
    return [f"[{name}] {first[:400]}"] + [f"      {line[:300]}" for line in rest[:8]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="查看一条 Case 的运行和检查结果")
    parser.add_argument("case_id")
    parser.add_argument("--report", help="报告编号，默认用最新的一份")
    parser.add_argument("--run", type=int, help="展开第几次运行（从 0 数）")
    args = parser.parse_args(argv)

    report_id = args.report or sorted(p.stem for p in REPORTS_DIR.glob("*.json"))[-1]
    report = json.loads((REPORTS_DIR / f"{report_id}.json").read_text(encoding="utf-8"))
    runs = json.loads((RUNS_DIR / f"{report_id}.json").read_text(encoding="utf-8"))
    case = next((c for c in load_cases() if c.id == args.case_id), None)
    results = sorted((r for r in report["results"] if r["case_id"] == args.case_id), key=lambda r: r["run_index"])
    if case is None or not results:
        print(f"报告 {report_id} 里没有 {args.case_id}")
        return 2

    print(f"Case {case.id}   {case.business} / {case.situation}   来源: {case.source}   {case.set}   {case.split}")
    if case.note:
        print(f"备注: {case.note}")
    env = case.env
    print(f"环境: 客户 {env.customer or '未登录'}   今天 {env.today}   索引 {env.index}   模拟故障 {env.fault}")
    print(f"报告 {report_id}: " + "  ".join(f"#{r['run_index']} {'通过' if r['passed'] else '不通过'}" for r in results))

    chosen = args.run if args.run is not None else next((r["run_index"] for r in results if not r["passed"]), 0)
    result = next(r for r in results if r["run_index"] == chosen)
    run = next(r for r in runs if r["case_id"] == args.case_id and r["run_index"] == chosen)
    print(f"\n======== 第 {chosen} 次运行 ========")
    for index, turn in enumerate(case.turns):
        record, evaluated = run["turns"][index], result["turns"][index]
        print(f"\n—— 第 {index + 1} 轮 ——")
        print(f"用户> {turn.user}   （{record['seconds']}s）")
        print("期望:")
        expect = turn.expect.model_dump(mode="json", exclude_defaults=True)
        for line in yaml.safe_dump(expect, allow_unicode=True, sort_keys=False, width=200).rstrip().split("\n"):
            print(f"    {line}")
        print("实际每一步:")
        for step in record["steps"]:
            for line in _step_lines(step):
                print(f"    {line}")
        print(f"客服> {(record['answer'] or '').strip()}")
        print(f"检查（这一轮{'通过' if evaluated['passed'] else '不通过'}，结局 {evaluated['outcome']}）:")
        for check in evaluated["checks"]:
            if check["check"] == "cost":
                d = check["data"]
                print(f"    –  成本      模型调用 {d['model_steps']} 次，SQL {d['sql']} 次")
                continue
            flags = ("  [红线]" if check["redline"] else "") + ("" if check["gating"] else "  [只统计]")
            print(f"    {MARK[check['passed']]}  {STAGE_LABEL[check['stage']]:<4}  {check['check']:<24} {check['detail'][:160]}{flags}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
