"""指标语义层加载器。

把 ``metrics.yaml`` / ``dimensions.yaml`` 读成不可变的 Python 对象，
并且在**服务启动时**就把配置错误全部炸出来 —— 宁可起不来，
也不要放一条口径错误的 SQL 出去。

用法::

    from app.semantic.loader import load_registry

    reg = load_registry("app/semantic")
    reg.get("gmv")            # 按 key 精确取
    reg.resolve("销售额")      # 按 name / synonym 匹配，给意图识别用
    reg.visible()             # 用户可以直接问的指标

也可以直接跑，当作配置检查工具::

    python backend/app/semantic/loader.py
"""

from __future__ import annotations

import difflib
import sys
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

import sqlglot
import yaml

__all__ = [
    "DIALECT",
    "DimensionSpec",
    "MetricRegistry",
    "MetricSpec",
    "SemanticConfigError",
    "load_registry",
]

# DuckDB 是当前语义层的默认 SQL 方言；所有表达式都按它来解析和规范化。
DIALECT = "duckdb"

# 业务上指标分为三类：
# - additive: 可直接聚合的原子指标
# - derived: 由其他指标组合而成
# - ratio: 分子/分母型比率指标
KINDS: tuple[str, ...] = ("additive", "derived", "ratio")

# 退款口径允许的枚举值：
# - none: 退款不计入指标
# - keep_original: 保持原始退款口径不变
REFUND_TREATMENTS: tuple[str, ...] = ("none", "keep_original")
DIMENSION_TYPES: tuple[str, ...] = ("time", "category")
GRANULARITIES: tuple[str, ...] = ("day", "week", "month", "quarter")
VERIFICATIONS: tuple[str, ...] = ("strict", "component")

#: 组件口径不一致、但指标显式声明了 mixed_treatment_ok 时的取值。
MIXED_TREATMENT = "mixed"


class SemanticConfigError(Exception):
    """语义层配置有误。消息里带文件名、指标名和具体原因。"""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DimensionSpec:
    """一个可分组维度。"""

    key: str
    type: str  # time | category
    expr: str
    granularities: tuple[str, ...] = ()
    #: 分类维度必须写，形如 ``orders o``；用来查这一列的真实取值，
    #: 否则模型会把 'wechat' 写成「微信支付」，查询直接返回 0 行。
    source: str | None = None

    @property
    def is_time(self) -> bool:
        return self.type == "time"


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """一个指标的口径契约。

    ``refund_treatment`` 是**解析后**的值：derived / ratio 没显式写的话，
    由组件推导出来，不会是空字符串。校验工具拿到这个对象就能直接比对。
    """

    key: str
    name: str
    synonyms: tuple[str, ...]
    kind: str
    expression: str
    normalized_expression: str
    source: str | None
    required_filters: tuple[str, ...]
    required_joins: tuple[str, ...]
    time_field: str
    refund_treatment: str
    dimensions: tuple[str, ...]
    default_window: str
    unit: str
    description: str
    verification: str
    visible: bool
    numerator: str | None
    denominator: str | None
    components: tuple[str, ...]
    mixed_treatment_ok: bool

    @property
    def is_derived(self) -> bool:
        return self.kind in ("derived", "ratio")

    @property
    def dependencies(self) -> tuple[str, ...]:
        """本指标依赖的其他指标，按声明顺序去重。"""
        refs = list(self.components)
        for ref in (self.numerator, self.denominator):
            if ref and ref not in refs:
                refs.append(ref)
        return tuple(refs)


