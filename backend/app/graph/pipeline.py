"""LangGraph 主循环。

把前六步串成一条能跑的链路::

    问题
     │
     ▼
  understand ──匹配不到指标──> clarify ──> 结束
     │ 匹配到
     ▼
   generate ──生成失败──> give_up ──> 结束
     │ SQL
     ▼
    guard ──通过──> execute ──> summarize ──> 结束
     │ 不通过
     ▼
    repair ──> guard（最多重试 max_repair 次）

两个环：守卫→修复→守卫，是这条链路的核心。
模型第一次写不对很正常，关键是要能拿到「哪里不对」再改。
"""

from __future__ import annotations

import re
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.semantic.loader import MetricRegistry, MetricSpec

from .chart import build_chart
from .executor import Executor
from .guards import GuardError, check
from .llm import SQLGenerator
from .state import AnalysisState, event

#: 第一次生成之后还能重修几次。
DEFAULT_MAX_REPAIR = 2


def build_graph(
    registry: MetricRegistry,
    executor: Executor,
    generator: SQLGenerator,
    *,
    max_repair: int = DEFAULT_MAX_REPAIR,
):
    """编译图。返回的对象可以直接 ``.invoke({"question": ...})``。"""
    max_attempts = max_repair + 1

    # -- 节点 --------------------------------------------------------------

    def understand(state: AnalysisState) -> dict[str, Any]:
        spec = registry.resolve(state["question"])
        if spec is None:
            return {
                "metric_key": None,
                "trace": [event("understand", "没匹配到任何指标")],
            }
        return {
            "metric_key": spec.key,
            "attempts": 0,
            "guard_error": None,
            "status": "running",
            "trace": [event("understand", f"匹配到 {spec.key}（{spec.name}）")],
        }

    def clarify(state: AnalysisState) -> dict[str, Any]:
        names = "、".join(f"{m.name}（{m.key}）" for m in registry.visible())
        return {
            "status": "clarify",
            "answer": f"没太确定你想看哪个指标。目前支持：{names}。换个说法再问一次？",
            "trace": [event("clarify", "追问用户")],
        }

    def generate(state: AnalysisState) -> dict[str, Any]:
        spec = registry.get(state["metric_key"])
        try:
            sql = generator.generate(
                state["question"], spec, registry, bounds=executor.bounds()
            )
        except Exception as exc:
            return {
                "sql": None,
                "status": "failed",
                "answer": f"生成 SQL 失败：{exc}",
                "trace": [event("generate", f"失败：{exc}")],
            }

        attempt = state.get("attempts", 0) + 1
        return {
            "sql": sql.strip(),
            "attempts": attempt,
            "trace": [event("generate", f"第 {attempt} 次生成 SQL")],
        }

    def repair(state: AnalysisState) -> dict[str, Any]:
        spec = registry.get(state["metric_key"])
        try:
            sql = generator.generate(
                state["question"],
                spec,
                registry,
                bounds=executor.bounds(),
                previous_sql=state.get("sql"),
                error=state.get("guard_error"),
            )
        except Exception as exc:
            return {
                "sql": None,
                "status": "failed",
                "answer": f"修复 SQL 失败：{exc}",
                "trace": [event("repair", f"失败：{exc}")],
            }

        attempt = state.get("attempts", 0) + 1
        return {
            "sql": sql.strip(),
            "attempts": attempt,
            "trace": [event("repair", f"第 {attempt} 次生成 SQL（修复）")],
        }

    def guard(state: AnalysisState) -> dict[str, Any]:
        spec = registry.get(state["metric_key"])
        try:
            check(state["sql"] or "", spec, registry)
        except GuardError as exc:
            return {
                "guard_error": exc.hint,
                "trace": [event("guard", f"未通过：{exc.code}")],
            }
        except Exception as exc:
            return {
                "guard_error": str(exc),
                "trace": [event("guard", f"未通过：{exc}")],
            }
        return {"guard_error": None, "trace": [event("guard", "通过")]}

    def execute(state: AnalysisState) -> dict[str, Any]:
        try:
            result = executor.run(state["sql"] or "")
        except Exception as exc:
            return {
                "status": "failed",
                "answer": f"SQL 执行失败：{exc}",
                "trace": [event("execute", f"失败：{exc}")],
            }
        return {
            "columns": result.columns,
            "rows": result.rows,
            "status": "ok",
            "trace": [
                event("execute", f"{result.row_count} 行，{result.elapsed_ms:.0f} ms")
            ],
        }

    def summarize(state: AnalysisState) -> dict[str, Any]:
        spec = registry.get(state["metric_key"])
        columns = state.get("columns", [])
        rows = state.get("rows", [])
        return {
            "answer": summarize_result(spec, columns, rows),
            "chart": build_chart(spec, columns, rows),
            "trace": [event("summarize", "结论只用结果里的数字拼，不经过模型")],
        }

    def give_up(state: AnalysisState) -> dict[str, Any]:
        reason = state.get("guard_error") or state.get("answer") or "未知原因"
        return {
            "status": "failed",
            "answer": (
                f"没能生成合规的 SQL（已尝试 {state.get('attempts', 0)} 次）。"
                f"最后一次的问题：{reason}"
            ),
            "trace": [event("give_up", "超过重试上限")],
        }

    # -- 路由 --------------------------------------------------------------

    def after_understand(state: AnalysisState) -> str:
        return "clarify" if not state.get("metric_key") else "generate"

    def after_generate(state: AnalysisState) -> str:
        return "guard" if state.get("sql") else "give_up"

    def after_guard(state: AnalysisState) -> str:
        if not state.get("guard_error"):
            return "execute"
        if state.get("attempts", 0) >= max_attempts:
            return "give_up"
        return "repair"

    # -- 装配 --------------------------------------------------------------

    graph = StateGraph(AnalysisState)

    for name, node in (
        ("understand", understand),
        ("clarify", clarify),
        ("generate", generate),
        ("guard", guard),
        ("repair", repair),
        ("execute", execute),
        ("summarize", summarize),
        ("give_up", give_up),
    ):
        graph.add_node(name, node)

    graph.add_edge(START, "understand")
    graph.add_conditional_edges(
        "understand", after_understand, {"generate": "generate", "clarify": "clarify"}
    )
    graph.add_conditional_edges(
        "generate", after_generate, {"guard": "guard", "give_up": "give_up"}
    )
    graph.add_conditional_edges(
        "guard",
        after_guard,
        {"execute": "execute", "repair": "repair", "give_up": "give_up"},
    )
    graph.add_conditional_edges(
        "repair", after_generate, {"guard": "guard", "give_up": "give_up"}
    )
    graph.add_edge("execute", "summarize")

    for terminal in ("clarify", "summarize", "give_up"):
        graph.add_edge(terminal, END)

    return graph.compile()


