from __future__ import annotations

import datetime
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from hare.dialects.base.renderers.checked_term_renderers import CheckedTermRenderers
from hare.dialects.sqlite.lookups.json.sqlite_json_type_criterion import SqliteJsonTypeCriterion
from hare.dialects.sqlite.renderers.sqlite_defaults import SqliteDefaults
from hare.dialects.sqlite.renderers.sqlite_json_renderers import SqliteJsonRenderers
from hare.dialects.sqlite.renderers.sqlite_number_renderers import SqliteNumberRenderers
from hare.dialects.sqlite.renderers.sqlite_temporal_renderers import SqliteTemporalRenderers
from hare.dialects.sqlite.renderers.sqlite_text_renderers import SqliteTextRenderers
from hare.sql.sql_context import SqlContext
from hare.sql.terms.values.row_value_list import RowValueList
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from hare.fields.field import Field
    from hare.sql.terms.term import Term


class SqliteRenderers(CheckedTermRenderers):
    """How SQLite renders the terms whose SQL differs between dialects - SQLite's own functions, and
    hare's for what SQLite has none of or a type hare keeps in a form of its own."""

    def add_dialect_renderers(self) -> None:
        SqliteJsonRenderers.register(self)
        self.register(SqliteJsonTypeCriterion, SqliteJsonRenderers.render_json_type)
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
        # A decimal column under its collation is sorted by its byte key - one function call
        # per row instead of a collation call per pair of rows. SQL kept in DDL names no function.
        # Imported here: the modules import each other.
        from hare.dialects.sqlite.constants import (
            SQLITE_DECIMAL_COLLATION_NAME,
            SQLITE_DECIMAL_SORT_KEY_FUNCTION_NAME,
        )
        from hare.sql.functions.text.collate import Collate
        from hare.sql.terms.functions.function import Function

        if sql_context.native_functions_only or not isinstance(term, Collate):
            return term
        if term.collation == SQLITE_DECIMAL_COLLATION_NAME:
            return Function(SQLITE_DECIMAL_SORT_KEY_FUNCTION_NAME, term.args[0], alias=term.alias)
        return term

    def get_assigned_decimal_term(
        self, term: Term, max_digits: int, decimal_places: int, value_scale: int | None
    ) -> Term:
        # A Decimal is stored as text: the value is quantized and written as a plain write writes it -
        # the double SQLite computes a Decimal expression in first to the expression's scale.
        # Imported here: the modules import each other.
        from hare.dialects.sqlite.functions.decimal.sqlite_stored_decimal import SqliteStoredDecimal

        return SqliteStoredDecimal(term, max_digits, decimal_places, value_scale)

    def get_decimal_operation_term(
        self, operator_name: str, left: Term, right: Term, scales: list[int | None]
    ) -> Term | None:
        # Worked out exactly from the operands' text, the result kept as decimal text under the
        # decimal collation, as a column is. Imported here: the modules import each other.
        from hare.dialects.sqlite.constants import SQLITE_DECIMAL_COLLATION_NAME
        from hare.dialects.sqlite.functions.decimal.sqlite_decimal_operation import SqliteDecimalOperation
        from hare.sql.functions.text.decimal_text_collate import DecimalTextCollate

        operation = SqliteDecimalOperation(
            operator_name, self.get_decimal_operand(left), self.get_decimal_operand(right)
        )
        return DecimalTextCollate(operation, SQLITE_DECIMAL_COLLATION_NAME)

    @staticmethod
    def get_decimal_operand(term: Term) -> Term:
        """An operand of an exact Decimal operation - its text, not the number a cast reads of it.

        Args:
            term: The operand.

        Returns:
            The operand without its collation or numeric cast.
        """
        # Imported here: the modules import each other.
        from hare.sql.functions.cast import Cast
        from hare.sql.functions.text.collate import Collate
        from hare.sql.types.sql_types import SqlTypes

        if isinstance(term, Cast) and str(term.as_type).upper() == SqlTypes.NUMERIC:
            # A numeric cast reads the text as a double - the exact text is taken instead.
            term = term.args[0]
        return Collate.strip(term)

    @staticmethod
    def is_decimal_result(output_field: Field[Any] | None) -> bool:
        """Whether a result is a Decimal - a value SQLite keeps as its exact text.

        Args:
            output_field: The field the result is decoded through.

        Returns:
            True for a Decimal.
        """
        # Imported here: the modules import each other.
        from hare.query.expressions.enums import NumericValueType
        from hare.query.expressions.numeric.numeric_typing import NumericTyping

        numeric_type = NumericTyping.get_field_type(output_field)
        return numeric_type is not None and numeric_type.type is NumericValueType.DECIMAL

    def get_result_branch_term(self, term: Term, output_field: Field[Any] | None) -> Term:
        # A branch of a Decimal result is its exact text - a numeric cast would make it a double.
        # Imported here: the modules import each other.
        from hare.dialects.sqlite.functions.decimal.sqlite_decimal_text import SqliteDecimalText

        if not self.is_decimal_result(output_field):
            return term
        return SqliteDecimalText(self.get_decimal_operand(term))

    def get_result_term(self, term: Term, output_field: Field[Any] | None) -> Term:
        # A Decimal result is compared and ordered by its exact value, as a column is.
        # Imported here: the modules import each other.
        from hare.dialects.sqlite.constants import SQLITE_DECIMAL_COLLATION_NAME
        from hare.sql.functions.text.decimal_text_collate import DecimalTextCollate

        if not self.is_decimal_result(output_field):
            return term
        return DecimalTextCollate(term, SQLITE_DECIMAL_COLLATION_NAME)

    def get_decimal_aggregate_name(self, function_name: str) -> str | None:
        # Imported here: the modules import each other.
        from hare.dialects.sqlite.functions.constants import SQLITE_DECIMAL_AGGREGATE_FUNCTION_NAMES

        return SQLITE_DECIMAL_AGGREGATE_FUNCTION_NAMES.get(function_name)

    def get_decimal_dividend(self, term: Term) -> Term:
        # A Decimal stored as text casts to an INTEGER when whole, and INTEGER / INTEGER truncates.
        # Local import: the SQL functions import this module.
        from hare.sql.functions.cast import Cast

        return Cast(term, "REAL")

    def get_checked_integer_result(self, term: Term, minimum: int, maximum: int, type_name: str) -> Term:
        # SQLite turns an integer past 64 bits into a REAL - a UDF refuses it.
        # Local imports: the SQL functions import this module.
        from hare.dialects.sqlite.constants import SQLITE_INTEGER_IN_RANGE_FUNCTION_NAME
        from hare.dialects.sqlite.functions.sqlite_checked_integer import SqliteCheckedInteger

        return SqliteCheckedInteger(
            SQLITE_INTEGER_IN_RANGE_FUNCTION_NAME,
            term,
            ValueWrapper(minimum, allow_parametrize=False),
            ValueWrapper(maximum, allow_parametrize=False),
            ValueWrapper(type_name, allow_parametrize=False),
        )

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
