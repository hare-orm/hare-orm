from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from hare.dialects.base.renderers.checked_term_renderers import CheckedTermRenderers
from hare.dialects.clickhouse.constants import CLICKHOUSE_NEVER_NULLABLE_TYPE_PREFIXES
from hare.dialects.clickhouse.fields.constants import UINT64_RANGE
from hare.dialects.clickhouse.renderers.clickhouse_aggregate_renderers import ClickhouseAggregateRenderers
from hare.dialects.clickhouse.renderers.clickhouse_assigned_enum_number import ClickhouseAssignedEnumNumber
from hare.dialects.clickhouse.renderers.clickhouse_cast_renderers import ClickhouseCastRenderers
from hare.dialects.clickhouse.renderers.clickhouse_condition_renderers import ClickhouseConditionRenderers
from hare.dialects.clickhouse.renderers.clickhouse_container_renderers import ClickhouseContainerRenderers
from hare.dialects.clickhouse.renderers.clickhouse_json_renderers import ClickhouseJsonRenderers
from hare.dialects.clickhouse.renderers.clickhouse_number_renderers import ClickhouseNumberRenderers
from hare.dialects.clickhouse.renderers.clickhouse_temporal_renderers import ClickhouseTemporalRenderers
from hare.dialects.clickhouse.renderers.clickhouse_text_renderers import ClickhouseTextRenderers
from hare.dialects.clickhouse.renderers.clickhouse_window_renderers import ClickhouseWindowRenderers
from hare.dialects.clickhouse.renderers.constants import (
    CLICKHOUSE_INTEGER_TYPE_PATTERN,
    CLICKHOUSE_ORDER_SQLS,
    CLICKHOUSE_VALUE_RANGE_KEY_TEMPLATE,
    DECIMAL_QUOTIENT_SCALE,
)
from hare.dialects.clickhouse.spatial.clickhouse_spatial_renderers import ClickhouseSpatialRenderers
from hare.dialects.clickhouse.types.clickhouse_type_names import ClickhouseTypeNames
from hare.dialects.clickhouse.types.constants import CLICKHOUSE_JSON_COLUMN_TYPE
from hare.exceptions import UnSupportedError
from hare.fields.data.constants import INT64_MIN
from hare.sql import functions
from hare.sql.order import Order

if TYPE_CHECKING:
    import re

    from hare.fields.field import Field
    from hare.sql.sql_context import SqlContext
    from hare.sql.terms.functions.analytic_function import AnalyticFunction
    from hare.sql.terms.term import Term


