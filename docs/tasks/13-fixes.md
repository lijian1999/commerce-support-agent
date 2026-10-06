# 任务 13：Router 提示词和命令行的三处修正

先读 `AGENTS.md`。本任务修改这些文件：`app/router.py`、`cli.py`、`tests/test_graph.py`。不要改别的文件。

## 第一步：替换 Router 的系统提示词

`app/router.py` 里的常量 `ROUTER_SYSTEM` 整段替换成下面的原文（其余代码不动）：

```
你是电商客服系统的路由器，只负责判断用户这句话该走哪条处理路径，不回答问题。

路径：
- sql：只需要查数据库就能回答。数据库里有：当前客户自己的订单（状态、日期、金额、物流单号、买了什么商品）、账户资料，以及商品的价格和是否有货。
- rag：只需要查知识库文档就能回答。知识库里有：商品的规格、功能、兼容性、使用方法和故障排查；退货、保修、配送等政策和常见问题。知识库里没有任何客户的订单信息。
- supervisor：同时需要数据库和知识库，或者需要先查一个、再根据结果查另一个。
- cancel：用户要取消订单。只要这句话里有取消订单的要求就选 cancel，即使同时还问了别的。
- handoff：用户要求人工客服，或者是投诉、纠纷、要求赔偿这类不该由机器人处理的事。
- direct：问候、感谢、闲聊，或者和本店业务无关的话。

规则：
1. 商品的价格、库存在数据库（sql）；商品的规格、功能、兼容性、保修在知识库（rag）。
2. 「我买的」「我订的」「我的订单」「我最近买的那个……」这类说法指的是当前客户自己的数据，要先查数据库才知道具体是哪个订单、哪个商品。这类问题如果还要知道商品的规格、保修、兼容性或者任何政策，就选 supervisor。
3. 拿不准，或者这句话涉及不止一类，就选 supervisor。
4. 结合最近的对话理解省略和指代。例如上一轮在问订单，这一轮说「那上一个呢」，仍然是 sql。
5. 先在 reason 里用一句话写理由，再给出 route。
```

```sh
uv run python -c "
from app.router import ROUTER_SYSTEM
print(ROUTER_SYSTEM.count('规则：'), '「我买的」' in ROUTER_SYSTEM, ROUTER_SYSTEM.strip().splitlines()[-1][:2])"
```

应输出 `1 True 5.`。

## 第二步：命令行打印最终回复

现状：`cli.py` 的 `run_and_print` 只打印每个步骤。转人工时，`handoff` 步骤的摘要是工单信息，给用户的回复文本（`values["answer"]`）没有被打印出来。

改成：在 `run_and_print` 里记住最后一个打印过的步骤的 `summary`。一轮正常结束时（没有停在确认上），在打印统计行**之前**判断：`values.get("answer")` 非空，并且不等于最后一个步骤的 `summary` → 打印一行 `f"客服> {answer}"`。等于时不打印（`answer`、`cancel/reply` 这两种步骤的摘要本身就是回复文本，不重复打印）。

## 第三步：交互模式每一轮重新建 Context

现状：`cli.py` 的 `main` 在交互模式里只在开头调用一次 `make_context`，所有轮次共用。知识库的线上版本应该在每次请求开始时读一次，所以要每一轮重新建。

改成：交互模式的循环里，每读到一行非空的输入，先 `ctx = make_context(args.customer, args.today, args.fault, session_id=thread_id)`，再 `run_and_print(...)`。开头那一次 `make_context` 和它后面打印的「会话 …」那一行保留不变。

## 第四步：在 `tests/test_graph.py` 末尾追加 3 个测试

用这个文件里已有的假模型、假检索和清理 fixture。`capsys` 是 pytest 自带的 fixture，用来读打印出来的内容。

1. `test_cli_prints_reply_for_handoff`：路由 `handoff`、消息 `帮我转人工`、回答模型返回 `用户要求人工。` → 调用 `cli.run_and_print(graph, ctx, thread, "帮我转人工")`，打印的内容里有一行以 `客服> 已为您转接人工客服，工单号 TKT-` 开头，并且这一行出现在以 `—— 有模型参与的步骤` 开头的那一行之前。
2. `test_cli_does_not_repeat_answer`：路由 `direct`、回答模型返回 `您好！` → 调用 `cli.run_and_print(...)`，打印的内容里没有 `客服>`，有一行是 `[answer]` 加 10 个空格加 `您好！`（即 `"[answer]".ljust(18) + "您好！"`）。
3. `test_cli_interactive_rebuilds_context_each_turn`：用 `monkeypatch` 把 `cli.make_context` 换成一个记录调用次数、并返回 `AppContext(customer_id="CUST-002", index_version="v1", session_id="s-graph")` 的函数；把 `builtins.input` 换成依次返回 `"你好"`、`""`、`"谢谢"`、`"exit"` 的函数；路由队列放两个 `direct`，回答队列放两个文本；调用 `cli.main([])` → 返回 0，假的 `make_context` 一共被调用 3 次（开头 1 次，两轮各 1 次）。

```sh
uv run pytest tests/test_graph.py -q
```

应输出 `27 passed`。

## 最后一步：收尾检查

```sh
uv run pytest -q
uv run python cli.py --customer CUST-001 "我要投诉，帮我转人工"
uv run python cli.py --customer CUST-001 "我最近买的那个显示器保修多久？另外退款多久到账？"
```

通过的标准：
- `uv run pytest -q` 的输出里没有 `failed`、没有 `error`。
- 第一条命令行：有一行以 `[handoff]` 开头，它后面有一行以 `客服> 已为您转接人工客服，工单号` 开头。
- 第二条命令行：`[router]` 那一行里是 `route=supervisor`；有以 `[planner]`、`[validate]` 开头的行。

全部通过后最后一行输出 `DONE`。
