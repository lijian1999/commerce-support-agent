"""评测集的版本标识和发布前一致性校验。

评测集、判分方式各算一个内容指纹；Manifest 把评测集和环境的标识存进 Git；
报告带着它运行时的全部标识；发布前核对「现在重算的」「Manifest 里的」「报告里的」三者是否一致。
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from app import config
from evals import env
from evals.schema import EVALS_DIR, MANIFEST_PATH, Case

# 判分方式的组成部分：代码判、模型判、判分校准集，按约定这个顺序算 hash
EVALUATOR_FILES = (EVALS_DIR / "checks.py", EVALS_DIR / "judges.py",
                   EVALS_DIR / "calibration" / "items.yaml")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def case_hash(case: Case) -> str:
    """一个 Case 的内容指纹：它的字段全部序列化后算 SHA-256。"""
    payload = json.dumps(case.model_dump(mode="json"), sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"))
    return _sha(payload)


def eval_set_version(hashes: dict[str, str]) -> str:
    """评测集版本：所有 `id:hash` 按 id 排序后拼接算 SHA-256。"""
    joined = "\n".join(f"{case_id}:{case_hash_value}" for case_id, case_hash_value in sorted(hashes.items()))
    return "ds-" + _sha(joined)[:8]


def diff_cases(old: dict[str, str], new: dict[str, str]) -> dict:
    """两份 {id: hash} 的差异，三个列表都按 id 排序。"""
    return {
        "added": sorted(set(new) - set(old)),
        "removed": sorted(set(old) - set(new)),
        "modified": sorted(i for i in set(old) & set(new) if old[i] != new[i]),
    }


def describe_diff(diff: dict) -> str:
    """把差异写成一句话。"""
    groups = [("修改", diff.get("modified", [])), ("新增", diff.get("added", [])),
              ("删除", diff.get("removed", []))]
    parts = [f"{name} {len(ids)} 个（{'、'.join(ids[:5])}{' 等' if len(ids) > 5 else ''}）"
             for name, ids in groups if ids]
    return "，".join(parts) if parts else "没有变化"


def evaluator_version(files: list[Path] | None = None, judge_model: str | None = None) -> dict:
    """判分方式的版本：判分相关的文件内容加上判分模型的名字。文件不存在当作空内容。"""
    paths = [Path(f) for f in files] if files is not None else list(EVALUATOR_FILES)
    judge = judge_model if judge_model is not None else config.role_target("judge").model
    parts = [p.read_text(encoding="utf-8", errors="replace") if p.exists() else "" for p in paths]
    version = "ev-" + _sha("\n".join(parts + [judge]))[:8]
    return {"version": version, "judge_model": judge}


def cases_hashes(cases: list[Case]) -> dict[str, str]:
    return {case.id: case_hash(case) for case in cases}


def current_state(cases: list[Case]) -> dict:
    """现在重算的全部标识：评测集、候选 Agent、环境、判分方式。"""
    hashes = cases_hashes(cases)
    return {
        "eval_set_version": eval_set_version(hashes),
        "cases": hashes,
        "agent": env.agent_version(),
        "env": env.env_fingerprint(),
        "evaluator": evaluator_version(),
    }


def build_manifest(cases: list[Case]) -> dict:
    """Manifest：评测集和环境现在的样子（不含 Agent，那是某个候选版本的事）。"""
    hashes = cases_hashes(cases)
    return {
        "eval_set_version": eval_set_version(hashes),
        "cases": hashes,
        "env": env.env_fingerprint(),
        "evaluator": evaluator_version(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def write_manifest(manifest: dict, path: Path = MANIFEST_PATH) -> None:
    Path(path).write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")


def load_manifest(path: Path = MANIFEST_PATH) -> dict | None:
    path = Path(path)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def check_release(report: dict, manifest: dict | None, current: dict) -> list[dict]:
    """发布前三项核对：报告、Manifest、现在重算的是不是同一套东西。"""
    items: list[dict] = []

    # 第 1 项：评测集版本
    cur_set = current["eval_set_version"]
    rep_set = report.get("eval_set_version")
    line = f"本地重算 {cur_set}   Manifest {manifest['eval_set_version'] if manifest else '无'}   报告 {rep_set}"
    problems: list[str] = []
    if manifest is None:
        problems.append("没有 Manifest，先运行 scripts/eval_manifest.py")
    elif manifest.get("eval_set_version") != cur_set:
        problems.append("Case 改了但没有重新生成 Manifest："
                        + describe_diff(diff_cases(manifest.get("cases", {}), current["cases"])))
    if rep_set != cur_set:
        problems.append(f"报告是在 {rep_set} 上跑的，当前评测集是 {cur_set}："
                        + describe_diff(diff_cases(report.get("cases", {}), current["cases"])))
    items.append({"name": "评测集版本", "ok": not problems, "line": line, "problems": problems})

    # 第 2 项：Agent 版本
    cur_agent = current["agent"]
    rep_agent = report.get("agent", {})
    line = (f"当前候选 {cur_agent['git']} / 模型 {cur_agent['models_hash']} / 索引 "
            f"{cur_agent['index_version']}（{cur_agent['index_hash']}）")
    problems = []
    if str(cur_agent["git"]).endswith("-dirty"):
        problems.append("工作区有未提交的修改，不能用于发布")
    if str(rep_agent.get("git", "")).endswith("-dirty"):
        problems.append("报告是在有未提交修改的代码上跑的")
    if rep_agent.get("git") != cur_agent["git"]:
        problems.append(f"报告是在代码 {rep_agent.get('git')} 上跑的，当前是 {cur_agent['git']}")
    if rep_agent.get("models_hash") != cur_agent["models_hash"]:
        rep_models, cur_models = rep_agent.get("models", {}), cur_agent["models"]
        roles = sorted({r for r in set(rep_models) | set(cur_models) if rep_models.get(r) != cur_models.get(r)})
        problems.append("模型配置不同，不一样的角色：" + "、".join(roles))
    if rep_agent.get("index_hash") != cur_agent["index_hash"]:
        problems.append(f"知识库索引不同：报告用的是 {rep_agent.get('index_version')}（{rep_agent.get('index_hash')}）"
                        f"，当前是 {cur_agent['index_version']}（{cur_agent['index_hash']}）")
    items.append({"name": "Agent 版本", "ok": not problems, "line": line, "problems": problems})

    # 第 3 项：环境
    cur_env = current["env"]
    rep_env = report.get("env", {})
    line = f"数据库 {cur_env['db_hash']}   日期 {cur_env['today']}   Evaluator {current['evaluator']['version']}"
    problems = []
    if rep_env.get("db_hash") != cur_env["db_hash"]:
        problems.append(f"数据库内容不同：报告是在 {rep_env.get('db_hash')} 上跑的，当前是 {cur_env['db_hash']}")
    if manifest and manifest.get("env", {}).get("db_hash") != cur_env["db_hash"]:
        problems.append(f"数据库内容和 Manifest 记录的不同（Manifest: {manifest['env']['db_hash']}，"
                        f"当前: {cur_env['db_hash']}），重跑 scripts/import_mysql.py 可以恢复初始数据")
    if rep_env.get("today") != cur_env["today"]:
        problems.append(f"Runtime 的日期不同：报告 {rep_env.get('today')}，当前 {cur_env['today']}")
    if report.get("evaluator", {}).get("version") != current["evaluator"]["version"]:
        problems.append(f"判分方式变了：报告用的是 {report['evaluator']['version']}，"
                        f"当前是 {current['evaluator']['version']}")
    items.append({"name": "环境", "ok": not problems, "line": line, "problems": problems})

    return items