@dataclass(frozen=True, slots=True)
class MetricRegistry:
    """加载完成后的只读指标注册表。"""

    metrics: Mapping[str, MetricSpec]
    dimensions: Mapping[str, DimensionSpec]

    def __contains__(self, key: object) -> bool:
        return key in self.metrics

    def __len__(self) -> int:
        return len(self.metrics)

    def get(self, key: str) -> MetricSpec:
        try:
            return self.metrics[key]
        except KeyError:
            close = difflib.get_close_matches(key, list(self.metrics), n=3)
            hint = f"，是不是想找 {close}？" if close else ""
            raise KeyError(f"没有名为 `{key}` 的指标{hint}") from None

    def visible(self) -> tuple[MetricSpec, ...]:
        """用户可以直接问的指标（``visible: true``）。"""
        return tuple(m for m in self.metrics.values() if m.visible)

    def resolve(self, text: str, *, visible_only: bool = True) -> MetricSpec | None:
        """把用户说的词映射到指标。

        依次尝试：key → name → synonym 精确匹配，最后用最长别名做子串兜底
        （「近30天销售额」能命中 gmv，但不会让短别名抢走长别名）。
        """
        needle = _norm(text)
        if not needle:
            return None

        pool = [m for m in self.metrics.values() if m.visible] if visible_only else list(self.metrics.values())

        for metric in pool:
            if _norm(metric.key) == needle:
                return metric
        for metric in pool:
            if _norm(metric.name) == needle:
                return metric
        for metric in pool:
            if any(_norm(alias) == needle for alias in metric.synonyms):
                return metric

        best: MetricSpec | None = None
        best_len = 0
        for metric in pool:
            for alias in (metric.name, *metric.synonyms):
                candidate = _norm(alias)
                if candidate and candidate in needle and len(candidate) > best_len:
                    best, best_len = metric, len(candidate)
        return best


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def load_registry(directory: str | Path) -> MetricRegistry:
    """读取并校验一个语义层目录，返回只读注册表。

    任何配置错误都抛 :class:`SemanticConfigError`，不做半成品返回。
    """
    return _Loader(Path(directory)).load()


# ---------------------------------------------------------------------------
# 实现
# ---------------------------------------------------------------------------