class ClickhouseRenderers(CheckedTermRenderers):
    """How ClickHouse renders the terms whose SQL differs between dialects."""

    def add_dialect_renderers(self) -> None:
        ClickhouseAggregateRenderers.register(self)
        ClickhouseCastRenderers.register(self)
        ClickhouseConditionRenderers.register(self)
        ClickhouseContainerRenderers.register(self)
        ClickhouseJsonRenderers.register(self)
        ClickhouseTemporalRenderers.register(self)
        ClickhouseNumberRenderers.register(self)
        ClickhouseTextRenderers.register(self)
        ClickhouseSpatialRenderers.register(self)
        ClickhouseWindowRenderers.register(self)

    def get_order_sql(self, order: Order) -> str:
        return CLICKHOUSE_ORDER_SQLS.get(order, order.value)

    def get_null_if_result(self, term: Term, field: Field[Any] | None) -> Term:
        """Checks that the NULLIF result can use the database's nullable representation.

        Args:
            term: The NULLIF call.
            field: The first argument's field, None when its type is unknown.

        Returns:
            The unchanged call for a supported or unknown argument type.

        Raises:
            UnSupportedError: The argument's column type cannot be wrapped in Nullable,
                including the intrinsically nullable Dynamic and Variant types.
        """
        if field is not None:
            value_field = self.dialect.types.get_value_field(field)
            column_type = value_field.get_column_type(self.dialect)
            if column_type.startswith(CLICKHOUSE_NEVER_NULLABLE_TYPE_PREFIXES):
                raise UnSupportedError(f"NullIf() cannot return Nullable({column_type}) on clickhouse")
        return term

    def get_window_ordering_sql(self, function: AnalyticFunction, term_sql: str, order: Order | None) -> str:
        # Local import: hare.sql's terms render through the dialect.
        from hare.sql.terms.functions.window_frame_analytic_function import WindowFrameAnalyticFunction

        if not isinstance(function, WindowFrameAnalyticFunction) or not function.has_value_offsets():
            return super().get_window_ordering_sql(function, term_sql, order)
        # With a descending ordering's NULLS FIRST (CLICKHOUSE_ORDER_SQLS) every row's range of values
        # would be the whole partition - the ordering is by a key putting a NULL where it sorts.
        order = order or Order.ASC
        null_key = "inf" if order.puts_nulls_first(sorts_nulls_first=False) != order.is_ascending else "-inf"
        direction = "ASC" if order.is_ascending else "DESC"
        return f"{CLICKHOUSE_VALUE_RANGE_KEY_TEMPLATE.format(term_sql, null_key)} {direction}"

    def get_json_path_comparand(
        self, value: Any, encode_json_text: Callable[[Any], str], column_type: str | None, *, as_parameter: bool
    ) -> Any:
        # A path reads JSON text as ClickHouse writes it: the value is compared as that text too -
        # an item of a list is turned into it by the list lookup's operator.
        # Local import: the lookups module imports hare's SQL terms, which render through this one.
        from hare.dialects.clickhouse.lookups.clickhouse_json_value_text import ClickhouseJsonValueText

        # Local import: the lookups module imports hare's SQL terms, which render through this one.
        from hare.dialects.clickhouse.lookups.declarations import ClickhouseJsonNullText
        from hare.sql.terms.values.value_wrapper import ValueWrapper

        if value is None and column_type == CLICKHOUSE_JSON_COLUMN_TYPE:
            # A JSON column keeps no null - a path holding one reads as a missing path, NULL.
            return None if as_parameter else ClickhouseJsonNullText(ValueWrapper(encode_json_text(value)))
        json_text = encode_json_text(value)
        return json_text if as_parameter else ClickhouseJsonValueText(ValueWrapper(json_text))

    def get_distinct_from_sql(self, left_sql: str, right_sql: str) -> str:
        # ClickHouse has IS DISTINCT FROM in no condition but a JOIN's: the two are distinct when they
        # aren't equal - a NULL on one side included - unless both are NULL.
        return f"(ifNull({left_sql} = {right_sql}, 0) = 0 AND NOT (isNull({left_sql}) AND isNull({right_sql})))"

    def get_concatenated_argument_sql(self, argument_sql: str, argument: Any) -> str:
        # concat() of text and a date, a UUID or another value that isn't text finds no common type.
        return f"toString({argument_sql})"

    def get_ordering_clause_sql(self, term: Term, term_sql: str, order: Order | None, sql_context: SqlContext) -> str:
        collation_suffix = self.get_collation_suffix(term, term_sql, sql_context)
        if collation_suffix is None:
            return super().get_ordering_clause_sql(term, term_sql, order, sql_context)
        value_sql = term_sql[: -len(collation_suffix)]
        return f"{super().get_ordering_clause_sql(term, value_sql, order, sql_context)}{collation_suffix}"

    def get_window_ordering_clause_sql(
        self, function: AnalyticFunction, term: Term, term_sql: str, order: Order | None, sql_context: SqlContext
    ) -> str:
        collation_suffix = self.get_collation_suffix(term, term_sql, sql_context)
        if collation_suffix is None:
            return super().get_window_ordering_clause_sql(function, term, term_sql, order, sql_context)
        value_sql = term_sql[: -len(collation_suffix)]
        return f"{self.get_window_ordering_sql(function, value_sql, order)}{collation_suffix}"

    @staticmethod
    def get_collation_suffix(term: Term, term_sql: str, sql_context: SqlContext) -> str | None:
        """The ``COLLATE 'name'`` an ordered term's SQL ends with - it follows the direction on ClickHouse
        (``ORDER BY value ASC COLLATE 'en'``).

        Args:
            term: The ordered term.
            term_sql: Its SQL.
            sql_context: The rendering state of the statement.

        Returns:
            The suffix, None for a term of no collation a user named.
        """
        if not isinstance(term, functions.Collate) or not term.names_collation:
            return None
        collation_suffix = f" COLLATE {sql_context.dialect.literals.get_collation_sql(term.collation)}"
        return collation_suffix if term_sql.endswith(collation_suffix) else None

    def get_result_branch_term(self, term: Term, output_field: Field[Any] | None) -> Term:
        # ClickHouse refuses a CASE or a COALESCE of Decimals of different scales - each branch of a
        # Decimal result is cast to the result's scale. Local imports: the fields and hare.sql's terms
        # render through the dialect.
        from hare.fields.data.json.json_field import JSONField
        from hare.fields.data.numeric.decimal_field import DecimalField
        from hare.fields.data.numeric.float_field import FloatField
        from hare.fields.generated_field import GeneratedField
        from hare.query.expressions.numeric.unrounded_decimal_field import UnroundedDecimalField
        from hare.sql.terms.functions.function import Function
        from hare.sql.terms.values.literal_value import LiteralValue
        from hare.sql.terms.values.value_wrapper import ValueWrapper

        effective_field = None if output_field is None else GeneratedField.get_effective_field(output_field)
        if isinstance(effective_field, JSONField):
            # A JSON column and a dict literal's text have no common type either - each branch is read as
            # its JSON text, the form the field decodes.
            return Function("toString", term)
        if isinstance(effective_field, FloatField):
            # A float result of a Decimal and a float branch has no common type either - each branch
            # is read as a float; toFloat64() keeps a NULL.
            return Function("toFloat64", term)
        if effective_field is not None and effective_field.field_type is int and isinstance(term, ValueWrapper):
            # A number literal is read as the narrowest type holding it - literals of different
            # widths and signs (UInt256 and Int8) share no type. Read as the result's own integer
            # type; accurateCast() refuses one out of its range rather than wrapping it. An enum's
            # number stays as it is - cast, it would be read as the member's name.
            server_type = ClickhouseTypeNames.get_server_type(effective_field.get_column_type(self.dialect))
            integer_type_match = CLICKHOUSE_INTEGER_TYPE_PATTERN.fullmatch(server_type)
            # A literal beyond the result's type (a result taken from the literals themselves, an
            # Int64 of 2**64 - 1) stays as it is - it is read as a type holding it.
            if integer_type_match and self.integer_type_holds(integer_type_match, term.value):
                # A NULL branch keeps the result nullable.
                cast_type = f"Nullable({server_type})" if term.value is None else server_type
                return Function("accurateCast", term, LiteralValue(f"'{cast_type}'"))
        if not isinstance(effective_field, DecimalField):
            return term
        scale = (
            DECIMAL_QUOTIENT_SCALE
            if isinstance(effective_field, UnroundedDecimalField)
            else effective_field.decimal_places
        )
        return Function("toDecimal256", term, scale)

    @staticmethod
    def integer_type_holds(integer_type_match: re.Match[str], value: Any) -> bool:
        """Whether an integer type holds a literal's value.

        Args:
            integer_type_match: The type's match of ``CLICKHOUSE_INTEGER_TYPE_PATTERN``.
            value: The literal's value - None and a value of no integer always fit.

        Returns:
            Whether it does.
        """
        if not isinstance(value, int) or isinstance(value, bool):
            return True
        bits = int(integer_type_match["bits"])
        if integer_type_match["unsigned"]:
            return 0 <= value < 2**bits
        return -(2 ** (bits - 1)) <= value < 2 ** (bits - 1)

    def get_assigned_enum_term(self, term: Term, member_numbers: list[int]) -> Term:
        # A mutation writes a number no label has into an Enum8/Enum16 column, even through a CAST to
        # its type - the drivers then read it as NULL or fail; such a number is refused.
        return ClickhouseAssignedEnumNumber(term, member_numbers)

    def get_enum_number_term(self, term: Term) -> Term:
        # An Enum16 column takes no arithmetic, average or math function - its number does. Local
        # import: hare.sql's terms render through the dialect.
        from hare.sql.terms.functions.function import Function

        return Function("toInt16", term)

    def get_same_type_math_result(self, term: Term, field: Field[Any] | None) -> Term:
        # abs() of a signed integer is unsigned - no common type with a signed one in Coalesce()/Case();
        # converted back to the argument's column type, as ClickHouse converts (the type's minimum
        # stays itself). The conversion keeps a Nullable. Local imports: the fields and hare.sql's
        # terms render through the dialect.
        from hare.fields.generated_field import GeneratedField
        from hare.sql.terms.functions.function import Function

        effective_field = None if field is None else GeneratedField.get_effective_field(field)
        if effective_field is None or effective_field.field_type is not int:
            return term
        server_type = ClickhouseTypeNames.get_server_type(effective_field.get_column_type(self.dialect))
        # An enum's number is no integer column type.
        if CLICKHOUSE_INTEGER_TYPE_PATTERN.fullmatch(server_type) is None:
            return term
        return Function(f"to{server_type}", term)

    def get_scaled_duration(self, term: Term) -> Term:
        # The microseconds times a float are a Float64, which shares no type with a duration's Int64 in
        # Coalesce()/Greatest()/Case() - read back as the column's integer, a half to the even one, as
        # Python makes a timedelta of a fraction of microseconds. Local import: hare.sql's terms render
        # through the dialect.
        from hare.sql.terms.functions.function import Function

        return Function("toInt64", Function("roundBankers", term))

    def get_float_operand(self, term: Term, field: Field[Any] | None) -> Term:
        # ClickHouse has no arithmetic of an integer past 64 bits and a Float64 - such an operand, a
        # column's or a literal's, is read as a float. Local imports: hare.sql's terms render through
        # the dialect.
        from hare.fields.generated_field import GeneratedField
        from hare.sql.terms.functions.function import Function
        from hare.sql.terms.values.value_wrapper import ValueWrapper

        if isinstance(term, ValueWrapper) and isinstance(term.value, int):
            wide = not INT64_MIN <= term.value <= UINT64_RANGE[1]
        else:
            effective_field = None if field is None else GeneratedField.get_effective_field(field)
            column_range = getattr(effective_field, "COLUMN_TYPE_RANGE", None)
            wide = column_range is not None and (column_range[0] < INT64_MIN or column_range[1] > UINT64_RANGE[1])
        return Function("toFloat64", term) if wide else term

    def get_statistic_as_float(self, term: Term) -> Term:
        # Local import: hare.sql's terms render through the dialect. toFloat64() keeps the NULL of an
        # aggregate over no rows, where a CAST to Float64 would fail on it.
        from hare.sql.terms.functions.function import Function

        return Function("toFloat64", term)

    def get_never_null_column_count_argument(self, term: Term, *, is_filtered: bool) -> Term:
        # Local import: hare.sql's terms render through the dialect. count() reads no column - but a
        # count with a condition takes the column: COUNT(*) FILTER is countIf of two arguments.
        from hare.sql.terms.star import Star

        return term if is_filtered else Star()

    def get_composite_distinct_key(self, terms: Sequence[Term]) -> Term:
        # Local import: hare.sql's terms render through the dialect.
        from hare.sql.terms.functions.function import Function

        # As text: the counted key is NULL for a row of no key, and a tuple is never Nullable.
        return Function("toString", Function("tuple", *terms))
