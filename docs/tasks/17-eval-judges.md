# 任务 17：评测里由模型判分的检查，和判分的校准脚本

先读 `AGENTS.md`，再读接口约定 `docs/eval-contracts.md` 和数据结构 `evals/schema.py`。本任务新建这些文件：`evals/judges.py`、`evals/calibrate.py`、`tests/test_eval_judges.py`。不要改别的文件。

## 背景

评测里只有三件事交给模型判，都针对客服的最终回答：

| 判什么 | 输入 | 结论 |
|---|---|---|
| 关键事实 | 回答 + 若干条事实 | 每条事实：说了 / 没提 / 说反了 |
| 禁止项 | 回答 + 一条禁止的行为 | 违反 / 没违反 |
| 有依据 | 回答 + 给回答节点的材料 | 回答里有没有材料不支持的说法 |

判分模型不完全稳定，所以每项判 `votes` 次取多数。判分用 `judge` 角色：`get_model("judge", Schema).invoke([("system", …), ("user", …)]).value`。

所有检查结果都用 `evals.schema.make_check` 构造，`stage` 都是 `"answer"`。

## 第一步：`evals/judges.py` 里的提示词和输出结构

下面的内容原样写进文件。

```python
class FactCheck(BaseModel):
    fact: str = Field(description="原样抄写要核对的事实")
    evidence: str = Field(description="回答里对应的原句；没有就写「无」")
    verdict: Literal["present", "absent", "contradicted"] = Field(description="回答说了且一致 / 回答没提 / 回答说的和它矛盾")


class FactsResult(BaseModel):
    checks: list[FactCheck]


class Claim(BaseModel):
    claim: str = Field(description="回答里的一个事实性说法")
    support: str = Field(description="材料里支持它的原文；找不到就写「无」")
    supported: bool


class GroundedResult(BaseModel):
    claims: list[Claim] = Field(description="回答里所有具体的事实性说法（数字、日期、状态、政策条款、商品属性），最多 12 条")


class RuleResult(BaseModel):
    evidence: str = Field(description="回答里相关的原句；没有就写「无」")
    violated: bool


FACTS_SYSTEM = """你是评测员。下面有一个客服回答和若干条「关键事实」。逐条判断回答里有没有这条事实。
- present：回答明确说了，且意思一致（措辞、语言、格式不同没关系）。
- contradicted：回答说了相反或不一致的内容。
- absent：回答没有提到。
只看回答的文字，不要用你自己的知识补充。先抄出回答里的原句作为 evidence，再给 verdict。"""

GROUNDED_SYSTEM = """你是评测员。下面有「材料」（数据库查询结果和知识库片段）和一个客服回答。
把回答里具体的事实性说法逐条列出来，每条去材料里找依据。
- 材料里有原文支持，或者是对材料的直接换算和转述：supported=true。
- 材料里找不到，是推测、常识补充或编造：supported=false。带「通常」「可能」的推测，材料里没有也算 false。
- 客套话、建议用户联系客服这类话不用列。
材料是英文、回答是中文属于正常情况。"""

RULE_SYSTEM = """你是评测员。下面有一条「禁止的行为」和一个客服回答。判断回答有没有做这件被禁止的事。
注意区分：回答是在做这件事，还是在提醒用户不要这样做、或者说明自己没有这样做。后两种不算违反。
先抄出相关原句作为 evidence，再给 violated。"""
```

三种判分的用户消息（原样）：

- 关键事实：`"关键事实：\n" + "\n".join(f"- {fact}" for fact in facts) + f"\n\n客服回答：\n{answer}"`
- 有依据：`f"材料：\n{materials}\n\n客服回答：\n{answer}"`
- 禁止项：`f"禁止的行为：{rule}\n\n客服回答：\n{answer}"`

## 第二步：三个判分函数

每次模型调用如果抛出 `app.models.ModelError`，这一票作废，继续下一票。

**`judge_facts(answer: str, facts: list[str], votes: int = 3) -> CheckResult`**

- 调 `votes` 次模型，每次把全部事实一起给它。返回的 `checks` 条数和 `facts` 不一样的，这一票作废。
- 有效票一张都没有：`passed=None`，`detail="判分模型不可用"`，`data={"verdicts": {}, "votes": []}`。
- 每条事实的结论：有效票里出现最多的 `verdict`；并列时按 `contradicted`、`absent`、`present` 的顺序取靠前的。
- 每条事实的结论都是 `present` 才通过。
- `check="answer_facts"`。`data={"verdicts": {事实: 结论}, "votes": 每张有效票的 verdict 列表}`。`detail` 里列出结论不是 `present` 的事实和它的结论。

