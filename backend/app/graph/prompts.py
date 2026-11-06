"""拼给模型的提示词。

把语义层里的口径契约原样交给模型 —— 模型不需要「理解」口径，
只需要照着抄。抄错了守卫会拦下来。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.semantic.loader import MetricRegistry, MetricSpec

_SYSTEM_TEMPLATE = """\
你是一个电商数据分析助手，负责把中文问题翻译成 DuckDB SQL。

输出要求：
1. 只输出一条 SELECT 语句。不要 markdown 代码块标记，不要解释，不要分号。
2. 指标的计算表达式必须和下面给出的完全一致，不要改写、不要四舍五入、不要换算单位。
3. WHERE 里必须包含「必带过滤」列出的每一条条件。
4. 需要的表都写在「数据来源」里；如果「必带 JOIN」非空，ON 条件也必须写进去。
5. 只能按「可用维度」里的字段 GROUP BY。时间维度用 DATE_TRUNC('粒度', 字段)。
6. 问题里没给时间范围时，用「默认时间窗口」，基准日是 {data_end}。
7. 输出列名用英文小写别名。

数据覆盖范围：{data_start} 到 {data_end}（历史快照，不是真实今天）。

【本次要算的指标】
key: {key}
名称: {name}
单位: {unit}
计算表达式: {expression}
数据来源: {source}
必带过滤: {filters}
必带 JOIN: {joins}
默认时间窗口: {default_window}
时间字段: {time_field}
口径说明: {description}

【可用维度】
{dimensions}
"""

_REPAIR_TEMPLATE = """\
上一条 SQL 没有通过口径校验：

{sql}

校验器给出的问题：
{error}

请修正后重新输出一条完整的 SQL。
"""


def build_messages(
    question: str,
    spec: MetricSpec,
    registry: MetricRegistry,
    *,
    bounds: tuple[date, date],
    previous_sql: str | None = None,
    error: str | None = None,
) -> list[dict[str, str]]:
    """返回 OpenAI 兼容格式的 messages。"""
    system = _SYSTEM_TEMPLATE.format(
        data_start=bounds[0].isoformat(),
        data_end=bounds[1].isoformat(),
        key=spec.key,
        name=spec.name,
        unit=spec.unit,
        expression=spec.expression,
        source=(spec.source or "").strip() or "（由组件指标提供）",
        filters="\n  - ".join([""] + list(spec.required_filters))
        if spec.required_filters
        else "（无，由组件指标提供）",
        joins="\n  - ".join([""] + list(spec.required_joins))
        if spec.required_joins
        else "（无）",
        default_window=spec.default_window,
        time_field=spec.time_field,
        description=spec.description,
        dimensions=_dimension_block(spec, registry),
    )

    messages: list[dict[str, str]] = [{"role": "system", "content": system}]

    if previous_sql:
        messages.append(
            {
                "role": "user",
                "content": _REPAIR_TEMPLATE.format(
                    sql=previous_sql,
                    error=error or "（没有详细信息）",
                ),
            }
        )

    messages.append({"role": "user", "content": f"问题：{question}\n\n请给出 SQL。"})
    return messages


def _dimension_block(spec: MetricSpec, registry: MetricRegistry) -> str:
    lines: list[str] = []
    for name in spec.dimensions:
        dimension = registry.dimensions[name]
        if dimension.is_time:
            granularities = "/".join(dimension.granularities)
            lines.append(
                f"- {name}: {dimension.expr}"
                f"（时间维度，粒度 {granularities}，用 DATE_TRUNC('粒度', {dimension.expr})）"
            )
        else:
            lines.append(f"- {name}: {dimension.expr}")
    return "\n".join(lines) if lines else "（无）"


def describe_available(registry: MetricRegistry) -> list[dict[str, Any]]:
    """给澄清节点用：列出用户能问哪些指标。"""
    return [
        {"key": metric.key, "name": metric.name, "unit": metric.unit}
        for metric in registry.visible()
    ]
