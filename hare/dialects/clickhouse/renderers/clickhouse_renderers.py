from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, cast

from hare.dialects.base.renderers.checked_term_renderers import CheckedTermRenderers
from hare.dialects.clickhouse.renderers.clickhouse_aggregate_renderers import ClickhouseAggregateRenderers
from hare.dialects.clickhouse.renderers.clickhouse_cast_renderers import ClickhouseCastRenderers
from hare.dialects.clickhouse.renderers.clickhouse_condition_renderers import ClickhouseConditionRenderers
from hare.dialects.clickhouse.renderers.clickhouse_container_renderers import ClickhouseContainerRenderers
from hare.dialects.clickhouse.renderers.clickhouse_json_renderers import ClickhouseJsonRenderers
from hare.dialects.clickhouse.renderers.clickhouse_number_renderers import ClickhouseNumberRenderers
from hare.dialects.clickhouse.renderers.clickhouse_temporal_renderers import ClickhouseTemporalRenderers
from hare.dialects.clickhouse.renderers.clickhouse_text_renderers import ClickhouseTextRenderers
from hare.dialects.clickhouse.renderers.clickhouse_window_renderers import ClickhouseWindowRenderers
from hare.dialects.clickhouse.renderers.constants import (
    CLICKHOUSE_EXACT_INTEGER_TYPE,
    CLICKHOUSE_INTEGER_DIVISION_FUNCTION_NAME,
    CLICKHOUSE_INTEGER_TYPE_PATTERN,
    CLICKHOUSE_NUMERIC_SCALE,
    CLICKHOUSE_ORDER_SQLS,
    CLICKHOUSE_QUOTIENT_SCALE,
    CLICKHOUSE_VALUE_RANGE_KEY_TEMPLATE,
)
from hare.dialects.clickhouse.spatial.clickhouse_spatial_renderers import ClickhouseSpatialRenderers
from hare.dialects.clickhouse.types.clickhouse_type_names import ClickhouseTypeNames
from hare.sql.constants import SQL_DIVISOR_FUNCTION_NAME
from hare.sql.enums import Order

if TYPE_CHECKING:  # pragma: nocoverage
    import re

    from hare.dialects.clickhouse.clickhouse_dialect import ClickhouseDialect
    from hare.fields.field import Field
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

    def get_window_ordering_sql(self, function: AnalyticFunction, term_sql: str, order: Order | None) -> str:
        # Local import: hare.sql's terms render through the dialect.
        from hare.sql.terms.functions.window_frame_analytic_function import WindowFrameAnalyticFunction

        if not isinstance(function, WindowFrameAnalyticFunction) or not function.has_value_offsets():
            return super().get_window_ordering_sql(function, term_sql, order)
        # A range of values around a NULL ordering value is its peers alone on PostgreSQL; ClickHouse
        # takes other rows into it, and with NULLS FIRST every row's range is the whole partition.
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

        if value is None and cast("ClickhouseDialect", self.dialect).stores_json_natively:
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

    def get_integer_quotient(self, dividend: Term, divisor: Term) -> Term:
        # ``/`` of integers gives a float on ClickHouse; intDiv() rounds towards zero, as PostgreSQL.
        # Local import: hare.sql's terms render through the dialect.
        from hare.sql.terms.functions.function import Function

        return Function(CLICKHOUSE_INTEGER_DIVISION_FUNCTION_NAME, dividend, divisor)

    def get_divisor(self, dividend: Term, divisor: Term) -> Term:
        # ClickHouse gives an infinity of a division by zero - the divisor is checked by its math
        # function template. Local import: hare.sql's terms render through the dialect.
        from hare.sql.functions.declarations import MathFunction

        if self.is_non_zero_number(divisor):
            return divisor
        return MathFunction(SQL_DIVISOR_FUNCTION_NAME, dividend, divisor)

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
            CLICKHOUSE_QUOTIENT_SCALE
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

    def get_assigned_decimal_term(
        self, term: Term, max_digits: int, decimal_places: int, value_scale: int | None
    ) -> Term:
        # ClickHouse cuts a value of more places to the column's scale - it is rounded half away from
        # zero first, as PostgreSQL rounds it; a float by its text, as a Decimal is. Local import:
        # hare.sql's terms render through the dialect.
        from hare.sql.terms.functions.function import Function

        return Function(
            "round",
            Function("toDecimal256", Function("toString", term), CLICKHOUSE_NUMERIC_SCALE),
            decimal_places,
        )

    def get_enum_number_term(self, term: Term) -> Term:
        # An Enum16 column takes no arithmetic, average or math function - its number does. Local
        # import: hare.sql's terms render through the dialect.
        from hare.sql.terms.functions.function import Function

        return Function("toInt16", term)

    def get_exact_integer_operand(self, term: Term) -> Term:
        # ClickHouse wraps a 64-bit sum around - the operands are widened, so that the written value's
        # check sees the exact result; accurateCast() refuses a UInt256 too large for Int256. Local
        # import: hare.sql's terms render through the dialect.
        from hare.sql.terms.functions.function import Function
        from hare.sql.terms.values.literal_value import LiteralValue

        return Function("accurateCast", term, LiteralValue(CLICKHOUSE_EXACT_INTEGER_TYPE))

    def get_checked_integer_result(self, term: Term, minimum: int, maximum: int, type_name: str) -> Term:
        # The result of widened operands, refused out of the type's range by a check the value
        # carries. Local import: hare.sql's terms render through the dialect.
        from hare.sql.terms.functions.function import Function
        from hare.sql.terms.values.literal_value import LiteralValue
        from hare.sql.terms.values.value_wrapper import ValueWrapper

        out_of_range = (term < LiteralValue(str(minimum))) | (term > LiteralValue(str(maximum)))
        refusal = Function(
            "throwIf",
            Function("ifNull", out_of_range, ValueWrapper(0, allow_parametrize=False)),
            ValueWrapper(f"{type_name} out of range", allow_parametrize=False),
        )
        return term + refusal

    def get_decimal_dividend(self, term: Term) -> Term:
        # A Decimal quotient has the dividend's scale on ClickHouse - the dividend is widened, so that
        # the quotient isn't cut. Local import: hare.sql's terms render through the dialect.
        from hare.sql.terms.functions.function import Function

        return Function("toDecimal256", term, CLICKHOUSE_QUOTIENT_SCALE)

    def get_integer_aggregate_as_float(self, term: Term) -> Term:
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
