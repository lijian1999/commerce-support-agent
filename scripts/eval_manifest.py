"""生成或更新评测集 Manifest：把现在重算的评测集和环境标识写进 evals/manifest.json。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.manifest import build_manifest, describe_diff, diff_cases, load_manifest, write_manifest
from evals.schema import load_cases


def main() -> None:
    cases = load_cases()
    old = load_manifest()
    manifest = build_manifest(cases)
    write_manifest(manifest)
    print(f"评测集版本 {manifest['eval_set_version']}，共 {len(cases)} 个 Case")
    if old is None:
        print("相对上一份 Manifest：之前没有 Manifest")
    else:
        print(f"相对上一份 Manifest：{describe_diff(diff_cases(old.get('cases', {}), manifest['cases']))}")


if __name__ == "__main__":
    main()
