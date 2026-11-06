"""命令行入口。

    # 接真实模型（OpenAI 兼容接口）
    python -m app.graph "2026年6月各省的销售额"

    # 不接模型，用预置脚本跑通整条链路
    python -m app.graph --demo

需要环境变量 LLM_API_KEY / LLM_MODEL，可选 LLM_BASE_URL。
"""

from __future__ import annotations

import argparse
import sys
import unicodedata
from pathlib import Path
from typing import Any

from app.semantic import load_registry

from .executor import Executor
from .llm import OpenAICompatGenerator, ScriptedGenerator
from .pipeline import DEFAULT_MAX_REPAIR, run

_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB = _ROOT / "data" / "ecommerce.duckdb"
SEMANTIC_DIR = Path(__file__).resolve().parents[1] / "semantic"

_BASE_FILTER = "o.status IN ('paid', 'shipped', 'completed') AND o.paid_at IS NOT NULL"
_BASE_JOIN = (
    "JOIN order_items oi ON oi.order_id = o.order_id "
    "JOIN products p ON p.product_id = oi.product_id"
)

# --demo 用的预置 SQL ------------------------------------------------------

_SQL_GMV_BY_PROVINCE = f"""
SELECT o.province AS province,
       ROUND(SUM(oi.quantity * oi.unit_price), 2) AS gmv
FROM orders o
{_BASE_JOIN}
WHERE {_BASE_FILTER}
  AND o.paid_at >= DATE '2026-06-01'
  AND o.paid_at <  DATE '2026-07-01'
GROUP BY 1
ORDER BY gmv DESC
"""

# 第一次故意漏掉 LEFT JOIN refunds —— 守卫应该拦下来
_SQL_REFUND_RATE_BROKEN = f"""
SELECT p.category AS category,
       COUNT(DISTINCT CASE WHEN r.refund_id IS NOT NULL THEN o.order_id END)
         / NULLIF(COUNT(DISTINCT o.order_id), 0) AS refund_rate
FROM orders o
{_BASE_JOIN}
WHERE {_BASE_FILTER}
GROUP BY 1
ORDER BY 2 DESC
"""

_SQL_REFUND_RATE_FIXED = f"""
SELECT p.category AS category,
       COUNT(DISTINCT CASE WHEN r.refund_id IS NOT NULL THEN o.order_id END)
         / NULLIF(COUNT(DISTINCT o.order_id), 0) AS refund_rate
FROM orders o
{_BASE_JOIN}
LEFT JOIN refunds r ON r.order_id = o.order_id
WHERE {_BASE_FILTER}
GROUP BY 1
ORDER BY 2 DESC
"""

_DEMO_SCRIPTS: dict[str, list[str]] = {
    "2026年6月各省的销售额": [_SQL_GMV_BY_PROVINCE],
    "各品类的退款率从高到低": [_SQL_REFUND_RATE_BROKEN, _SQL_REFUND_RATE_FIXED],
}

_DEMO_QUESTIONS = [
    "2026年6月各省的销售额",
    "各品类的退款率从高到低",
    "最近生意怎么样",
]


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------


def _show(question: str, state: dict[str, Any]) -> None:
    print(f"\n{'=' * 62}")
    print(f"问题：{question}")

    for item in state.get("trace", []):
        print(f"  [{item['node']:<10}] {item['message']}")

    print(f"\n结论：{state.get('answer', '')}")

    sql = state.get("sql")
    if sql and state.get("status") == "ok":
        print("\nSQL：")
        for line in sql.splitlines():
            print("  " + line)

    rows = state.get("rows") or []
    columns = state.get("columns") or []
    if rows and columns:
        _print_table(columns, rows)


def _print_table(columns: list[str], rows: list[dict[str, Any]], limit: int = 5) -> None:
    shown = rows[:limit]
    cells = [[_cell(row.get(column)) for column in columns] for row in shown]

    widths = [
        max(_width(column), *(_width(line[i]) for line in cells))
        for i, column in enumerate(columns)
    ]

    print(f"\n前 {len(shown)} 行（共 {len(rows)} 行）：")
    print("  " + "  ".join(_pad(column, width) for column, width in zip(columns, widths)))
    print("  " + "  ".join("-" * width for width in widths))
    for line in cells:
        print("  " + "  ".join(_pad(value, width) for value, width in zip(line, widths)))


def _width(text: str) -> int:
    """中日韩字符占两个字符宽，直接 len() 会对不齐。"""
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in str(text))


def _pad(text: str, width: int) -> str:
    return str(text) + " " * max(0, width - _width(text))


def _cell(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return str(value)
    if isinstance(value, float):
        text = f"{value:,.4f}".rstrip("0").rstrip(".")
        return text or "0"
    return str(value)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    try:
        for stream in (sys.stdout, sys.stderr):
            stream.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(
        prog="python -m app.graph",
        description="电商经营分析 Agent（命令行版）",
    )
    parser.add_argument("question", nargs="*", help="要问的问题")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="DuckDB 文件路径")
    parser.add_argument("--demo", action="store_true", help="用预置脚本离线跑整条链路")
    parser.add_argument("--max-repair", type=int, default=DEFAULT_MAX_REPAIR)
    args = parser.parse_args(argv)

    registry = load_registry(SEMANTIC_DIR)

    with Executor(args.db) as executor:
        if args.demo:
            generator = ScriptedGenerator(_DEMO_SCRIPTS)
            questions = args.question or _DEMO_QUESTIONS
        else:
            if not args.question:
                parser.error("请给一个问题，或者加 --demo")
            try:
                generator = OpenAICompatGenerator.from_env()
            except RuntimeError as exc:
                print(exc, file=sys.stderr)
                return 2
            questions = args.question

        for question in questions:
            try:
                state = run(
                    question,
                    registry=registry,
                    executor=executor,
                    generator=generator,
                    max_repair=args.max_repair,
                )
            except KeyError as exc:
                print(f"\n问题：{question}\n  [skip] {exc}")
                continue
            _show(question, state)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
