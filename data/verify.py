#!/usr/bin/env python3
"""数据验收工具 —— 只定义「什么叫合格」，不关心 generate.py 怎么实现。

用法：
    python data/verify.py data/ecommerce.duckdb
    python data/verify.py --repro          # 连跑两次生成器，检查可复现性

退出码：0 全过；1 有失败项。

这个文件由我（Claude）维护，你不需要改。你改的是 generate.py。
"""
from __future__ import annotations

import argparse
import hashlib
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

# Windows 终端默认 GBK，中文输出会乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")

try:
    import duckdb
except ImportError:
    sys.exit("缺少 duckdb：pip install duckdb")

DATA_VERSION = "ec_v1"
DATE_START, DATE_END = "2025-01-01", "2026-09-30"

# 表 -> (必需列, 期望行数)
SCHEMA: dict[str, tuple[list[str], int]] = {
    "customers": (
        ["customer_id", "name", "gender", "age", "city", "province",
         "register_date", "channel"], 20_000),
    "products": (
        ["product_id", "name", "category", "sub_category", "price", "cost"], 2_000),
    "orders": (
        ["order_id", "customer_id", "order_date", "paid_at", "status",
         "channel", "province", "payment_method", "shipping_fee", "discount_amount"], 100_000),
    "order_items": (
        ["order_id", "product_id", "quantity", "unit_price"], 250_000),
    "refunds": (
        ["refund_id", "order_id", "refund_date", "amount", "reason"], 15_000),
}

ROW_TOLERANCE = 0.10          # 行数允许 ±10%
PEAK_MONTHS = {"2025-06", "2025-11", "2026-06"}
PEAK_FACTOR = 1.3             # 大促月 GMV >= 月中位数 × 1.3
VALID_STATUSES = {"paid", "shipped", "completed"}

# (子表, 外键列, 父表, 父键) —— NULL 不算孤儿
FOREIGN_KEYS = [
    ("orders", "customer_id", "customers", "customer_id"),
    ("order_items", "order_id", "orders", "order_id"),
    ("order_items", "product_id", "products", "product_id"),
    ("refunds", "order_id", "orders", "order_id"),
]


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passes: list[str] = []

    def ok(self, msg: str) -> None:
        self.passes.append(msg)

    def fail(self, msg: str) -> None:
        self.failures.append(msg)

    def dump(self) -> int:
        for m in self.passes:
            print(f"  PASS  {m}")
        for m in self.failures:
            print(f"  FAIL  {m}")
        print(f"\n{len(self.passes)} passed, {len(self.failures)} failed")
        return 0 if not self.failures else 1


def q(con, sql: str):
    return con.execute(sql).fetchall()


def check_schema(con, r: Report) -> None:
    for table, (cols, _) in SCHEMA.items():
        try:
            have = [row[0] for row in q(con, f"DESCRIBE {table}")]
        except Exception:
            r.fail(f"表 {table} 不存在")
            continue
        missing = [c for c in cols if c not in have]
        if missing:
            r.fail(f"{table} 缺列: {missing}")
        else:
            r.ok(f"{table} 结构完整 ({len(cols)} 列)")


def check_meta(con, r: Report) -> None:
    try:
        rows = q(con, "SELECT value FROM dataset_meta WHERE key = 'DATA_VERSION'")
    except Exception:
        r.fail("缺 dataset_meta 表（应含 key/value 两列）")
        return
    if not rows:
        r.fail("dataset_meta 没有 DATA_VERSION 记录")
    elif rows[0][0] != DATA_VERSION:
        r.fail(f"DATA_VERSION 应为 {DATA_VERSION}，实际 {rows[0][0]!r}")
    else:
        r.ok(f"DATA_VERSION = {DATA_VERSION}")


def check_counts(con, r: Report) -> None:
    for table, (_, expect) in SCHEMA.items():
        try:
            n = q(con, f"SELECT COUNT(*) FROM {table}")[0][0]
        except Exception:
            continue  # 缺表已在 check_schema 报过
        lo, hi = expect * (1 - ROW_TOLERANCE), expect * (1 + ROW_TOLERANCE)
        if lo <= n <= hi:
            r.ok(f"{table} 行数 {n:,} (期望 {expect:,} ±10%)")
        else:
            r.fail(f"{table} 行数 {n:,}，超出 [{lo:,.0f}, {hi:,.0f}]")


def check_orders_status(con, r: Report) -> None:
    try:
        statuses = {row[0] for row in q(con, "SELECT DISTINCT status FROM orders")}
    except Exception:
        return
    if "refunded" in statuses:
        r.fail("orders.status 出现 'refunded'：退款不得修改订单状态（design.md §4）")
    else:
        r.ok("orders.status 无 'refunded'")

    missing = VALID_STATUSES - statuses
    if missing:
        r.fail(f"缺少有效订单状态: {sorted(missing)}")
    else:
        r.ok("含 paid / shipped / completed 三种有效状态")

    nulls = q(con, "SELECT COUNT(*) FROM orders WHERE paid_at IS NULL")[0][0]
    if nulls == 0:
        r.fail("没有 paid_at IS NULL 的订单，有效订单过滤就失去了意义")
    else:
        r.ok(f"{nulls:,} 条订单 paid_at 为 NULL（有效订单过滤才有意义）")


def check_foreign_keys(con, r: Report) -> None:
    for child, col, parent, pcol in FOREIGN_KEYS:
        try:
            n = q(con, f"""
                SELECT COUNT(*) FROM {child} x
                LEFT JOIN {parent} y ON x.{col} = y.{pcol}
                WHERE x.{col} IS NOT NULL AND y.{pcol} IS NULL
            """)[0][0]
        except Exception as e:
            r.fail(f"外键检查失败 {child}.{col} -> {parent}.{pcol}: {e}")
            continue
        if n == 0:
            r.ok(f"{child}.{col} -> {parent}.{pcol} 无孤儿")
        else:
            r.fail(f"{child}.{col} 有 {n:,} 条孤儿引用 {parent}.{pcol}")


