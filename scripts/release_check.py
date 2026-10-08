"""发布前一致性校验：核对「现在重算的」「Manifest 里的」「报告里的」三者是不是同一套东西。"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.manifest import check_release, current_state, load_manifest
from evals.schema import load_cases


def main() -> int:
    parser = argparse.ArgumentParser(description="发布前一致性校验")
    parser.add_argument("--report", required=True, help="报告文件的路径")
    args = parser.parse_args()

    path = Path(args.report)
    if not path.exists():
        print(f"找不到报告: {args.report}")
        return 2
    report = json.loads(path.read_text(encoding="utf-8"))

    items = check_release(report, load_manifest(), current_state(load_cases()))
    clean = True
    for item in items:
        print(f"{item['name']}   {item['line']}   {'✓' if item['ok'] else '✗'}")
        for problem in item["problems"]:
            print(f"    - {problem}")
        clean = clean and item["ok"]
    print("结论: 一致，可以发布" if clean else "结论: 不一致，不能发布")
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