# ---------------------------------------------------------------------------
# 结论
# ---------------------------------------------------------------------------


def summarize_result(
    spec: MetricSpec,
    columns: list[str],
    rows: list[dict[str, Any]],
) -> str:
    """用查回来的 rows 拼一句话。

    这里刻意不走模型 —— 设计文档里的硬要求是「结论里的每个数字都要能在
    rows 里找到」。模板拼字符串是满足这条最省事、也最不会出错的写法。
    """
    if not rows:
        return "没有查到符合条件的数据。"
    if not columns:
        return "查询没有返回任何列。"

    if len(rows) == 1 and len(columns) == 1:
        return f"{spec.name}为 {_fmt(rows[0][columns[0]], spec.unit)}。"

    value_column = _pick_value_column(spec, columns, rows)
    if value_column is None:
        return f"共 {len(rows)} 行结果。"

    label_columns = [column for column in columns if column != value_column]
    ordered = sorted(rows, key=lambda row: _number(row.get(value_column)), reverse=True)

    if len(ordered) == 1:
        return f"{spec.name}为 {_fmt(ordered[0][value_column], spec.unit)}。"

    top, bottom = ordered[0], ordered[-1]
    return (
        f"{_count(ordered, label_columns)}。"
        f"最高的是 {_label(top, label_columns)}"
        f"（{_fmt(top[value_column], spec.unit)}），"
        f"最低的是 {_label(bottom, label_columns)}"
        f"（{_fmt(bottom[value_column], spec.unit)}）。"
    )


def _pick_value_column(
    spec: MetricSpec,
    columns: list[str],
    rows: list[dict[str, Any]],
) -> str | None:
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


_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _count(rows: list[dict[str, Any]], label_columns: list[str]) -> str:
    """「共 90 组」读着别扭，按时间分组时该说「共 90 天」。"""
    if len(label_columns) == 1:
        values = [row.get(label_columns[0]) for row in rows]
        if all(isinstance(value, str) and _ISO_DATE.match(value) for value in values):
            unit = "个月" if all(value.endswith("-01") for value in values) else "天"
            return f"共 {len(rows)} {unit}"
    return f"共 {len(rows)} 组"


def _label(row: dict[str, Any], label_columns: list[str]) -> str:
    parts = [_human(row[column]) for column in label_columns if column in row]
    return " / ".join(parts) if parts else "合计"


def _human(value: Any) -> str:
    """把 2026-06-01 这种机器日期说成人话。"""
    if isinstance(value, str):
        matched = _ISO_DATE.match(value)
        if matched:
            year, month, day = matched.groups()
            if day == "01":
                return f"{year}年{int(month)}月"
            return f"{year}年{int(month)}月{int(day)}日"
    return str(value)


def _number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) else float("-inf")


def _fmt(value: Any, unit: str) -> str:
    if isinstance(value, bool) or value is None:
        return str(value)

    # 百分比指标在库里存的是 0.1838，展示时要乘 100
    if unit == "%" and isinstance(value, (int, float)):
        return f"{value * 100:.2f}%"

    if isinstance(value, int):
        return f"{value:,} {unit}"
    if isinstance(value, float):
        if abs(value) >= 1000:
            text = f"{value:,.2f}"
        elif abs(value) < 1:
            text = f"{value:.4f}"
        else:
            text = f"{value:.2f}"
        return f"{text} {unit}"
    return f"{value} {unit}"


# ---------------------------------------------------------------------------
# 便捷入口
# ---------------------------------------------------------------------------


def run(
    question: str,
    *,
    registry: MetricRegistry,
    executor: Executor,
    generator: SQLGenerator,
    max_repair: int = DEFAULT_MAX_REPAIR,
) -> AnalysisState:
    graph = build_graph(registry, executor, generator, max_repair=max_repair)
    return graph.invoke({"question": question, "trace": []})


def stream(
    question: str,
    *,
    registry: MetricRegistry,
    executor: Executor,
    generator: SQLGenerator,
    max_repair: int = DEFAULT_MAX_REPAIR,
):
    """和 :func:`run` 跑同一张图，但每走完一个节点就往外吐一条进度。

    产出 ``(事件, 当时的状态)``；最后一条事件的 ``node`` 是 ``None``，
    这时第二个元素就是最终状态。前端拿它做「看着它一步步走」的效果。
    """
    graph = build_graph(registry, executor, generator, max_repair=max_repair)

    cursor = 0
    final: AnalysisState | None = None

    for snapshot in graph.stream({"question": question, "trace": []}, stream_mode="values"):
        final = snapshot
        trace = snapshot.get("trace", [])
        for item in trace[cursor:]:
            yield item, snapshot
        cursor = len(trace)

    if final is not None:
        yield None, final
