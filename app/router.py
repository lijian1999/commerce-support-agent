"""路由器：一次模型调用把用户这句话分到 6 条处理路径之一。"""
from typing import Literal

from langgraph.runtime import Runtime
from pydantic import BaseModel, Field

from app.context import AppContext
from app.models import ModelError, get_model
from app.state import State, last_user_text, make_step, history_text

ROUTER_SYSTEM = """你是电商客服系统的路由器，只负责判断用户这句话该走哪条处理路径，不回答问题。

路径：
- sql：只需要查数据库就能回答。数据库里有：当前客户的订单（状态、日期、金额、物流单号、买了什么商品）、账户资料、商品的价格和是否有货。
- rag：只需要查知识库文档就能回答。知识库里有：商品的规格、功能、兼容性、使用方法和故障排查；退货、保修、配送等政策和常见问题。
- supervisor：同时需要数据库和知识库，或者需要先查一个、再根据结果查另一个。
- cancel：用户要取消订单。只要这句话里有取消订单的要求就选 cancel，即使同时还问了别的。
- handoff：用户要求人工客服，或者是投诉、纠纷、要求赔偿这类不该由机器人处理的事。
- direct：问候、感谢、闲聊，或者和本店业务无关的话。

规则：
1. 商品的价格、库存在数据库（sql）；商品的规格、功能、兼容性在知识库（rag）。
2. 拿不准，或者这句话涉及不止一类，就选 supervisor。
3. 结合最近的对话理解省略和指代。例如上一轮在问订单，这一轮说「那上一个呢」，仍然是 sql。
4. 先在 reason 里用一句话写理由，再给出 route。"""


class RouteDecision(BaseModel):
    """路由模型的输出：先写理由，再给路径。"""
    reason: str = Field(description="一句话理由")
    route: Literal["sql", "rag", "supervisor", "cancel", "handoff", "direct"] = Field(description="选择的路径")


def conversation_block(state: State) -> str:
    """拼出给模型看的「对话上下文」：有历史就先列历史，再写本轮问题。"""
    question = last_user_text(state["messages"])
    history = history_text(state["messages"])
    if history:
        return f"最近的对话：\n{history}\n\n用户的问题：{question}"
    return f"用户的问题：{question}"


def router_node(state: State, runtime: Runtime[AppContext]) -> dict:
    """路由节点：一次模型调用决定这一轮走哪条路。"""
    fault = runtime.context.fault
    messages = [("system", ROUTER_SYSTEM), ("user", conversation_block(state))]
    try:
        result = get_model("router", RouteDecision, fault=fault).invoke(messages)
    except ModelError as e:
        return {
            "route": "handoff",
            "route_reason": "路由模型不可用",
            "handoff": {"reason": "system_failure", "detail": "路由模型不可用"},
            "steps": [make_step("router", "model", "模型调用失败，转人工", None, e.notes)],
        }
    value = result.value
    return {
        "route": value.route,
        "route_reason": value.reason,
        "steps": [make_step("router", "model", f"route={value.route}   理由: {value.reason}",
                            {"route": value.route, "reason": value.reason}, result.notes)],
    }
