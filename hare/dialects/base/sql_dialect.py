from __future__ import annotations

from operator import attrgetter

from hare.dialects.base.dialect import Dialect
from hare.dialects.base.lookups.filter_operators import FilterOperators
from hare.dialects.base.renderers.term_renderers import TermRenderers
from hare.dialects.base.types.type_mapping import TypeMapping
from hare.dialects.base.types.type_registry import TypeRegistry
from hare.dialects.enums import DialectName


class SqlDialect(Dialect):
    """Plain SQL, not tied to a database: every field keeps its own column type - ISO SQL's
    ``DECIMAL`` of a DecimalField too."""

    name = DialectName.SQL
    otel_system_name = "other_sql"

    def build_types(self) -> TypeRegistry:
        # Local import: the field modules import this one.
        from hare.fields.data.numeric.decimal_field import DecimalField

        types = TypeRegistry()
        types.register(DecimalField, TypeMapping(column_type=attrgetter("SQL_TYPE")))
        return types

    def build_filter_operators(self) -> FilterOperators:
        return FilterOperators(self, {})

    def build_renderers(self) -> TermRenderers:
        return TermRenderers(self)