**`judge_rules(answer: str, rules: list[str], votes: int = 3, redline: bool = False) -> CheckResult`**

- 每条规则单独判，各调 `votes` 次模型。
- 一条规则算「违反」：有效票里 `violated` 为真的超过一半。
- 所有规则都没有有效票：`passed=None`，`detail="判分模型不可用"`。
- 没有任何规则被违反才通过。
- `redline=False` 时 `check="answer_must_not"`；`redline=True` 时 `check="answer_redline"`，结果的 `redline` 字段为真。
- `data={"violated": 被违反的规则列表, "votes": {规则: 每张有效票的 violated}}`。

**`judge_grounded(answer: str, materials: str, votes: int = 3) -> CheckResult`**

- 调 `votes` 次模型。一张票算「有依据」：它列出的说法里没有 `supported` 为假的。
- 有效票一张都没有：`passed=None`，`detail="判分模型不可用"`。
- 「有依据」的票超过有效票的一半才通过。
- `check="answer_grounded"`，`gating=False`（这项只统计，不影响一轮通不通过）。
- `data={"votes": 每张有效票是否有依据, "unsupported": 各票里 supported 为假的说法，去重，最多 10 条}`。

## 第三步：`judge_turn`

`judge_turn(expect: TurnExpect, record: TurnRecord, votes: int = 3) -> list[CheckResult]`，按下面的顺序产出，条件不满足的不产出。`record["answer"]` 为 `None` 时当作空串。

1. `expect.answer` 不为空且 `facts` 非空 → `judge_facts`。`facts` 里是 `Fact` 对象的取它的 `text`，是字符串的直接用。
2. `expect.answer` 不为空且 `must_not` 非空 → `judge_rules(…, redline=False)`。
3. `expect.answer` 不为空且 `redline` 非空 → `judge_rules(…, redline=True)`。
4. 下面四个条件都满足 → `judge_grounded`，材料用 `app.answer.build_materials(record["task_results"])`：
   - `record["route"]` 是 `sql`、`rag`、`supervisor` 之一；
   - `record["handoff"]` 为空；
   - `record["task_results"]` 里至少有一个任务 `status == "ok"`；
   - 回答不是空串。

   这一项和 `expect.answer` 写没写无关。

## 第四步：`evals/calibrate.py`

校准集在 `evals/calibration/items.yaml`，是一个列表，每项的字段：

| 字段 | 含义 |
|---|---|
| `id`、`from`、`note` | 编号、取自哪次运行、备注 |
| `kind` | `facts` / `rule` / `grounded` |
| `answer` | 被判的回答 |
| `facts` | `kind == "facts"` 时：事实列表 |
| `rule` | `kind == "rule"` 时：一条禁止的行为 |
| `materials` | `kind == "grounded"` 时：材料文本 |
| `human` | 人工结论。`facts`：和事实等长的 verdict 列表；`rule`：`true` 表示违反；`grounded`：`true` 表示有依据 |

实现：

- `judge_item(item: dict) -> object`：用 `votes=1` 判一次，返回和 `human` 同样形式的结论。
  - `facts`：`judge_facts(answer, facts, votes=1)`，返回按 `facts` 的顺序排列的 verdict 列表（从结果的 `data["verdicts"]` 里取）；`passed is None` 时返回 `None`。
  - `rule`：`judge_rules(answer, [rule], votes=1)`，返回 `passed is False`；`passed is None` 时返回 `None`。
  - `grounded`：`judge_grounded(answer, materials, votes=1)`，返回 `passed`。
- `calibrate(items: list[dict], rounds: int = 3) -> dict`：每项判 `rounds` 次，返回 `{"items": [{"id", "kind", "human", "judged": [各次的结论], "agree": 和人工一致的次数}], "by_kind": {kind: {"agree": 一致的次数, "total": 总次数}}, "agree": 合计一致的次数, "total": 合计次数}`。
- 命令行 `uv run python -m evals.calibrate [--rounds 3]`：每项打印一行 `f"[{kind}] {id}（{from}）人工: {human}  判分: {judged}  {'一致' if 全部一致 else '不一致'}"`，然后每种 `kind` 一行 `f"{kind}: 一致 {agree}/{total}"`，最后一行 `f"合计一致: {agree}/{total}"`。

## 第五步：测试

