"""DuckDB 执行器。

只负责「把一条已经通过守卫的 SQL 跑出结果」，不做任何 SQL 加工。
连库一律 read_only，行数有上限，返回值转成能直接 JSON 序列化的类型。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb


@dataclass(slots=True)
class ExecutionResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    elapsed_ms: float
    truncated: bool = False

    @property
    def row_count(self) -> int:
        return len(self.rows)


class Executor:
    """一个只读的 DuckDB 连接。

    长连接比每次查询都重新打开快，但要注意用完 close。
    """

    def __init__(self, db_path: str | Path, *, row_limit: int = 1000) -> None:
        self.db_path = Path(db_path)
        if not self.db_path.is_file():
            raise FileNotFoundError(
                f"找不到数据库：{self.db_path}\n"
                f"先跑一次 python data/generate.py --out {self.db_path}"
            )

        self.row_limit = row_limit
        self._con = duckdb.connect(str(self.db_path), read_only=True)
        self._bounds: tuple[date, date] | None = None
        self._values: dict[str, list[str]] | None = None

    # -- 数据集边界 --------------------------------------------------------

    def bounds(self) -> tuple[date, date]:
        """数据覆盖的日期范围。

        演示数据是历史快照，「最近 30 天」必须相对数据最后一天算，
        否则按真实今天算会一条都查不到。
        """
        if self._bounds is None:
            row = self._con.execute(
                "SELECT MIN(paid_at)::DATE, MAX(paid_at)::DATE "
                "FROM orders WHERE paid_at IS NOT NULL"
            ).fetchone()
            self._bounds = (row[0], row[1])
        return self._bounds

    # -- 维度的真实取值 ----------------------------------------------------

    def dimension_values(
        self,
        registry,
        *,
        max_distinct: int = 40,
    ) -> dict[str, list[str]]:
        """低基数列的真实取值，拼提示词时用。

        模型不知道库里 `payment_method` 存的是 ``wechat`` 而不是「微信支付」，
        不知道省份是 ``上海市`` 而不是「上海」。不给取值，它只能猜，猜错就是 0 行。
        基数太高的列（比如订单号）直接跳过，塞进去只会把提示词撑爆。
        """
        if self._values is not None:
            return self._values

        values: dict[str, list[str]] = {}
        for name, dimension in registry.dimensions.items():
            if dimension.is_time or not dimension.source:
                continue
            try:
                rows = self._con.execute(
                    f"SELECT DISTINCT {dimension.expr} FROM {dimension.source} "
                    f"WHERE {dimension.expr} IS NOT NULL "
                    f"ORDER BY 1 LIMIT {max_distinct + 1}"
                ).fetchall()
            except duckdb.Error:
                continue
            if len(rows) > max_distinct:
                continue
            values[name] = [str(row[0]) for row in rows]

        self._values = values
        return values

    # -- 执行 --------------------------------------------------------------

    def run(self, sql: str) -> ExecutionResult:
        statement = sql.strip().rstrip(";")

        # 外面再套一层，兜住模型忘了写 LIMIT 的情况
        wrapped = f"SELECT * FROM (\n{statement}\n) AS _q LIMIT {self.row_limit + 1}"

        started = time.perf_counter()
        cursor = self._con.execute(wrapped)
        columns = [description[0] for description in cursor.description]
        raw = cursor.fetchall()
        elapsed_ms = (time.perf_counter() - started) * 1000

        truncated = len(raw) > self.row_limit
        rows = [
            dict(zip(columns, (_jsonable(value) for value in record)))
            for record in raw[: self.row_limit]
        ]
        return ExecutionResult(columns, rows, elapsed_ms, truncated)

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> Executor:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _jsonable(value: Any) -> Any:
    """DuckDB 会返回 Decimal / date / datetime，转成前端和 JSON 认得的类型。"""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == datetime.min.time() else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value