class _Loader:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def load(self) -> MetricRegistry:
        # 语义层在服务启动前就做全量校验：
        # 1) 维度定义是否正确
        # 2) 指标依赖是否存在且无环
        # 3) 退款口径是否一致
        # 4) SQL 表达式是否能在 DuckDB 上解析
        if not self.directory.is_dir():
            raise SemanticConfigError(f"语义层目录不存在：{self.directory}")
        dimensions = self._load_dimensions()
        metrics = self._load_metrics(dimensions)
        return MetricRegistry(
            metrics=MappingProxyType(metrics),
            dimensions=MappingProxyType(dimensions),
        )

    # -- 读文件 ------------------------------------------------------------

    def _read_yaml(self, filename: str) -> Mapping[str, Any]:
        path = self.directory / filename
        if not path.is_file():
            raise SemanticConfigError(f"找不到配置文件：{path}")
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise SemanticConfigError(f"{filename} 不是合法的 YAML：{exc}") from exc
        except OSError as exc:
            raise SemanticConfigError(f"读不了 {path}：{exc}") from exc

        if raw is None:
            raise SemanticConfigError(f"{filename} 是空的")
        if not isinstance(raw, Mapping):
            raise SemanticConfigError(
                f"{filename} 顶层必须是键值映射，实际是 {type(raw).__name__}"
            )
        return raw

    @staticmethod
    def _unwrap(raw: Mapping[str, Any], wrapper: str) -> Mapping[str, Any]:
        """允许两种写法：裸映射，或者外面套一层 ``metrics:`` / ``dimensions:``。"""
        inner = raw.get(wrapper)
        if inner is not None and isinstance(inner, Mapping):
            return inner
        return raw

    # -- 维度 --------------------------------------------------------------
    # 维度定义负责统一分组字段和时间粒度校验；这里的要求比普通 SQL 更严格，
    # 目的是把口径不一致的字段提前拦住，避免查询时才发现 "订单日期" 和 "支付日期" 混用。

    def _load_dimensions(self) -> dict[str, DimensionSpec]:
        raw = self._unwrap(self._read_yaml("dimensions.yaml"), "dimensions")
        out: dict[str, DimensionSpec] = {}

        for key, body in raw.items():
            key = str(key)
            where = f"dimensions.yaml → {key}"
            if not isinstance(body, Mapping):
                raise SemanticConfigError(
                    f"{where}：必须是一个映射，实际是 {type(body).__name__}"
                )

            dim_type = body.get("type")
            if dim_type not in DIMENSION_TYPES:
                raise SemanticConfigError(
                    f"{where}：`type` 必须是 {' / '.join(DIMENSION_TYPES)}，"
                    f"实际是 {dim_type!r}"
                )

            expr = body.get("expr")
            if not isinstance(expr, str) or not expr.strip():
                raise SemanticConfigError(f"{where}：`expr` 必须是非空字符串")

            granularities = body.get("granularities") or []
            if isinstance(granularities, str):
                granularities = [granularities]
            if not isinstance(granularities, list):
                raise SemanticConfigError(f"{where}：`granularities` 必须是列表")
            if dim_type == "time" and not granularities:
                raise SemanticConfigError(
                    f"{where}：时间维度必须声明 granularities（{' / '.join(GRANULARITIES)}）"
                )
            if dim_type != "time" and granularities:
                raise SemanticConfigError(
                    f"{where}：只有 time 维度支持 granularities"
                )

            bad = [g for g in granularities if g not in GRANULARITIES]
            if bad:
                raise SemanticConfigError(
                    f"{where}：不支持的粒度 {bad}，可选 {' / '.join(GRANULARITIES)}"
                )

            source = body.get("source")
            if source is not None:
                if not isinstance(source, str) or not source.strip():
                    raise SemanticConfigError(
                        f"{where}：`source` 必须是非空字符串，比如 `orders o`"
                    )
                source = " ".join(source.split())
            if dim_type == "category" and not source:
                raise SemanticConfigError(
                    f"{where}：分类维度必须声明 `source`（比如 `orders o`），"
                    f"否则没法查这一列的真实取值"
                )

            out[key] = DimensionSpec(
                key=key,
                type=str(dim_type),
                expr=expr.strip(),
                granularities=tuple(str(g) for g in granularities),
                source=source,
            )

        if not out:
            raise SemanticConfigError("dimensions.yaml 里一个维度都没有")

        return out

    # -- 指标 --------------------------------------------------------------

    def _load_metrics(self, dimensions: Mapping[str, DimensionSpec]) -> dict[str, MetricSpec]:
        raw = self._unwrap(self._read_yaml("metrics.yaml"), "metrics")

        specs: dict[str, MetricSpec] = {}
        for key, body in raw.items():
            key = str(key)
            specs[key] = self._parse_metric(key, body, dimensions)

        if not specs:
            raise SemanticConfigError("metrics.yaml 里一个指标都没有")

        # 拓扑序保证依赖排在自己前面，所以 resolved 里一定有依赖的最终口径
        resolved: dict[str, MetricSpec] = {}
        for key in self._toposort(specs):
            resolved[key] = self._resolve_treatment(specs[key], resolved)
        return resolved

    def _parse_metric(
        self,
        key: str,
        body: Any,
        dimensions: Mapping[str, DimensionSpec],
    ) -> MetricSpec:
        where = f"metrics.yaml → {key}"
        if not isinstance(body, Mapping):
            raise SemanticConfigError(
                f"{where}：必须是一个映射，实际是 {type(body).__name__}"
            )

        kind = _text(body, "kind", where)
        if kind not in KINDS:
            raise SemanticConfigError(
                f"{where}：`kind` 必须是 {' / '.join(KINDS)}，实际是 {kind!r}"
            )

        verification = _text(body, "verification", where)
        if verification not in VERIFICATIONS:
            raise SemanticConfigError(
                f"{where}：`verification` 必须是 {' / '.join(VERIFICATIONS)}，"
                f"实际是 {verification!r}"
            )

        used_dimensions = _texts(body, "dimensions", where)
        unknown = [d for d in used_dimensions if d not in dimensions]
        if unknown:
            raise SemanticConfigError(
                f"{where}：维度 {unknown} 不在 dimensions.yaml 里，"
                f"可用：{', '.join(sorted(dimensions))}"
            )

        expression = _text(body, "expression", where)
        _expr_node, normalized = _parse_expression(where, expression)

        components = _texts(body, "components", where, required=False)
        numerator = _optional_text(body, "numerator", where)
        denominator = _optional_text(body, "denominator", where)

        source: str | None = None
        required_filters: tuple[str, ...] = ()
        required_joins: tuple[str, ...] = ()
        treatment: str

        if kind == "additive":
            if components or numerator or denominator:
                raise SemanticConfigError(
                    f"{where}：additive 指标要自己写 expression，"
                    f"不要声明 components / numerator / denominator"
                )
            if body.get("mixed_treatment_ok"):
                raise SemanticConfigError(
                    f"{where}：`mixed_treatment_ok` 只对 derived / ratio 有意义"
                )

            source = _text(body, "source", where)
            required_filters = _texts(body, "required_filters", where)
            required_joins = _texts(body, "required_joins", where, required=False)

            treatment = _text(body, "refund_treatment", where)
            if treatment not in REFUND_TREATMENTS:
                raise SemanticConfigError(
                    f"{where}：`refund_treatment` 必须是 "
                    f"{' / '.join(REFUND_TREATMENTS)}，实际是 {treatment!r}"
                )

            _parse_query(where, expression, source=source, filters=required_filters)

        else:  # derived / ratio
            # 派生指标并不直接从事实表取数，而是通过组件拼装。
            # 因此 source / required_filters / required_joins 这些底层查询信息，
            # 必须由它们依赖的原子指标提供，不能在这里重复声明。
            for forbidden in ("source", "required_filters", "required_joins"):
                if forbidden in body:
                    raise SemanticConfigError(
                        f"{where}：`{kind}` 指标不要写 `{forbidden}`，"
                        f"口径由组件继承"
                    )

            explicit = _optional_text(body, "refund_treatment", where)
            if explicit is not None and explicit not in REFUND_TREATMENTS:
                raise SemanticConfigError(
                    f"{where}：`refund_treatment` 必须是 "
                    f"{' / '.join(REFUND_TREATMENTS)}，实际是 {explicit!r}"
                )
            treatment = explicit or ""

            if kind == "ratio":
                if components:
                    raise SemanticConfigError(
                        f"{where}：ratio 指标用 numerator / denominator，不要写 components"
                    )
                if not numerator or not denominator:
                    raise SemanticConfigError(
                        f"{where}：ratio 指标必须同时声明 `numerator` 和 `denominator`"
                    )
            else:  # derived
                if components and (numerator or denominator):
                    raise SemanticConfigError(
                        f"{where}：derived 指标要么用 components，"
                        f"要么用 numerator + denominator，不要混用"
                    )
                if not components and not (numerator and denominator):
                    raise SemanticConfigError(
                        f"{where}：derived 指标必须声明 `components`，"
                        f"或者同时声明 `numerator` 和 `denominator`"
                    )
                if bool(numerator) != bool(denominator):
                    raise SemanticConfigError(
                        f"{where}：`numerator` 和 `denominator` 必须成对出现"
                    )

        return MetricSpec(
            key=key,
            name=_text(body, "name", where),
            synonyms=_texts(body, "synonyms", where, required=False),
            kind=kind,
            expression=expression,
            normalized_expression=normalized,
            source=source,
            required_filters=required_filters,
            required_joins=required_joins,
            time_field=_text(body, "time_field", where),
            refund_treatment=treatment,
            dimensions=used_dimensions,
            default_window=_text(body, "default_window", where),
            unit=_text(body, "unit", where),
            description=_text(body, "description", where),
            verification=verification,
            visible=_flag(body, "visible", where),
            numerator=numerator,
            denominator=denominator,
            components=components,
            mixed_treatment_ok=_flag(body, "mixed_treatment_ok", where, default=False),
        )

    # -- 依赖与口径继承 ----------------------------------------------------

    @staticmethod
    def _toposort(specs: Mapping[str, MetricSpec]) -> list[str]:
        """按依赖顺序排好，顺便把断链和成环都查出来。"""
        order: list[str] = []
        state: dict[str, int] = {}  # 0 = 正在访问, 1 = 已完成
        stack: list[str] = []

        def visit(key: str) -> None:
            mark = state.get(key)
            if mark == 1:
                return
            if mark == 0:
                cycle = " → ".join(stack[stack.index(key) :] + [key])
                raise SemanticConfigError(f"指标依赖成环：{cycle}")

            state[key] = 0
            stack.append(key)

            for dep in specs[key].dependencies:
                if dep == key:
                    raise SemanticConfigError(f"metrics.yaml → {key}：指标不能引用自己")
                if dep not in specs:
                    raise SemanticConfigError(
                        f"metrics.yaml → {key}：引用了不存在的指标 `{dep}`，"
                        f"已有：{', '.join(sorted(specs))}"
                    )
                visit(dep)

            stack.pop()
            state[key] = 1
            order.append(key)

        # 通过拓扑排序保证：先解析依赖，再处理当前指标。
        # 这样所有依赖的 refund_treatment 都已经确定，后续创建的 MetricSpec
        # 可以直接使用 resolved 中的最终口径做一致性检查。
        for key in specs:
            visit(key)
        return order

    @staticmethod
    def _resolve_treatment(
        spec: MetricSpec,
        resolved: Mapping[str, MetricSpec],
    ) -> MetricSpec:
        # 派生指标的退款口径可以从依赖项中继承，但前提是它们的口径一致。
        # 只有当未显式声明 refund_treatment 且依赖项仍是派生指标时，才需要做补推导。
        if not spec.is_derived or spec.refund_treatment:
            return spec

        # resolved 里存的都是已完成解析的口径
        treatments = {resolved[dep].refund_treatment for dep in spec.dependencies}

        if len(treatments) == 1:
            return replace(spec, refund_treatment=treatments.pop())

        if spec.mixed_treatment_ok:
            return replace(spec, refund_treatment=MIXED_TREATMENT)

        detail = "，".join(f"{d}={resolved[d].refund_treatment}" for d in spec.dependencies)
        raise SemanticConfigError(
            f"metrics.yaml → {spec.key}：组件的退款口径不一致（{detail}），"
            f"但没有声明 `mixed_treatment_ok: true`"
        )


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    return "".join(str(text).split()).lower()


