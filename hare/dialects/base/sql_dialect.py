from __future__ import annotations

from hare.dialects.base.dialect import Dialect
from hare.dialects.base.operators import FilterOperators
from hare.dialects.base.renderers.term_renderers import TermRenderers
from hare.dialects.base.types.type_registry import TypeRegistry
from hare.dialects.enums import DialectName


class SqlDialect(Dialect):
    """Plain SQL, not tied to a database: every field keeps its own column type."""

    name = DialectName.SQL
    otel_system_name = "other_sql"

    def build_types(self) -> TypeRegistry:
        return TypeRegistry()

    def build_filter_operators(self) -> FilterOperators:
        return FilterOperators({})

    def build_renderers(self) -> TermRenderers:
        return TermRenderers()
