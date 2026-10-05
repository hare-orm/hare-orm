from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, cast

from hare.dialects.base.renderers.checked_term_renderers import CheckedTermRenderers
from hare.dialects.clickhouse.renderers.clickhouse_aggregate_renderers import ClickhouseAggregateRenderers
from hare.dialects.clickhouse.renderers.clickhouse_assigned_enum_number import ClickhouseAssignedEnumNumber
from hare.dialects.clickhouse.renderers.clickhouse_cast_renderers import ClickhouseCastRenderers
from hare.dialects.clickhouse.renderers.clickhouse_checked_integer_result import ClickhouseCheckedIntegerResult
from hare.dialects.clickhouse.renderers.clickhouse_condition_renderers import ClickhouseConditionRenderers
from hare.dialects.clickhouse.renderers.clickhouse_container_renderers import ClickhouseContainerRenderers
from hare.dialects.clickhouse.renderers.clickhouse_json_renderers import ClickhouseJsonRenderers
from hare.dialects.clickhouse.renderers.clickhouse_number_renderers import ClickhouseNumberRenderers
from hare.dialects.clickhouse.renderers.clickhouse_temporal_renderers import ClickhouseTemporalRenderers
from hare.dialects.clickhouse.renderers.clickhouse_text_renderers import ClickhouseTextRenderers
from hare.dialects.clickhouse.renderers.clickhouse_window_renderers import ClickhouseWindowRenderers
from hare.dialects.clickhouse.renderers.constants import (
    CLICKHOUSE_DECIMAL_AGGREGATE_NAMES,
    CLICKHOUSE_EXACT_INTEGER_TYPE,
    CLICKHOUSE_INTEGER_TYPE_PATTERN,
    CLICKHOUSE_NULLABLE_EXACT_INTEGER_TYPE,
    CLICKHOUSE_NUMERIC_OVERFLOW_MESSAGE,
    CLICKHOUSE_NUMERIC_SCALE,
    CLICKHOUSE_ORDER_SQLS,
    CLICKHOUSE_VALUE_RANGE_KEY_TEMPLATE,
    CLICKHOUSE_WIDE_DECIMAL_DIGITS,
)
from hare.dialects.clickhouse.spatial.clickhouse_spatial_renderers import ClickhouseSpatialRenderers
from hare.dialects.clickhouse.types.clickhouse_type_names import ClickhouseTypeNames
from hare.dialects.clickhouse.types.constants import CLICKHOUSE_JSON_COLUMN_TYPE
from hare.sql import functions
from hare.sql.constants import DECIMAL_QUOTIENT_SCALE
from hare.sql.enums import Order

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

    def get_assigned_decimal_term(
        self, term: Term, max_digits: int, decimal_places: int, value_scale: int | None
    ) -> Term:
        # A float goes in by its text - converted as a double, 0.29 would be cut to 0.28. Local import:
        # hare.sql's terms render through the dialect.
        from hare.sql.terms.functions.function import Function

        return Function("toDecimal256", Function("toString", term), CLICKHOUSE_NUMERIC_SCALE)

    def get_assigned_enum_term(self, term: Term, member_numbers: list[int]) -> Term:
        # A mutation writes a number no label has into an Enum8/Enum16 column, even through a CAST to
        # its type - the drivers then read it as NULL or fail; such a number is refused.
        return ClickhouseAssignedEnumNumber(term, member_numbers)

    def get_enum_number_term(self, term: Term) -> Term:
        # An Enum16 column takes no arithmetic, average or math function - its number does. Local
        # import: hare.sql's terms render through the dialect.
        from hare.sql.terms.functions.function import Function

        return Function("toInt16", term)

    def get_widened_integer_sum_argument(self, term: Term) -> Term:
        # sum() of an Int64 is an Int64 that wraps around - summed as an Int128, which no sum of
        # Int64 values overflows; widening never fails, and keeps a Nullable. Local import: hare.sql's
        # terms render through the dialect.
        from hare.sql.terms.functions.function import Function

        return Function("toInt128", term)

    def get_exact_integer_operand(self, term: Term, may_be_null: bool) -> Term:
        # ClickHouse wraps a 64-bit sum around - the operands are widened, so that the read result's
        # range check sees the exact result; accurateCast() refuses a UInt256 too large for Int256. A NULL
        # operand needs the Nullable type, which costs a sum of NOT NULL operands its speed. Local
        # import: hare.sql's terms render through the dialect.
        from hare.sql.terms.functions.function import Function
        from hare.sql.terms.values.literal_value import LiteralValue

        exact_type = CLICKHOUSE_NULLABLE_EXACT_INTEGER_TYPE if may_be_null else CLICKHOUSE_EXACT_INTEGER_TYPE
        return Function("accurateCast", term, LiteralValue(exact_type))

    def get_checked_integer_result(self, term: Term, minimum: int, maximum: int, type_name: str) -> Term:
        # The result of widened operands, refused out of the type's range by a check the value
        # carries. Local import: hare.sql's terms render through the dialect.
        from hare.sql.terms.functions.function import Function
        from hare.sql.terms.values.literal_value import LiteralValue
        from hare.sql.terms.values.value_wrapper import ValueWrapper

        # A result of other checked results is written once - the check below writes it three
        # times, each level of arithmetic tripling the SQL.
        nodes: list[Any] = list(term.nodes_())
        if any(
            isinstance(node, ClickhouseCheckedIntegerResult) or (isinstance(node, Function) and node.name == "throwIf")
            for node in nodes
        ):
            return ClickhouseCheckedIntegerResult(term, minimum, maximum, type_name)
        out_of_range = (term < LiteralValue(str(minimum))) | (term > LiteralValue(str(maximum)))
        refusal = Function(
            "throwIf",
            Function("ifNull", out_of_range, ValueWrapper(0, allow_parametrize=False)),
            ValueWrapper(f"{type_name} out of range", allow_parametrize=False),
        )
        return term + refusal

    def get_float_operand(self, term: Term) -> Term:
        # ClickHouse has no arithmetic of an Int128/Int256 - a checked integer result - and a
        # Float64. Local import: hare.sql's terms render through the dialect.
        from hare.sql.terms.functions.function import Function

        return Function("toFloat64", term)

    def get_decimal_dividend(self, term: Term) -> Term:
        # A Decimal quotient has the dividend's scale on ClickHouse - the dividend is widened, so that
        # the quotient isn't cut. Local import: hare.sql's terms render through the dialect.
        from hare.sql.terms.functions.function import Function

        return Function("toDecimal256", term, DECIMAL_QUOTIENT_SCALE)

    def get_decimal_operation_term(
        self, operator_name: str, left: Term, right: Term, scales: list[int | None]
    ) -> Term | None:
        # A Decimal128 sum or product wraps around past its 38 digits without an error, and a Decimal
        # beside an integer takes the Decimal's own narrow type - each operand is widened to a
        # Decimal256 of its scale first. A quotient widens its dividend (get_decimal_dividend()).
        # Local import: hare.sql's terms render through the dialect.
        from hare.sql.terms.functions.function import Function
        from hare.sql.terms.term import Term as SqlTerm
        from hare.sql.terms.values.literal_value import LiteralValue
        from hare.sql.terms.values.value_wrapper import ValueWrapper

        if operator_name == "truediv" or None in scales:
            return None
        left_term, right_term = (
            Function("toDecimal256", operand, ValueWrapper(scale, allow_parametrize=False))
            for operand, scale in zip((left, right), scales, strict=True)
        )
        result = cast("Term", getattr(SqlTerm, f"__{operator_name}__")(left_term, right_term))
        # A Decimal256 wraps around past its digits too - a result whose magnitude reaches them is
        # refused, the magnitude worked out from the operands as doubles.
        left_magnitude, right_magnitude = (Function("abs", Function("toFloat64", term)) for term in (left, right))
        is_product = operator_name == "mul"
        magnitude = left_magnitude * right_magnitude if is_product else left_magnitude + right_magnitude
        result_scale = sum(cast("list[int]", scales)) if is_product else max(cast("list[int]", scales))
        limit = LiteralValue(f"1e{CLICKHOUSE_WIDE_DECIMAL_DIGITS - result_scale}")
        refusal = Function(
            "throwIf",
            Function("ifNull", magnitude >= limit, ValueWrapper(0, allow_parametrize=False)),
            ValueWrapper(CLICKHOUSE_NUMERIC_OVERFLOW_MESSAGE, allow_parametrize=False),
        )
        return cast("Term", result + refusal)

    def get_decimal_aggregate_argument(self, term: Term, scale: int) -> Term:
        # Summed as Decimal256 - a Decimal128 sum wraps around past 38 digits.
        # Local import: hare.sql's terms render through the dialect.
        from hare.sql.terms.functions.function import Function
        from hare.sql.terms.values.value_wrapper import ValueWrapper

        return Function("toDecimal256", term, ValueWrapper(scale, allow_parametrize=False))

    def get_decimal_aggregate_name(self, function_name: str) -> str | None:
        return CLICKHOUSE_DECIMAL_AGGREGATE_NAMES.get(function_name)

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