def _text(body: Mapping[str, Any], field: str, where: str) -> str:
    value = body.get(field)
    if value is None:
        raise SemanticConfigError(f"{where}：缺少必填字段 `{field}`")
    if not isinstance(value, str) or not value.strip():
        raise SemanticConfigError(f"{where}：`{field}` 必须是非空字符串")
    return value.strip()


def _optional_text(body: Mapping[str, Any], field: str, where: str) -> str | None:
    value = body.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SemanticConfigError(f"{where}：`{field}` 必须是非空字符串")
    return value.strip()


def _texts(
    body: Mapping[str, Any],
    field: str,
    where: str,
    *,
    required: bool = True,
) -> tuple[str, ...]:
    value = body.get(field)
    if value is None:
        if required:
            raise SemanticConfigError(f"{where}：缺少必填字段 `{field}`")
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise SemanticConfigError(f"{where}：`{field}` 必须是列表")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise SemanticConfigError(f"{where}：`{field}` 的每一项都必须是非空字符串")
    return tuple(item.strip() for item in value)


def _flag(
    body: Mapping[str, Any],
    field: str,
    where: str,
    *,
    default: bool | None = None,
) -> bool:
    if field not in body:
        if default is None:
            raise SemanticConfigError(f"{where}：缺少必填字段 `{field}`")
        return default
    value = body[field]
    if not isinstance(value, bool):
        raise SemanticConfigError(f"{where}：`{field}` 必须是 true / false")
    return value


