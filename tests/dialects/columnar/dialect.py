from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from hare.dialects.base.types import TypeMapping
from hare.dialects.sqlite.dialect import SqliteDialect
from hare.fields.data.uuids import UUIDField
from hare.query.queryset.extensions import QuerySetExtensions
from hare.sql.terms.base import LiteralValue
from hare.sql.terms.functions import Function
from tests.dialects.columnar.table_options import ColumnarTableOptions

if TYPE_CHECKING:
    from hare.dialects.base.renderers import TermRenderers
    from hare.dialects.base.types import TypeRegistry
    from hare.sql.context import SqlContext
    from hare.sql.queries.builder import QueryBuilder


class ColumnarUuidText(Function):
    """A UUID column's 16 bytes as the UUID's text - how a JSON object holds it."""

    #: (start, length) of each dash-separated group in the 32 hex digits.
    HEX_GROUPS = ((1, 8), (9, 4), (13, 4), (17, 4), (21, 12))

    def __init__(self, term: Any) -> None:
        super().__init__("hex", term)

    def get_function_sql(self, ctx: SqlContext) -> str:
        value_sql = self.get_arg_sql(self.args[0], ctx)
        hex_sql = f"hex({value_sql})"
        groups = " || '-' || ".join(f"substr({hex_sql}, {start}, {length})" for start, length in self.HEX_GROUPS)
        # hex(NULL) is an empty string - a NULL stays NULL.
        return f"CASE WHEN {value_sql} IS NULL THEN NULL ELSE lower({groups}) END"


class ColumnarDialect(SqliteDialect):
    """SQLite's engine without transactions, foreign keys or unique constraints, with numbered
    placeholders and backtick-quoted identifiers."""

    name = "columnar"
    otel_system_name = "other_sql"
    placeholder_template = "?{}"
    identifier_quote_char = "`"
    supports_foreign_keys = False
    supports_unique_constraints = False
    #: Percent of rows ``sample()`` keeps.
    max_sample_percent = 100

    def install(self) -> None:
        super().install()
        QuerySetExtensions.register("sample", self.name, self.apply_sample)

    def apply_sample(self, builder: QueryBuilder, percent: int) -> QueryBuilder:
        """``QuerySet.sample(percent)`` - roughly ``percent`` percent of the rows, picked at random.

        Args:
            builder: The query's builder.
            percent: The share of rows kept, 0 to 100.

        Returns:
            The builder, filtered.

        Raises:
            ValueError: ``percent`` isn't an integer from 0 to 100.
        """
        if isinstance(percent, bool) or not isinstance(percent, int) or not 0 <= percent <= self.max_sample_percent:
            raise ValueError(f"sample() takes a percent from 0 to {self.max_sample_percent}, got {percent!r}")
        return builder.where(LiteralValue(f"abs(random()) % 100 < {percent}"))

    def build_table_options_class(self) -> type[ColumnarTableOptions]:
        return ColumnarTableOptions

    def build_types(self) -> TypeRegistry:
        types = super().build_types()
        types.register(
            UUIDField,
            TypeMapping(
                column_type="BLOB",
                to_db=self.get_uuid_bytes,
                to_lookup=self.get_uuid_bytes,
                to_python=self.get_uuid,
                json_term=lambda field, term: ColumnarUuidText(term),
            ),
        )
        return types

    @staticmethod
    def get_uuid_bytes(field: UUIDField[Any], value: Any, instance: Any) -> bytes | None:
        """A UUID as the 16 bytes its BLOB column holds - after the field's own conversion, which
        validates it.

        Args:
            field: The UUID field.
            value: A UUID, its text, or None.
            instance: The model or instance the value is written for.

        Returns:
            The bytes, None for None.
        """
        text = field.to_db_value(value, instance)
        return None if text is None else uuid.UUID(text).bytes

    @staticmethod
    def get_uuid(field: UUIDField[Any], value: Any) -> uuid.UUID | None:
        """A UUID read back from its BLOB column.

        Args:
            field: The UUID field.
            value: The column's bytes, a UUID, its text, or None.

        Returns:
            The UUID, None for None.
        """
        if value is None or isinstance(value, uuid.UUID):
            return value
        if isinstance(value, bytes):
            return uuid.UUID(bytes=value)
        return uuid.UUID(str(value))

    def build_renderers(self) -> TermRenderers:
        renderers = super().build_renderers()
        renderers.register_function("LENGTH", self.render_length)
        return renderers

    @staticmethod
    def render_length(length: Function, ctx: SqlContext) -> str:
        """``LENGTH`` of the value's text - a number or a BLOB counts its characters too.

        Args:
            length: The function.
            ctx: The rendering context.

        Returns:
            The SQL.
        """
        return f"length(CAST({length.get_arg_sql(length.args[0], ctx)} AS TEXT))"


COLUMNAR_DIALECT = ColumnarDialect()
