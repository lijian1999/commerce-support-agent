"""对比两份评测报告，回答「相对上一份报告是变好了还是变坏了」。

模型输出不完全稳定，所以比较有三条规矩：只在两份报告里内容 hash 相同的 Case 上比、
一条 Case 通过次数的小幅变动不下结论、总通过率差得不多也不下结论。
`compare_reports` 是纯函数，`render_compare` 把结果渲染成人看的文本，
`main` 是对应的命令行。
"""
import json
from pathlib import Path
import sys
from typing import Any

# 分环节指标的固定顺序，两边都从 summary["stages"] 里取
STAGE_METRICS: tuple[tuple[str, str], ...] = (
    ("router", "accept_rate"),
    ("router", "prefer_rate"),
    ("plan", "pass_rate"),
    ("sql", "result_rate"),
    ("sql", "rows_rate"),
    ("retrieval", "hit_at_6"),
    ("retrieval", "mrr"),
    ("answer", "facts_rate"),
    ("answer", "facts_rate_upstream_ok"),
    ("answer", "grounded_rate"),
    ("turn", "outcome_rate"),
)


def _rows(report: dict) -> dict[str, dict]:
    """报告 case_table 里的行，按 id 索引。"""
    return {row.get("id"): row for row in ((report.get("summary") or {}).get("case_table") or [])}


def _rate(passes: int, runs: int) -> float | None:
    """通过率，分母为 0 时记 None。"""
    return None if not runs else passes / runs


def _version(report: dict) -> dict:
    """一份报告里用来对照版本的三项。"""
    return {"report_id": report.get("report_id"),
            "eval_set_version": report.get("eval_set_version"),
            "git": (report.get("agent") or {}).get("git")}


def compare_reports(old: dict, new: dict, noise: float = 0.05) -> dict:
    """比较两份报告，返回说明 24 里那个字典。"""
    old_cases, new_cases = old.get("cases") or {}, new.get("cases") or {}
    both = sorted(set(old_cases) & set(new_cases))
    common = sorted(case_id for case_id in both if old_cases[case_id] == new_cases[case_id])
    changed = sorted(case_id for case_id in both if old_cases[case_id] != new_cases[case_id])
    only_old = sorted(set(old_cases) - set(new_cases))
    only_new = sorted(set(new_cases) - set(old_cases))

    old_rows, new_rows = _rows(old), _rows(new)
    regressed: list[dict] = []
    improved: list[dict] = []
    wobble: list[dict] = []
    same = 0
    passes_old = runs_old = passes_new = runs_new = 0

    for case_id in common:
        row_old, row_new = old_rows.get(case_id), new_rows.get(case_id)
        # 哪一边的 case_table 里没有这条，就不参与分类，也不计入通过率
        if row_old is None or row_new is None:
            continue
        p_old, r_old = int(row_old["passes"]), int(row_old["runs"])
        p_new, r_new = int(row_new["passes"]), int(row_new["runs"])
        rate_old, rate_new = _rate(p_old, r_old), _rate(p_new, r_new)
        text_old, text_new = f"{p_old}/{r_old}", f"{p_new}/{r_new}"

        if rate_old == 1.0 and rate_new is not None and rate_new <= 1 / 3:
            regressed.append({"id": case_id, "old": text_old, "new": text_new,
                              "first_failed_stage": row_new.get("first_failed_stage"),
                              "detail": row_new.get("detail")})
        elif rate_old is not None and rate_old <= 1 / 3 and rate_new == 1.0:
            improved.append({"id": case_id, "old": text_old, "new": text_new})
        elif (p_old, r_old) == (p_new, r_new):
            same += 1
        else:
            wobble.append({"id": case_id, "old": text_old, "new": text_new})
        passes_old, runs_old = passes_old + p_old, runs_old + r_old
        passes_new, runs_new = passes_new + p_new, runs_new + r_new

    rate_old = None if not runs_old else round(passes_old / runs_old, 4)
    rate_new = None if not runs_new else round(passes_new / runs_new, 4)
    rate_delta = None if rate_old is None or rate_new is None else round(rate_new - rate_old, 4)

    redlines_new = len((new.get("summary") or {}).get("redlines") or [])
    gate_failed_new = (new.get("summary") or {}).get("gate", {}).get("failed")
    rate_conclusive = rate_delta is not None and abs(rate_delta) >= noise

    problems: list[str] = []
    if redlines_new > 0:
        problems.append(f"红线违反 {redlines_new} 次")
    if gate_failed_new:
        problems.append(f"必过集有 {len(gate_failed_new)} 条没有稳定通过")
    if regressed:
        problems.append(f"有 {len(regressed)} 条 Case 回退")
    if rate_conclusive and rate_delta < 0:
        problems.append(f"通过率下降 {abs(rate_delta):.1%}")

    return {
        "old": _version(old), "new": _version(new),
        "common": common, "changed": changed, "only_old": only_old, "only_new": only_new,
        "regressed": regressed, "improved": improved, "wobble": wobble, "same": same,
        "rate_old": rate_old, "rate_new": rate_new, "rate_delta": rate_delta,
        "rate_conclusive": rate_conclusive,
        "redlines_old": len((old.get("summary") or {}).get("redlines") or []),
        "redlines_new": redlines_new,
        "gate_failed_new": gate_failed_new,
        "stages": _stages(old, new),
        "release_ok": not problems,
        "release_problems": problems,
    }