def _parse_expression(where: str, expression: str) -> tuple[Any, str]:
    try:
        node = sqlglot.parse_one(expression, dialect=DIALECT)
    except Exception as exc:  # sqlglot 不同版本抛的异常类型不统一
        raise SemanticConfigError(
            f"{where}：`expression` 解析失败（dialect={DIALECT}）\n"
            f"  原文：{expression}\n"
            f"  原因：{exc}"
        ) from exc
    if node is None:
        raise SemanticConfigError(f"{where}：`expression` 解析结果为空")
    return node, node.sql(dialect=DIALECT)


def _parse_query(
    where: str,
    expression: str,
    *,
    source: str,
    filters: tuple[str, ...],
) -> None:
    """把 expression + source + filters 拼成完整查询再解析。

    单看 expression 是合法的，拼起来未必 —— 比如 source 少了 JOIN。
    这一步的目的是让配置在「看似合法」的情况下也能尽早发现拼接错误。
    """
    sql = f"SELECT {expression} FROM {source}"
    if filters:
        sql += " WHERE " + " AND ".join(filters)
    try:
        node = sqlglot.parse_one(sql, dialect=DIALECT)
    except Exception as exc:
        raise SemanticConfigError(
            f"{where}：expression / source / required_filters 拼不成合法查询"
            f"（dialect={DIALECT}）\n"
            f"  SQL：{sql}\n"
            f"  原因：{exc}"
        ) from exc
    if node is None:
        raise SemanticConfigError(f"{where}：拼出的查询解析结果为空")

    _check_table_aliases(where, node, source)