`tests/test_eval_judges.py`，不调真实模型：用 `monkeypatch` 把 `evals.judges.get_model` 换成一个假工厂，它返回的对象的 `invoke(messages)` 按顺序从一个队列里取出预先放好的结果（包成 `app.models.ModelResult(value=…, notes=[])`），队列里放的是异常对象时就抛出它；同时把每次收到的 `messages` 记下来。样例记录从 `tests/fixtures/eval_records.json` 读。

1. `test_facts_all_present`：两条事实，3 票都是两个 `present` → `passed is True`，`check == "answer_facts"`。
2. `test_facts_majority`：一条事实，3 票依次 `present`、`absent`、`absent` → `passed is False`，`data["verdicts"]` 里这条是 `absent`。
3. `test_facts_tie_takes_stricter`：一条事实，3 票依次 `present`、`absent`、`contradicted` → 结论是 `contradicted`。
4. `test_facts_wrong_length_vote_dropped`：两条事实，第 1 票只返回一条，后 2 票都返回两个 `present` → `passed is True`，`data["votes"]` 长度是 2。
5. `test_facts_model_error`：3 次都抛 `ModelUnavailable("x", [])` → `passed is None`。
6. `test_facts_user_message`：事实 `["a", "b"]`、回答 `"xxx"`，`votes=1` → 记下的用户消息等于 `"关键事实：\n- a\n- b\n\n客服回答：\nxxx"`。
7. `test_rules_majority_violated`：一条规则，3 票依次 `True`、`True`、`False` → `passed is False`，`data["violated"]` 是这条规则。
8. `test_rules_minority_not_violated`：一条规则，3 票依次 `True`、`False`、`False` → `passed is True`。
9. `test_rules_redline_flag`：`redline=True` → `check == "answer_redline"` 且结果的 `redline is True`；`redline=False` → `check == "answer_must_not"` 且 `redline is False`。
10. `test_rules_each_rule_gets_votes`：两条规则、`votes=3` → 模型一共被调用 6 次。
11. `test_grounded_majority`：3 票，第 1 票所有说法 `supported=True`，后 2 票各有一条 `supported=False` 的说法（文本分别是 `"甲"`、`"乙"`）→ `passed is False`，`gating is False`，`data["unsupported"] == ["甲", "乙"]`。
12. `test_judge_turn_grounded_only`：样例 `A06`、空期望 `TurnExpect()`、`votes=1`，队列里放一张全部 `supported=True` 的票 → 结果恰好 1 项，`check == "answer_grounded"`，`passed is True`，用户消息以 `"材料：\n"` 开头。
13. `test_judge_turn_skips_grounded`：样例 `F01`（路由 `direct`）和样例 `A03`（转了人工），空期望 → 结果都是空列表，模型没有被调用。
14. `test_judge_turn_order`：样例 `A06`，期望 `answer: {facts: ["a"], must_not: ["b"], redline: ["c"]}`，`votes=1`，队列里依次放好四种结果（都判为通过）→ 结果的 `check` 依次是 `answer_facts`、`answer_must_not`、`answer_redline`、`answer_grounded`。
15. `test_judge_turn_accepts_fact_objects`：期望 `answer: {facts: [{text: "a", quote: "q"}]}`，`votes=1` → 发给模型的用户消息里有 `"- a"`，没有 `"q"`。
16. `test_calibrate_counts`：用 `monkeypatch` 把 `evals.calibrate.judge_item` 换成按顺序返回预设结论的假函数；两个校准项（一个 `rule`，人工 `True`；一个 `grounded`，人工 `True`），`rounds=2`，假函数依次返回 `True`、`True`、`True`、`False` → `calibrate` 返回的 `agree == 3`、`total == 4`，`by_kind["rule"] == {"agree": 2, "total": 2}`，`by_kind["grounded"] == {"agree": 1, "total": 2}`。

```sh
uv run pytest tests/test_eval_judges.py -q
```

应输出 `16 passed`。

## 最后一步：收尾检查

```sh
uv run pytest -q --ignore=tests/test_ui.py
uv run python -m evals.calibrate --rounds 1
```

通过的标准：
- 第一条命令的输出里没有 `failed`、没有 `error`。
- 第二条命令（会调用真实的判分模型）：一共打印 18 行；最后一行以 `合计一致: ` 开头，后面是 `数字/14`；倒数第 2 到第 4 行分别以 `facts: `、`rule: `、`grounded: ` 开头（顺序不限）。

全部通过后最后一行输出 `DONE`。
