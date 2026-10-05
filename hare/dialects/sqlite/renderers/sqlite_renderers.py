from __future__ import annotations

import datetime
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from hare.dialects.base.renderers.checked_term_renderers import CheckedTermRenderers
from hare.dialects.sqlite.renderers.sqlite_defaults import SqliteDefaults
from hare.dialects.sqlite.renderers.sqlite_json_renderers import SqliteJsonRenderers
from hare.dialects.sqlite.renderers.sqlite_number_renderers import SqliteNumberRenderers
from hare.dialects.sqlite.renderers.sqlite_temporal_renderers import SqliteTemporalRenderers
from hare.dialects.sqlite.renderers.sqlite_text_renderers import SqliteTextRenderers
from hare.sql.constants import SQL_DIVISOR_FUNCTION_NAME
from hare.sql.sql_context import SqlContext
from hare.sql.terms.values.row_value_list import RowValueList
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.sql.sql_context import SqlContext
    from hare.sql.terms.term import Term


class SqliteRenderers(CheckedTermRenderers):
    """How SQLite renders the terms whose SQL differs between dialects - mostly as calls of hare's
    own SQLite functions, which reproduce PostgreSQL's semantics."""

    def add_dialect_renderers(self) -> None:
        SqliteJsonRenderers.register(self)
        SqliteTemporalRenderers.register(self)
        SqliteNumberRenderers.register(self)
        SqliteTextRenderers.register(self)
        self.register(RowValueList, self.render_row_value_list)
        SqliteDefaults.register(self)
        # Local import: the vector, search and spatial terms import the SQL functions package, which
        # imports this one.
        from hare.dialects.sqlite.search.sqlite_search_renderers import SqliteSearchRenderers
        from hare.dialects.sqlite.spatial.sqlite_spatial_renderers import SqliteSpatialRenderers
        from hare.dialects.sqlite.vectors.sqlite_vector_renderers import SqliteVectorRenderers

        SqliteVectorRenderers.register(self)
        SqliteSearchRenderers.register(self)
        SqliteSpatialRenderers.register(self)

    @staticmethod
    def render_row_value_list(row_value_list: RowValueList, sql_context: SqlContext) -> str:
        """``(VALUES (?, ?), ...)`` - SQLite before 3.37 takes no list of row values after ``IN``."""
        rows_sql = ",".join(row.get_sql(sql_context) for row in row_value_list.values)
        return sql_context.format_alias_sql(f"(VALUES {rows_sql})", row_value_list.alias)

    is_distinct_from_operator = "IS NOT"

    def get_json_path_comparand(
        self, value: Any, encode_json_text: Callable[[Any], str], column_type: str | None, *, as_parameter: bool
    ) -> Any:
        # JSON is text read by functions: a number compares as-is and any other value as JSON
        # text, the form the path's own expression gives.
        if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
            return self.dialect.parameters.get_bindable_number(value)
        return encode_json_text(value)

    def get_decimal_compared_term(self, term: Term, *, only_decimals: bool) -> Term:
        # A Decimal binds as TEXT, and a result without column affinity compares TEXT above every
        # number. Compared with Decimals only, the term is read as exact decimal text under the
        # decimal collation; otherwise as a number.
        # Imported here: the modules import each other.
        from hare.dialects.sqlite.functions.decimal.sqlite_decimal_comparand import SqliteDecimalComparand
        from hare.sql.functions.numeric_cast import NumericCast

        return SqliteDecimalComparand(term) if only_decimals else NumericCast(term)

    def get_decimal_value_term(self, term: Any) -> Any:
        # A Decimal is stored as text: a literal or a DecimalField column's collated text is read
        # as a number where it meets other numbers.
        # Local imports: the SQL functions import this module.
        from hare.sql.functions.numeric_cast import NumericCast
        from hare.sql.functions.text.collate import Collate

        if Collate.is_decimal_text(term):
            return NumericCast(Collate.strip(term))
        if isinstance(term, Decimal) or (isinstance(term, ValueWrapper) and isinstance(term.value, Decimal)):
            return NumericCast(term)
        return term

    def get_value_range_ordering_term(self, term: Term) -> Term:
        # A decimal column is text: SQLite adds no offset to it, and the frame holds the row's peers
        # alone - ordered by its number. Imported here: the modules import each other.
        from hare.dialects.sqlite.constants import SQLITE_DECIMAL_COLLATION_NAME
        from hare.sql.functions.numeric_cast import NumericCast
        from hare.sql.functions.text.collate import Collate

        if isinstance(term, Collate) and term.collation == SQLITE_DECIMAL_COLLATION_NAME:
            return NumericCast(term.args[0], alias=term.alias)
        return term

    def get_ordering_term(self, term: Term, sql_context: SqlContext) -> Term:
        # A decimal or time column under its collation is sorted by its byte key - one function call
        # per row instead of a collation call per pair of rows. SQL kept in DDL names no function.
        # Imported here: the modules import each other.
        from hare.dialects.sqlite.constants import (
            SQLITE_DECIMAL_COLLATION_NAME,
            SQLITE_DECIMAL_SORT_KEY_FUNCTION_NAME,
            SQLITE_TIME_COLLATION_NAME,
            SQLITE_TIME_SORT_KEY_FUNCTION_NAME,
        )
        from hare.sql.functions.text.collate import Collate
        from hare.sql.terms.functions.function import Function

        if sql_context.native_functions_only or not isinstance(term, Collate):
            return term
        if term.collation == SQLITE_DECIMAL_COLLATION_NAME:
            return Function(SQLITE_DECIMAL_SORT_KEY_FUNCTION_NAME, term.args[0], alias=term.alias)
        if term.collation == SQLITE_TIME_COLLATION_NAME:
            return Function(SQLITE_TIME_SORT_KEY_FUNCTION_NAME, term.args[0], alias=term.alias)
        return term

    def get_assigned_decimal_term(
        self, term: Term, max_digits: int, decimal_places: int, value_scale: int | None
    ) -> Term:
        # A Decimal is stored as text: the value is quantized and written as a plain write writes it -
        # the double SQLite computes a Decimal expression in first to the expression's scale.
        # Imported here: the modules import each other.
        from hare.dialects.sqlite.functions.decimal.sqlite_stored_decimal import SqliteStoredDecimal

        return SqliteStoredDecimal(term, max_digits, decimal_places, value_scale)

    def get_decimal_dividend(self, term: Term) -> Term:
        # A Decimal stored as text casts to an INTEGER when whole, and INTEGER / INTEGER truncates.
        # Local import: the SQL functions import this module.
        from hare.sql.functions.cast import Cast

        return Cast(term, "REAL")

    def get_checked_integer_result(self, term: Term, minimum: int, maximum: int, type_name: str) -> Term:
        # SQLite keeps any 64-bit integer and turns a larger one into a REAL - a UDF refuses both.
        # Local imports: the SQL functions import this module.
        from hare.dialects.sqlite.constants import SQLITE_INTEGER_IN_RANGE_FUNCTION_NAME
        from hare.sql.terms.functions.function import Function

        return Function(
            SQLITE_INTEGER_IN_RANGE_FUNCTION_NAME,
            term,
            ValueWrapper(minimum, allow_parametrize=False),
            ValueWrapper(maximum, allow_parametrize=False),
            ValueWrapper(type_name, allow_parametrize=False),
        )

    def get_divisor(self, dividend: Term, divisor: Term) -> Term:
        # SQLite gives NULL of a division by zero - a zero divisor is refused by a math UDF. A column is
        # read twice at no cost, so the UDF is called for its zero only; ``+ 0`` compares a Decimal
        # stored as text as a number. Local imports: the SQL functions import this module.
        from hare.sql.functions.declarations import MathFunction
        from hare.sql.terms.case.case import Case
        from hare.sql.terms.field import Field

        if self.is_non_zero_number(divisor):
            return divisor
        checked_divisor = MathFunction(SQL_DIVISOR_FUNCTION_NAME, dividend, divisor)
        if isinstance(divisor, Field):
            zero = ValueWrapper(0, allow_parametrize=False)
            return Case().when((divisor + zero).ne(zero), divisor).else_(checked_divisor)
        return checked_divisor

    def get_datetime_part_comparand(self, value: datetime.date | datetime.time) -> Any:
        # A datetime is stored as ISO text; its date or time of day is read as ISO text too.
        # Local import: the constants module instantiates this class.
        from hare.dialects.sqlite.constants import TIME_TEXT_FORMAT

        if isinstance(value, datetime.time):
            return TIME_TEXT_FORMAT.format(value)
        return value.isoformat()

    def get_composite_distinct_key(self, terms: Sequence[Term]) -> Term:
        # Local import: hare.sql's terms render through the dialect.
        from hare.sql.terms.functions.function import Function

        # A JSON array - SQLite has no row values in COUNT(DISTINCT ...).
        return Function("JSON_ARRAY", *terms)

    def get_connection_only_function(self, sql: str) -> str | None:
        # Local import: the constants module instantiates this class.
        from hare.dialects.sqlite.renderers.constants import SQLITE_HARE_FUNCTION_PATTERN

        for function_match in SQLITE_HARE_FUNCTION_PATTERN.finditer(sql):
            if function_match.group()[0] not in "'\"":
                return function_match.group().lower()
        return None
