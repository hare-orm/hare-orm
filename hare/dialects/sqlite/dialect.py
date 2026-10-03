from __future__ import annotations

import datetime
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from hare.dialects.base.dialect import Dialect
from hare.dialects.constants import (
    SQLITE_IN_JSON_ARRAY_THRESHOLD,
    SQLITE_MINIMUM_SERVER_VERSION,
)
from hare.dialects.enums import DialectName
from hare.dialects.sqlite.table_options import SqliteTableOptions
from hare.exceptions import UnSupportedError
from hare.transactions.enums import IsolationLevel

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.table_options import TableOptions
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.operators import FilterOperators
    from hare.dialects.base.renderers.term_renderers import TermRenderers
    from hare.dialects.base.schema.editor import BaseSchemaEditor
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.inspectdb.introspector.schema_introspector import SchemaIntrospector
    from hare.models import Model
    from hare.sql.context import SqlContext
    from hare.sql.terms.base.term import Term


class SqliteDialect(Dialect):
    """SQLite."""

    name = DialectName.SQLITE
    otel_system_name = "sqlite"
    supports_schemas = False
    is_distinct_from_operator = "IS NOT"
    sorts_nulls_first = True
    enforces_numeric_ranges = False
    single_parameter_in_list_min_length = SQLITE_IN_JSON_ARRAY_THRESHOLD
    guarantees_returning_order = False
    # Every SQLite transaction is serializable - writers are serialized by the database lock.
    isolation_levels = (IsolationLevel.SERIALIZABLE,)
    supports_adding_constraints = False
    supports_partial_indexes = True
    # An index keeps NULLs first ascending and last descending.
    supports_index_nulls_order = False
    # Every SQLite trigger fires per row.
    supports_statement_triggers = False
    minimum_server_version = SQLITE_MINIMUM_SERVER_VERSION

    def build_schema_editor_class(self) -> type[BaseSchemaEditor]:
        # Local import: the schema editor works on hare's models, whose modules import this one.
        from hare.dialects.sqlite.schema.editor import SqliteSchemaEditor

        return SqliteSchemaEditor

    def build_table_options_class(self) -> type[TableOptions] | None:
        return SqliteTableOptions

    def build_introspector_class(self) -> type[SchemaIntrospector] | None:
        # Local import: the introspector reads into hare's models and fields, whose modules import this one.
        from hare.dialects.sqlite.introspection import SqliteIntrospector

        return SqliteIntrospector

    def get_literal_sql(self, value: Any) -> str:
        """A datetime is the text the sqlite3 parameter adapter writes for one - in UTC when aware,
        with a space separator - so a row filled by the default compares equal to the same
        instant written from Python."""
        if isinstance(value, datetime.datetime):
            if value.tzinfo is not None:
                value = value.astimezone(datetime.UTC)
            return self.get_string_literal_sql(value.isoformat(" "))
        return super().get_literal_sql(value)

    def get_json_path_comparand(
        self, value: Any, encode_json_text: Callable[[Any], str], column_type: str | None, *, as_parameter: bool
    ) -> Any:
        # JSON is text read by functions: a number compares as-is and any other value as JSON
        # text, the form the path's own expression gives.
        if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
            return self.get_bindable_number(value)
        return encode_json_text(value)

    def get_decimal_compared_term(self, term: Term, *, only_decimals: bool) -> Term:
        # A Decimal binds as TEXT, and a result without column affinity compares TEXT above every
        # number. Compared with Decimals only, the term is read as exact decimal text under the
        # decimal collation; otherwise as a number.
        # Imported here: the modules import each other.
        from hare.dialects.sqlite.functions.decimal import SqliteDecimalComparand
        from hare.sql.functions.declarations import NumericCast

        return SqliteDecimalComparand(term) if only_decimals else NumericCast(term)

    def get_decimal_value_term(self, term: Any) -> Any:
        # A Decimal is stored as text: a literal or a DecimalField column's collated text is read
        # as a number where it meets other numbers.
        # Local imports: the SQL functions import this module.
        from hare.sql.functions.collate import Collate
        from hare.sql.functions.declarations import NumericCast
        from hare.sql.terms.base.value_wrapper import ValueWrapper

        if Collate.is_decimal_text(term):
            return NumericCast(Collate.strip(term))
        if isinstance(term, Decimal) or (isinstance(term, ValueWrapper) and isinstance(term.value, Decimal)):
            return NumericCast(term)
        return term

    def get_ordering_term(self, term: Term, ctx: SqlContext) -> Term:
        # A decimal or time column under its collation is sorted by its byte key - one function call
        # per row instead of a collation call per pair of rows. SQL kept in DDL names no function.
        # Imported here: the modules import each other.
        from hare.dialects.sqlite.constants import (
            SQLITE_DECIMAL_COLLATION_NAME,
            SQLITE_DECIMAL_SORT_KEY_FUNCTION_NAME,
            SQLITE_TIME_COLLATION_NAME,
            SQLITE_TIME_SORT_KEY_FUNCTION_NAME,
        )
        from hare.sql.functions.collate import Collate
        from hare.sql.terms.functions.function import Function

        if ctx.native_functions_only or not isinstance(term, Collate):
            return term
        if term.collation == SQLITE_DECIMAL_COLLATION_NAME:
            return Function(SQLITE_DECIMAL_SORT_KEY_FUNCTION_NAME, term.args[0], alias=term.alias)
        if term.collation == SQLITE_TIME_COLLATION_NAME:
            return Function(SQLITE_TIME_SORT_KEY_FUNCTION_NAME, term.args[0], alias=term.alias)
        return term

    def get_assigned_decimal_term(self, term: Term, max_digits: int, decimal_places: int) -> Term:
        # A Decimal is stored as text: the value is quantized and written as a plain write writes it.
        # Imported here: the modules import each other.
        from hare.dialects.sqlite.functions.sqlite_stored_decimal import SqliteStoredDecimal

        return SqliteStoredDecimal(term, max_digits, decimal_places)

    def get_decimal_dividend(self, term: Term) -> Term:
        # A Decimal stored as text casts to an INTEGER when whole, and INTEGER / INTEGER truncates.
        # Local import: the SQL functions import this module.
        from hare.sql.functions.cast import Cast

        return Cast(term, "REAL")

    def get_datetime_part_comparand(self, value: datetime.date | datetime.time) -> Any:
        # A datetime is stored as ISO text; its date or time of day is read as ISO text too.
        # Local import: the constants module instantiates this class.
        from hare.dialects.sqlite.constants import TIME_TEXT_FORMAT

        if isinstance(value, datetime.time):
            return TIME_TEXT_FORMAT.format(value)
        return value.isoformat()

    async def clear_tables(self, db: DatabaseClient, quoted_tables: Sequence[str]) -> None:
        # Foreign keys are switched off meanwhile - a self-referencing or circular relation has no
        # delete order that satisfies them.
        await db.execute_script("PRAGMA foreign_keys = OFF")
        try:
            await super().clear_tables(db, quoted_tables)
        finally:
            await db.execute_script("PRAGMA foreign_keys = ON")

    def build_types(self) -> TypeRegistry:
        # Local import: the registry names hare's field classes, whose modules import this one.
        from hare.dialects.sqlite.types import SqliteTypes

        return SqliteTypes.build()

    def build_filter_operators(self) -> FilterOperators:
        # Local import: the operators are hare's lookups, whose modules import this one.
        from hare.dialects.sqlite.operators import SqliteFilterOperators

        return SqliteFilterOperators.build()

    def build_renderers(self) -> TermRenderers:
        # Local import: the renderers name hare's terms, whose modules import this one.
        from hare.dialects.sqlite.renderers import SqliteRenderers

        return SqliteRenderers.build()

    def get_array_literal_sql(self, element_sqls: Sequence[str]) -> str:
        # A JSON array.
        return f"[{','.join(element_sqls)}]"

    def get_composite_distinct_key(self, terms: Sequence[Term]) -> Term:
        # Local import: hare.sql's terms render through the dialect.
        from hare.sql.terms.functions.function import Function

        # A JSON array - SQLite has no row values in COUNT(DISTINCT ...).
        return Function("JSON_ARRAY", *terms)

    def get_bindable_number(self, value: int | float | Decimal) -> int | float | Decimal:
        # Local import: the constants module instantiates this class.
        from hare.dialects.sqlite.constants import SQLITE_INTEGER_MAX, SQLITE_INTEGER_MIN

        # A JSON integer outside 64 bits is held as a REAL, and can't be bound as an int.
        if isinstance(value, int) and not SQLITE_INTEGER_MIN <= value <= SQLITE_INTEGER_MAX:
            return float(value)
        return value

    def get_connection_only_function(self, sql: str) -> str | None:
        # Local import: the constants module instantiates this class.
        from hare.dialects.sqlite.constants import SQLITE_OWN_FUNCTION_PATTERN

        own_function = SQLITE_OWN_FUNCTION_PATTERN.search(sql)
        return None if own_function is None else own_function.group()

    def get_unbounded_limit_sql(self) -> str | None:
        # SQLite accepts no OFFSET without a LIMIT; a negative one means none.
        return "-1"

    def get_isolation_level_sql(self, level: IsolationLevel) -> str | None:
        return None

    def defer_cascade_foreign_keys(self, model: type[Model], db: DatabaseClient) -> AbstractAsyncContextManager[bool]:
        # Local import: the deferral walks hare's models, whose modules import this one.
        from hare.dialects.sqlite.deletion import SqliteForeignKeyDeferral

        return SqliteForeignKeyDeferral.defer(model, db)

    def get_explain_sql(self, sql: str, output_format: str | None, options: Mapping[str, bool]) -> str:
        if output_format:
            raise UnSupportedError("SQLite does not support different explain formats")
        if options:
            raise UnSupportedError("SQLite does not support explain options")
        return f"EXPLAIN QUERY PLAN {sql}"