def check_values(con, r: Report) -> None:
    cases = [
        ("order_items.quantity > 0", "SELECT COUNT(*) FROM order_items WHERE quantity <= 0"),
        ("order_items.unit_price > 0", "SELECT COUNT(*) FROM order_items WHERE unit_price <= 0"),
        ("refunds.amount > 0", "SELECT COUNT(*) FROM refunds WHERE amount <= 0"),
        ("products.price > 0", "SELECT COUNT(*) FROM products WHERE price <= 0"),
        ("products.cost > 0", "SELECT COUNT(*) FROM products WHERE cost <= 0"),
    ]
    for label, sql in cases:
        try:
            n = q(con, sql)[0][0]
        except Exception:
            continue
        if n == 0:
            r.ok(label)
        else:
            r.fail(f"{label} 被违反 {n:,} 行")


def check_date_range(con, r: Report) -> None:
    cases = [
        ("orders.order_date", "SELECT MIN(order_date), MAX(order_date) FROM orders"),
        ("refunds.refund_date", "SELECT MIN(refund_date), MAX(refund_date) FROM refunds"),
    ]
    for label, sql in cases:
        try:
            lo, hi = q(con, sql)[0]
        except Exception:
            continue
        if lo is None:
            r.fail(f"{label} 为空")
            continue
        lo_s, hi_s = str(lo)[:10], str(hi)[:10]
        if DATE_START <= lo_s and hi_s <= DATE_END:
            r.ok(f"{label} 落在 [{DATE_START}, {DATE_END}]")
        else:
            r.fail(f"{label} 越界: [{lo_s}, {hi_s}]")


def check_seasonality(con, r: Report) -> None:
    sql = """
        SELECT strftime(DATE_TRUNC('month', o.paid_at), '%Y-%m') AS m,
               SUM(oi.quantity * oi.unit_price) AS gmv
        FROM orders o
        JOIN order_items oi ON oi.order_id = o.order_id
        WHERE o.status IN ('paid','shipped','completed') AND o.paid_at IS NOT NULL
        GROUP BY 1
        ORDER BY 1
    """
    try:
        rows = q(con, sql)
    except Exception as e:
        r.fail(f"月度 GMV 查询失败: {e}")
        return
    if not rows:
        r.fail("月度 GMV 为空")
        return
    gmv = {m: float(v or 0) for m, v in rows}
    median = statistics.median(gmv.values())
    for m in sorted(PEAK_MONTHS):
        if m not in gmv:
            r.fail(f"缺少月份 {m}")
        elif gmv[m] < median * PEAK_FACTOR:
            r.fail(f"{m} GMV {gmv[m]:,.0f} 未达月中位数 {median:,.0f} × {PEAK_FACTOR}")
        else:
            r.ok(f"{m} 大促峰值 x{gmv[m] / median:.2f}")
    top3 = sorted(gmv.items(), key=lambda kv: kv[1], reverse=True)[:3]
    r.ok(f"GMV 前三月份: {', '.join(m for m, _ in top3)}")


def content_digest(db: Path) -> str:
    """按表内容（排序后）算 sha256，比文件 md5 稳（DuckDB 文件含元数据噪声）。"""
    con = duckdb.connect(str(db), read_only=True)
    h = hashlib.sha256()
    try:
        for table, (cols, _) in SCHEMA.items():
            order = ", ".join(str(i + 1) for i in range(len(cols)))
            for row in con.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall():
                h.update(repr(row).encode())
    finally:
        con.close()
    return h.hexdigest()


def check_repro(r: Report) -> None:
    gen = Path(__file__).parent / "generate.py"
    if not gen.exists():
        r.fail(f"找不到 {gen}")
        return
    with tempfile.TemporaryDirectory() as td:
        digests = []
        for name in ("a", "b"):
            out = Path(td) / f"{name}.duckdb"
            print(f"  运行 generate.py -> {name}.duckdb ...", flush=True)
            subprocess.run([sys.executable, str(gen), "--out", str(out)], check=True)
            digests.append(content_digest(out))
        if digests[0] == digests[1]:
            r.ok(f"两次生成内容一致 ({digests[0][:16]}…)")
        else:
            r.fail(f"两次生成内容不一致: {digests[0][:16]}… vs {digests[1][:16]}…")


def main() -> int:
    ap = argparse.ArgumentParser(description="电商数据集验收")
    ap.add_argument("db", nargs="?", help="ecommerce.duckdb 路径")
    ap.add_argument("--repro", action="store_true", help="检查生成器可复现性")
    args = ap.parse_args()

    r = Report()

    if args.repro:
        print("== 可复现性 ==")
        check_repro(r)
        return r.dump()

    if not args.db:
        ap.error("需要 db 路径，或使用 --repro")
    db = Path(args.db)
    if not db.exists():
        sys.exit(f"数据库不存在: {db}")

    con = duckdb.connect(str(db), read_only=True)
    try:
        print("== 结构 ==");        check_schema(con, r)
        print("== 元信息 ==");      check_meta(con, r)
        print("== 规模 ==");        check_counts(con, r)
        print("== 订单状态 ==");    check_orders_status(con, r)
        print("== 外键 ==");        check_foreign_keys(con, r)
        print("== 取值合理性 ==");  check_values(con, r)
        print("== 时间范围 ==");    check_date_range(con, r)
        print("== 大促峰值 ==");    check_seasonality(con, r)
    finally:
        con.close()

    return r.dump()


if __name__ == "__main__":
    sys.exit(main())
