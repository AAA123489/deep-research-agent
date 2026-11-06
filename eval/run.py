"""20 题评测。

两种模式：

    python eval/run.py              # 参考 SQL 模式：不经过模型
    python eval/run.py --model      # 真实模型模式：需要 LLM_API_KEY

参考 SQL 模式不测模型 —— 它把每题的标准 SQL 直接喂给守卫和执行器，
用来验证「守卫不会误拦合法查询」「执行结果和 golden 一致」「结论取对了列」。
模型的准确率必须用 --model 跑。

判分只看结果数字，不看 SQL 长什么样。所以模型用哪种写法都行，
只要算出来的东西对。

    python eval/run.py --rebuild    # 改了 reference_sql 后重算 golden
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.graph.executor import Executor  # noqa: E402
from app.graph.llm import OpenAICompatGenerator  # noqa: E402
from app.graph.pipeline import run  # noqa: E402
from app.semantic import load_registry  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parent
QUESTIONS_PATH = EVAL_DIR / "questions.yaml"
GOLDEN_PATH = EVAL_DIR / "golden.json"
DEFAULT_DB = ROOT / "data" / "ecommerce.duckdb"
SEMANTIC_DIR = ROOT / "backend" / "app" / "semantic"


# ---------------------------------------------------------------------------
# 生成器
# ---------------------------------------------------------------------------


class ReferenceGenerator:
    """直接返回题目的标准 SQL，用来验证链路本身。"""

    def __init__(self, sql_by_question: dict[str, str]) -> None:
        self._sql = sql_by_question

    def generate(self, question, spec, registry, *, bounds, previous_sql=None, error=None) -> str:
        return self._sql[question]


# ---------------------------------------------------------------------------
# golden
# ---------------------------------------------------------------------------


def rebuild_golden(db_path: Path) -> int:
    payload = _load_questions()
    golden: dict[str, Any] = {}

    with Executor(db_path) as executor:
        for item in payload["questions"]:
            if item.get("expect") == "clarify":
                continue
            result = executor.run(item["reference_sql"])
            golden[item["id"]] = {"columns": result.columns, "rows": result.rows}

    GOLDEN_PATH.write_text(
        json.dumps(golden, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"已写入 {GOLDEN_PATH}（{len(golden)} 题）")
    return 0


def _load_questions() -> dict[str, Any]:
    return yaml.safe_load(QUESTIONS_PATH.read_text(encoding="utf-8"))


def _load_golden() -> dict[str, Any]:
    if not GOLDEN_PATH.is_file():
        raise SystemExit(
            f"没有找到 {GOLDEN_PATH}。先跑一次：python eval/run.py --rebuild"
        )
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 判分
# ---------------------------------------------------------------------------


def judge(item: dict[str, Any], golden: dict[str, Any], state: dict[str, Any]) -> tuple[bool, str]:
    expect = item.get("expect", "scalar")

    if expect == "clarify":
        if state.get("status") == "clarify":
            return True, "正确追问"
        return False, f"应该追问，实际状态 {state.get('status')}"

    if state.get("status") != "ok":
        return False, (state.get("answer") or "").splitlines()[0][:40]

    actual_rows = state.get("rows") or []
    expected_entry = golden.get(item["id"])
    if expected_entry is None:
        return False, "golden 里没有这道题"

    expected_rows = expected_entry["rows"]

    if len(actual_rows) != len(expected_rows):
        return False, f"行数 {len(actual_rows)}，应为 {len(expected_rows)}"

    if not _rows_equal(actual_rows, expected_rows):
        return False, "数值不一致"

    return True, f"{len(actual_rows)} 行"


def _rows_equal(actual: list[dict[str, Any]], expected: list[dict[str, Any]]) -> bool:
    if not actual and not expected:
        return True
    if not actual or not expected:
        return False

    actual_keys = set(actual[0])
    expected_keys = set(expected[0])
    if actual_keys != expected_keys:
        return False

    order = sorted(expected_keys)
    left = sorted(actual, key=lambda row: tuple(_sort_key(row.get(k)) for k in order))
    right = sorted(expected, key=lambda row: tuple(_sort_key(row.get(k)) for k in order))

    for row_a, row_e in zip(left, right):
        for key in order:
            if not _close(row_a.get(key), row_e.get(key)):
                return False
    return True


def _sort_key(value: Any) -> str:
    return f"{value:020.6f}" if isinstance(value, (int, float)) and not isinstance(value, bool) else str(value)


def _close(left: Any, right: Any) -> bool:
    numeric = (int, float)
    if (
        isinstance(left, numeric)
        and isinstance(right, numeric)
        and not isinstance(left, bool)
        and not isinstance(right, bool)
    ):
        return abs(left - right) <= max(1e-6, abs(right) * 1e-6)
    return left == right


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in str(text))


def _pad(text: str, width: int) -> str:
    return str(text) + " " * max(0, width - _width(text))


def _print_report(rows: list[tuple[str, str, str, str, bool, str]]) -> None:
    headers = ("题号", "类型", "问题", "结果", "说明")
    widths = [
        max(_width(headers[0]), *(_width(r[0]) for r in rows)),
        max(_width(headers[1]), *(_width(r[1]) for r in rows)),
        max(_width(headers[2]), *(_width(r[2]) for r in rows)),
        _width(headers[3]),
        max(_width(headers[4]), *(_width(r[5]) for r in rows)),
    ]

    print("  ".join(_pad(h, w) for h, w in zip(headers, widths)))
    print("  ".join("-" * w for w in widths))
    for question_id, kind, question, result, _ok, detail in rows:
        print(
            "  ".join(
                _pad(v, w)
                for v, w in zip((question_id, kind, question, result, detail), widths)
            )
        )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    try:
        for stream in (sys.stdout, sys.stderr):
            stream.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(prog="python eval/run.py")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--rebuild", action="store_true", help="重算 golden.json")
    parser.add_argument("--model", action="store_true", help="用真实模型而不是参考 SQL")
    parser.add_argument("--only", nargs="*", help="只跑指定题号")
    parser.add_argument("--max-repair", type=int, default=2)
    args = parser.parse_args(argv)

    if args.rebuild:
        return rebuild_golden(args.db)

    payload = _load_questions()
    golden = _load_golden()
    questions = payload["questions"]
    if args.only:
        wanted = set(args.only)
        questions = [q for q in questions if q["id"] in wanted]

    registry = load_registry(SEMANTIC_DIR)

    if args.model:
        try:
            generator = OpenAICompatGenerator.from_env()
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 2
        mode = f"真实模型（{generator.model}）"
    else:
        generator = ReferenceGenerator(
            {q["question"]: q["reference_sql"] for q in questions if q.get("reference_sql")}
        )
        mode = "参考 SQL（不经过模型）"

    print(f"模式：{mode}")
    print(f"题目：{len(questions)} 道\n")

    report: list[tuple[str, str, str, str, bool, str]] = []
    passed = 0

    with Executor(args.db) as executor:
        for item in questions:
            try:
                state = run(
                    item["question"],
                    registry=registry,
                    executor=executor,
                    generator=generator,
                    max_repair=args.max_repair,
                )
                ok, detail = judge(item, golden, state)
            except Exception as exc:  # 单题炸了不影响其他题
                ok, detail = False, f"{type(exc).__name__}: {exc}"[:40]

            passed += 1 if ok else 0
            report.append(
                (
                    item["id"],
                    item.get("type", ""),
                    item["question"],
                    "✅" if ok else "❌",
                    ok,
                    detail,
                )
            )

    _print_report(report)
    print(f"\n通过 {passed}/{len(questions)}")

    if not args.model:
        print("\n注意：这是参考 SQL 模式，测的是守卫 / 执行 / 结论链路，不是模型准确率。")
        print("模型准确率请设置 LLM_API_KEY 后跑：python eval/run.py --model")

    return 0 if passed == len(questions) else 1


if __name__ == "__main__":
    raise SystemExit(main())
