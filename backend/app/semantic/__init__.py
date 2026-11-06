"""指标语义层：把 YAML 口径契约加载成只读对象。"""

from .loader import (
    DIALECT,
    DimensionSpec,
    MetricRegistry,
    MetricSpec,
    SemanticConfigError,
    load_registry,
)

__all__ = [
    "DIALECT",
    "DimensionSpec",
    "MetricRegistry",
    "MetricSpec",
    "SemanticConfigError",
    "load_registry",
]