def _stages(old: dict, new: dict) -> list[dict]:
    """按固定顺序列出的分环节指标，两边都从 summary["stages"] 里取，取不到记 None。"""
    old_stages = (old.get("summary") or {}).get("stages") or {}
    new_stages = (new.get("summary") or {}).get("stages") or {}
    return [{"stage": stage, "metric": metric,
             "old": (old_stages.get(stage) or {}).get(metric),
             "new": (new_stages.get(stage) or {}).get(metric)}
            for stage, metric in STAGE_METRICS]


def _pct(value: Any) -> str:
    """比率显示成百分数，None 显示成 -。"""
    return "-" if value is None else f"{value:.1%}"


def _signed_pct(value: Any) -> str:
    """带正负号的百分数，None 显示成 -。"""
    return "-" if value is None else f"{value:+.1%}"


def _f2(value: Any) -> str:
    """保留两位小数，None 显示成 -。"""
    return "-" if value is None else f"{value:.2f}"


def render_compare(result: dict) -> str:
    """把 compare_reports 的结果渲染成人看的纯文本。"""
    old, new = result["old"], result["new"]
    lines = [
        f"旧: {old['report_id']}  {old['eval_set_version']}  {old['git']}",
        f"新: {new['report_id']}  {new['eval_set_version']}  {new['git']}",
        (f"可比的 Case {len(result['common'])} 条（内容相同）；改过的 {len(result['changed'])} 条、"
         f"只在旧报告里的 {len(result['only_old'])} 条、只在新报告里的 {len(result['only_new'])} 条"
         f"不参与比较"),
        (f"通过率 {_pct(result['rate_old'])} → {_pct(result['rate_new'])}"
         f"（{_signed_pct(result['rate_delta'])}，"
         f"{'超出' if result['rate_conclusive'] else '没有超出'}噪声范围）"),
        f"红线违反 {result['redlines_old']} → {result['redlines_new']} 次",
        (f"回退 {len(result['regressed'])} 条   变好 {len(result['improved'])} 条   "
         f"小幅波动 {len(result['wobble'])} 条   没变 {result['same']} 条"),
    ]
    if result["regressed"]:
        lines.append("回退:")
        lines.extend(f"  {item['id']}  {item['old']} → {item['new']}  "
                     f"[{item['first_failed_stage']}]  {item['detail']}" for item in result["regressed"])
    if result["improved"]:
        lines.append("变好:")
        lines.extend(f"  {item['id']}  {item['old']} → {item['new']}" for item in result["improved"])
    if result["wobble"]:
        lines.append("小幅波动（不下结论）:")
        lines.extend(f"  {item['id']}  {item['old']} → {item['new']}" for item in result["wobble"])
    lines.append("分环节:")
    for item in result["stages"]:
        if item["old"] is None and item["new"] is None:
            continue
        show = _f2 if item["metric"] == "mrr" else _pct
        lines.append(f"  {item['stage']}.{item['metric']}  {show(item['old'])} → {show(item['new'])}")
    if result["release_ok"]:
        lines.append("发布门槛: 通过")
    else:
        lines.append(f"发布门槛: 不通过（{'；'.join(result['release_problems'])}）")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """命令行：`python -m evals.compare <旧报告> <新报告>`。"""
    args = list(sys.argv[1:]) if argv is None else list(argv)
    paths = [Path(arg) for arg in args]
    if len(paths) != 2:
        print("用法: python -m evals.compare <旧报告> <新报告>")
        return 2
    for path in paths:
        if not path.exists():
            print(f"找不到报告: {path}")
            return 2

    old, new = (json.loads(path.read_text(encoding="utf-8")) for path in paths)
    result = compare_reports(old, new)
    print(render_compare(result))
    return 0 if result["release_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
