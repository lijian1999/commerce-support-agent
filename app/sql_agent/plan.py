"""SQL Agent 输出的查询计划：模型只填结构，不写 SQL。"""
from typing import Literal

from pydantic import BaseModel, Field


class Metric(BaseModel):
    func: Literal["sum", "count", "count_distinct", "avg", "min", "max"] = Field(
        description="聚合函数：求和、计数、去重计数、平均、最小、最大"
    )
    column: str = Field(description="要聚合的列，写成 表.列 的形式")
    alias: str = Field(description="这一列在结果里的名字，小写字母开头，只能用字母、数字和下划线")


class GroupBy(BaseModel):
    column: str = Field(description="分组的列，写成 表.列 的形式")
    granularity: Literal["none", "month", "year"] = Field(
        description="分组粒度：none 按列原值，month 按月，year 按年；后两者只适用于日期列"
    )


class Filter(BaseModel):
    column: str = Field(description="要过滤的列，写成 表.列 的形式")
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte", "between", "in", "contains", "last_n_days"] = Field(
        description="比较方式，必须适用于这一列的类型"
    )
    values: list[str] = Field(
        description="值一律写成字符串；between 写两个值，in 写一到二十个值，其余写一个值"
    )


class OrderBy(BaseModel):
    key: str = Field(description="排序依据，写成 表.列 的形式，或者某个聚合的 alias")
    direction: Literal["asc", "desc"] = Field(description="升序还是降序")


class QueryPlan(BaseModel):
    reasoning: str = Field(description="用中文说明这份计划是怎么从问题推出来的")
    answerable: bool = Field(description="这个问题能不能靠一次查询回答")
    unanswerable_reason: str = Field(description="回答不了时的原因，能回答时留空")
    select: list[str] = Field(description="要原样返回的列，每项写成 表.列")
    metrics: list[Metric] = Field(description="要做聚合的列和结果名字")
    group_by: list[GroupBy] = Field(description="分组的列和粒度")
    filters: list[Filter] = Field(description="过滤条件，条件之间是并且的关系")
    order_by: list[OrderBy] = Field(description="排序，最多按几项排")
    limit: int = Field(description="最多返回多少行，0 表示交给系统决定上限")
