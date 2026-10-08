# 电商智能客服 Agent（精简重写版）

设计文档见 `docs/design.md`，模块接口见 `docs/contracts.md`。评测（设计文档第 6 节）在 `evals/`：146 条 Case、运行器、检查和判分、报告、Manifest 和发布前校验，命令见设计文档 6.1 第十三部分；接口约定见 `docs/eval-contracts.md`。写设计之前跑的探查在 `evals/exploration/`。

## 准备

```sh
docker compose up -d --wait            # MySQL 8.4，容器 csa-mysql，端口 3307
uv run python scripts/import_mysql.py  # 建表、导入数据、建 agent_ro / agent_rw 两个账号；重跑即把数据库恢复到初始状态
uv run python scripts/build_index.py   # 构建知识库索引的新版本（只对变化的片段生成 Embedding）
uv run python scripts/index.py publish v1
```

`.env` 里填 `LANGSMITH_API_KEY` 后自动开启追踪；留空则关闭。模型和 Embedding 的地址也在 `.env`。

## 运行

```sh
uv run python cli.py --customer CUST-001 "我最近一个订单到哪了？另外退货政策是什么？"
uv run python cli.py --customer CUST-001            # 交互模式，多轮
uv run streamlit run ui.py                          # 聊天界面
uv run pytest -q                                    # 单元测试（test_ui.py 会调用真实模型）
```

命令行参数：`--customer`（不给就是匿名）、`--fault off|primary_down|all_down`、`--yes` / `--no`（自动回答取消确认）、`--index-version v2`（指定知识库版本，可以是未发布的）、`--verbose`（打印每一步的完整输出）。

## 演示

| 要看什么 | 命令 |
|---|---|
| 单领域，固定 3 次模型调用 | `cli.py --customer CUST-001 "我在键盘上一共花了多少钱？"` |
| 跨领域，一次出计划、并行执行 | `cli.py --customer CUST-001 "我最近一个订单到哪了？另外退货政策是什么？"` |
| 有依赖的计划（先查库再查文档） | `cli.py --customer CUST-001 "我最近买的那个显示器保修多久？另外退款多久到账？"` |
| 身份由 Runtime 注入：查别人的订单得到 0 行 | `cli.py --customer CUST-001 "查一下订单 ORD-2025-0031 的状态"` |
| 匿名用户查订单被编译器拒绝 | `cli.py "我最近一个订单到哪了？"` |
| 取消订单（中断确认；`--customer CUST-002` 有两个处理中的订单） | `cli.py --customer CUST-002 "帮我取消订单 ORD-2025-0031"` |
| 转人工 | `cli.py --customer CUST-001 "我要投诉，帮我转人工"` |
| 主模型故障 → 重试 → 切备用模型 | `cli.py --customer CUST-001 --fault primary_down "我一共下过几单？"` |
| 主备都故障 → 转人工 | `cli.py --customer CUST-001 --fault all_down "我一共下过几单？"` |
| 知识库增量更新 | 改 `data/documents/policies/return_policy.md` 的一个小节，再跑 `scripts/build_index.py` |
| 发布与回滚 | `scripts/index.py status` / `publish v2` / `rollback` |
| 知识库里的诱导内容不会触发操作 | 把 `data/demo/injection_demo.md` 复制到 `data/documents/policies/`，构建并发布，问「订单取消有什么规定？」，看完后 `rollback` 并删掉这篇文档 |