def _check_table_aliases(where: str, node: Any, source: str) -> None:
    """expression / filters 用到的表别名，必须都在 source 里声明过。

    sqlglot 只做语法解析，``p.cost`` 里 ``p`` 没定义它也照过不误 ——
    这一层补上「忘了写 JOIN」这类错误。
    """
    try:
        from_source = sqlglot.parse_one(f"SELECT 1 FROM {source}", dialect=DIALECT)
    except Exception:
        return  # source 本身有问题，上面的解析已经报过了

    declared = {table.alias_or_name for table in from_source.find_all(sqlglot.exp.Table)}

    select = node if isinstance(node, sqlglot.exp.Select) else node.find(sqlglot.exp.Select)
    if select is None:
        return

    scopes = list(select.expressions)
    where_clause = select.args.get("where")
    if where_clause is not None:
        scopes.append(where_clause)

    used = {
        column.table
        for scope in scopes
        for column in scope.find_all(sqlglot.exp.Column)
        if column.table
    }

    unknown = sorted(used - declared)
    if unknown:
        raise SemanticConfigError(
            f"{where}：expression / required_filters 用了表别名 {unknown}，"
            f"但 source 里只声明了 {sorted(declared)} —— 是不是漏了 JOIN？"
        )


# ---------------------------------------------------------------------------
# 命令行：当配置检查工具用
# ---------------------------------------------------------------------------


def _main(argv: list[str]) -> int:
    try:
        for stream in (sys.stdout, sys.stderr):
            stream.reconfigure(encoding="utf-8")  # Windows 控制台默认 GBK
    except (AttributeError, OSError):
        pass

    directory = Path(argv[1]) if len(argv) > 1 else Path(__file__).parent

    try:
        registry = load_registry(directory)
    except SemanticConfigError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1

    print(f"[OK] {directory}")
    print(f"     指标 {len(registry.metrics)} 个（可问 {len(registry.visible())} 个），"
          f"维度 {len(registry.dimensions)} 个\n")

    header = f"{'指标':<16}{'kind':<10}{'可问':<6}{'退款口径':<16}{'单位':<6}"
    print(header)
    print("-" * 60)
    for metric in registry.metrics.values():
        print(
            f"{metric.key:<16}{metric.kind:<10}"
            f"{'是' if metric.visible else '否':<6}"
            f"{metric.refund_treatment:<16}{metric.unit:<6}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
