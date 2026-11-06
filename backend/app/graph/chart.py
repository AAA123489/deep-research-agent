"""把查询结果映射成图表规格。

设计文档里写的是「模型只输出 chart spec」，这里没有照做 —— 图表类型由
结果形状决定，不走模型。理由有两条：

1. 可复现。模型挑图每次可能不一样，同一份数据两次跑出两种图，没法验。
2. 形状本身已经把话说清楚了。一行一列就该是一个大数字，时间列就该是折线，
   单维度分组就该是横向条形，用不着模型来判断。

前端拿到 spec 再决定怎么画，所以这里只描述「画什么」，不涉及任何颜色和像素。

    {"type": "stat", "label": "成交总额", "value": 123, "unit": "元"}
    {"type": "bar",  "x": "province", "y": "gmv", "title": "成交总额 · 按省份"}
    {"type": "line", "x": "month",    "y": "gmv", "title": "成交总额 · 按月"}
    {"type": "table", "reason": "行数过多"}
"""

from __future__ import annotations

import re
from typing import Any

from app.semantic.loader import MetricSpec

#: 这些列名一看就是时间，画折线
_TIME_COLUMNS = {"date", "month", "week", "quarter", "day", "dt"}

#: 维度 key 的中文名，拼标题用
_DIMENSION_LABELS = {
    "date": "日期",
    "month": "月份",
    "province": "省份",
    "category": "品类",
    "sub_category": "子类",
    "channel": "渠道",
    "payment_method": "支付方式",
}

#: 超过这个行数就不画柱状图了 —— 五十根柱子谁也读不出来
MAX_BAR_ROWS = 50

#: 折线宽容一些。一天一个点的日均曲线看三个月完全读得动，柱子不行
MAX_LINE_ROWS = 400

_ISO_DATE = re.compile(r"^\d{4}-\d{2}(-\d{2})?")


def build_chart(
    spec: MetricSpec,
    columns: list[str],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """按结果形状选图表类型。永远返回一个 dict，前端照 type 分支。"""
    if not rows or not columns:
        return {"type": "none"}

    value_column = _pick_value_column(spec, columns, rows)

    # 一个数 —— 不求图，一个大数字最直接
    if len(rows) == 1 and len(columns) == 1 and value_column:
        return {
            "type": "stat",
            "label": spec.name,
            "value": rows[0][value_column],
            "unit": spec.unit,
        }

    if value_column is None:
        return {"type": "table", "reason": "结果里没有可比较的数值列"}

    label_columns = [column for column in columns if column != value_column]
    if len(label_columns) != 1:
        return {"type": "table", "reason": "两个以上维度交叉，改用表格"}

    x = label_columns[0]
    title = f"{spec.name} · 按{_DIMENSION_LABELS.get(x, x)}"

    if _is_time_column(x, rows):
        if len(rows) > MAX_LINE_ROWS:
            return {"type": "table", "reason": f"结果有 {len(rows)} 行，改用表格"}
        return {
            "type": "line",
            "x": x,
            "y": value_column,
            "title": title,
            "unit": spec.unit,
        }

    if len(rows) > MAX_BAR_ROWS:
        return {"type": "table", "reason": f"结果有 {len(rows)} 行，柱子画不下，改用表格"}

    return {
        "type": "bar",
        "x": x,
        "y": value_column,
        "title": title,
        "unit": spec.unit,
    }


def _pick_value_column(
    spec: MetricSpec,
    columns: list[str],
    rows: list[dict[str, Any]],
) -> str | None:
    """数值列。优先用指标 key 同名的列，否则取最后一列数值。"""
    numeric = [
        column
        for column in columns
        if all(
            isinstance(row.get(column), (int, float))
            and not isinstance(row.get(column), bool)
            for row in rows
        )
    ]
    if not numeric:
        return None
    if spec.key in numeric:
        return spec.key
    return numeric[-1]


def _is_time_column(column: str, rows: list[dict[str, Any]]) -> bool:
    if column.lower() in _TIME_COLUMNS:
        return True
    first = rows[0].get(column)
    return isinstance(first, str) and bool(_ISO_DATE.match(first))
