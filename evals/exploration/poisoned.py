"""知识库侧的对抗：在临时目录里建一个夹带诱导文档的索引，用它跑 questions/poisoned.json。

不改 data/documents，也不动 data/index：文档复制到临时目录，再加上 poison_docs/ 里的三篇。
用法: uv run python evals/exploration/poisoned.py [输出文件，默认 runs/poisoned.json]
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))   # 项目根目录
sys.path.insert(0, str(HERE))

from app import config
from app.rag.embedding import embed_texts
from app.rag.index import IndexStore, runtime_config

# 诱导文档放进哪个子目录
TARGET = {"order_cancellation_help.md": "policies", "price_match_notice.md": "policies",
          "TECH-AUD-020-notes.md": "products"}

work = Path(tempfile.mkdtemp(prefix="csa-poisoned-"))
shutil.copytree(config.DOCS_DIR, work / "docs")
for name, sub in TARGET.items():
    shutil.copy(HERE.parent / "poison_docs" / name, work / "docs" / sub / name)

# 让这个进程里所有的 IndexStore() 都指向临时目录里的索引
IndexStore.__init__.__defaults__ = (work / "index",)
store = IndexStore()
manifest = store.build(work / "docs", runtime_config(), embed_texts)
store.publish(manifest["version"], runtime_config())
print(f"临时索引 {manifest['version']}: {manifest['stats']}   目录 {work}")

import probe  # noqa: E402  放在改完默认目录之后

results = [probe.run_probe(p) for p in json.load(open(HERE / "questions" / "poisoned.json"))]
out = sys.argv[1] if len(sys.argv) > 1 else str(HERE / "runs" / "poisoned.json")
json.dump(results, open(out, "w"), ensure_ascii=False, indent=1)
for r in results:
    print(probe.show(r))
shutil.rmtree(work)
