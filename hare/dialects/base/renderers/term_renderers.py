from __future__ import annotations

import datetime
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.caching.cache import Cache
from hare.core.registries import Registries

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.fields.field import Field
    from hare.sql.enums import Order
    from hare.sql.sql_context import SqlContext
    from hare.sql.terms.functions.analytic_function import AnalyticFunction
    from hare.sql.terms.term import Term

    #: ``(term, sql_context) -> sql``.
    TermRenderer = Callable[[Any, SqlContext], str]
    #: ``(function, sql_context) -> name`` - an empty name keeps the function's own.
    FunctionNameRenderer = Callable[[Any, SqlContext], str]


class TermRenderers:
    """How one dialect writes the SQL of expressions: the renderers of the terms whose SQL differs
    between dialects, found through a term's class hierarchy - a term class of its own uses the
    renderer of its nearest registered base class, a term with no renderer renders its own standard
    SQL - and the terms the dialect puts in place of an expression where its SQL needs another.

    Attributes:
        is_distinct_from_operator: The NULL-safe inequality operator.
    """

    is_distinct_from_operator: str = "IS DISTINCT FROM"

    #: The renderer and the name renderer each set of renderers found for a term class through the
    #: class's bases, kept in a bucket of the set.
    found_renderers: ClassVar[Cache[Any]] = Cache(holds_sql=False, keyed_by_model=False)
    found_name_renderers: ClassVar[Cache[Any]] = Cache(holds_sql=False, keyed_by_model=False)

    def __init__(self, dialect: Dialect) -> None:
        """
        Args:
            dialect: The dialect whose terms are rendered.
        """
        self.dialect = dialect
        #: The buckets of the caches these renderers read (``Cache.get_owner_bucket()``).
        self.cache_buckets: dict[int, Any] = {}
        self.renderers: dict[type, TermRenderer] = {}
        self.name_renderers: dict[type, FunctionNameRenderer] = {}
        self.renderers_by_term_class: dict[type, TermRenderer | None] = TermRenderers.found_renderers.get_owner_bucket(
            self
        )
        self.name_renderers_by_term_class: dict[type, FunctionNameRenderer | None] = (
            TermRenderers.found_name_renderers.get_owner_bucket(self)
        )
        self.function_renderers: dict[str, TermRenderer] = {}

    def register(self, term_class: type, renderer: TermRenderer) -> None:
        """Sets how the dialect renders ``term_class`` and its subclasses.

        Args:
            term_class: The term class.
            renderer: A ``(term, sql_context) -> sql``.
        """
        self.renderers[term_class] = renderer
        TermRenderers.found_renderers.forget_owner(self)
        Registries.changed()

    def register_function(self, function_name: str, renderer: TermRenderer) -> None:
        """Sets how the dialect renders every function called ``function_name`` that has no
        renderer of its own class.

        Args:
            function_name: The SQL function's name, as hare writes it (``"LENGTH"``).
            renderer: A ``(function, sql_context) -> sql``.
        """
        self.function_renderers[function_name] = renderer
        Registries.changed()

    def get_function_renderer(self, function_name: str) -> TermRenderer | None:
        """The renderer of the functions called ``function_name``, None for none."""
        return self.function_renderers.get(function_name)

    def register_name(self, function_class: type, name_renderer: FunctionNameRenderer) -> None:
        """Sets the name the dialect calls ``function_class`` and its subclasses by.

        Args:
            function_class: The function class.
            name_renderer: A ``(function, sql_context) -> name``; an empty name keeps the function's own.
        """
        self.name_renderers[function_class] = name_renderer
        TermRenderers.found_name_renderers.forget_owner(self)
        Registries.changed()

    def get(self, term_class: type) -> TermRenderer | None:
        """The renderer of ``term_class``, None when the term renders its own SQL."""
        if term_class not in self.renderers_by_term_class:
            self.renderers_by_term_class[term_class] = next(
                (self.renderers[base] for base in term_class.__mro__ if base in self.renderers), None
            )
        return self.renderers_by_term_class[term_class]

    def get_name_renderer(self, function_class: type) -> FunctionNameRenderer | None:
        """The name renderer of ``function_class``, None when the function keeps its own name."""
        if function_class not in self.name_renderers_by_term_class:
            self.name_renderers_by_term_class[function_class] = next(
                (self.name_renderers[base] for base in function_class.__mro__ if base in self.name_renderers), None
            )
        return self.name_renderers_by_term_class[function_class]

    def get_distinct_from_sql(self, left_sql: str, right_sql: str) -> str:
        """A NULL-safe inequality - true when the two differ, one NULL included, false when both are
        NULL; never NULL.

        Args:
            left_sql: The left value's SQL.
            right_sql: The right value's SQL.

        Returns:
            ``left <is_distinct_from_operator> right`` by default.
        """
        return f"{left_sql} {self.is_distinct_from_operator} {right_sql}"

    def get_concatenated_argument_sql(self, argument_sql: str, argument: Any) -> str:
        """An argument of a text concatenation, as the database takes it.

        Args:
            argument_sql: The argument's SQL.
            argument: The argument's term.

        Returns:
            The SQL - unchanged by default; a database that types a parameter only from what's
            around it casts the argument.
        """
        return argument_sql

    def get_json_path_comparand(
        self, value: Any, encode_json_text: Callable[[Any], str], column_type: str | None, *, as_parameter: bool
    ) -> Any:
        """A value compared with the JSON value at a path.

        Args:
            value: A JSON-compatible Python value; ``None`` is the JSON ``null``.
            encode_json_text: Gives a value's JSON text, checking it.
            column_type: The SQL type of the JSON column.
            as_parameter: The value is an item of a list bound as one array parameter.

        Returns:
            The JSON text cast to the JSON type by default - the JSON text alone for an item of an
            array parameter.
        """
        # Local import: hare.sql renders through the dialect.
        from hare.sql.functions.cast import Cast
        from hare.sql.terms.values.value_wrapper import ValueWrapper

        json_text = encode_json_text(value)
        if as_parameter or column_type is None:
            return json_text
        return Cast(ValueWrapper(json_text), column_type)

    def get_integer_aggregate_as_float(self, term: Term) -> Term:
        """The average or a statistic of integers as a float - the term itself by default.

        Args:
            term: The aggregate.

        Returns:
            The term the float is read from.
        """
        return term

    def get_never_null_column_count_argument(self, term: Term, *, is_filtered: bool) -> Term:
        """What ``COUNT`` takes for a column of the queried table that holds no NULL - every row
        has it, so a dialect may count the rows instead of reading the column.

        Args:
            term: The column.
            is_filtered: Whether the count has a condition of its own (``_filter=``).

        Returns:
            The column itself by default.
        """
        return term

    def get_composite_distinct_key(self, terms: Sequence[Term]) -> Term:
        """The value ``COUNT(DISTINCT ...)`` counts a key of several columns by.

        Args:
            terms: The key's columns.

        Returns:
            A row value of them.
        """
        # Local import: hare.sql's terms render through the dialect.
        from hare.sql.terms.tuple import Tuple

        return Tuple(*terms)

    def get_decimal_compared_term(self, term: Term, *, only_decimals: bool) -> Term:
        """What an annotation compared with Decimal values is compared as - an aggregate or
        expression result carries no column type or collation of its own.

        Args:
            term: The annotation's term.
            only_decimals: Whether every compared value is a Decimal.

        Returns:
            The term itself by default - a decimal is a number to the database.
        """
        return term

    def get_decimal_value_term(self, term: Any) -> Any:
        """A Decimal literal, or a DecimalField column, where it meets other numbers - a CASE or
        COALESCE result.

        Args:
            term: A resolved term, or a raw Python value not wrapped into a term yet.

        Returns:
            The term itself by default.
        """
        return term

    def get_result_branch_term(self, term: Term, output_field: Field[Any] | None) -> Term:
        """A branch of a CASE or a COALESCE as the type of the whole result needs it.

        Args:
            term: The branch's term.
            output_field: The field the result is decoded through, None when unknown.

        Returns:
            The term itself by default.
        """
        return term

    def get_result_term(self, term: Term, output_field: Field[Any] | None) -> Term:
        """A whole CASE or COALESCE as the type of its result needs it.

        Args:
            term: The CASE or COALESCE.
            output_field: The field the result is decoded through, None when unknown.

        Returns:
            The term itself by default.
        """
        return term

    def get_order_sql(self, order: Order) -> str:
        """The SQL of a sort direction.

        Args:
            order: The direction.

        Returns:
            Its own text by default.
        """
        return order.value

    def get_ordering_clause_sql(self, term: Term, term_sql: str, order: Order | None, sql_context: SqlContext) -> str:
        """One key of an ``ORDER BY``.

        Args:
            term: The ordered term.
            term_sql: Its SQL - an alias when it is ordered by one.
            order: The direction, None for the database's own.
            sql_context: The rendering state of the statement.

        Returns:
            The term and its direction by default.
        """
        return term_sql if order is None else f"{term_sql} {self.get_order_sql(order)}"

    def get_window_ordering_clause_sql(
        self, function: AnalyticFunction, term: Term, term_sql: str, order: Order | None, sql_context: SqlContext
    ) -> str:
        """One key of a window's ``ORDER BY``.

        Args:
            function: The window function.
            term: The ordered term.
            term_sql: Its SQL.
            order: The direction, None for the default one.
            sql_context: The rendering state of the statement.

        Returns:
            ``get_window_ordering_sql()`` by default.
        """
        return self.get_window_ordering_sql(function, term_sql, order)

    def get_window_ordering_sql(self, function: AnalyticFunction, term_sql: str, order: Order | None) -> str:
        """The SQL of one ordering of a window.

        Args:
            function: The window function.
            term_sql: The SQL of the ordered term.
            order: The direction, None for the default one.

        Returns:
            The term and its direction by default.
        """
        return term_sql if order is None else f"{term_sql} {self.get_order_sql(order)}"

    def get_value_range_ordering_term(self, term: Term) -> Term:
        """What a window orders by for a term when its frame is a ``RANGE`` with an edge a number of
        values away from the current row's.

        Args:
            term: The ordered term.

        Returns:
            The term itself by default - for a database adding the offset to the term's value.
        """
        return term

    def get_ordering_term(self, term: Term, sql_context: SqlContext) -> Term:
        """What ``ORDER BY`` sorts by for a term.

        Args:
            term: The ordered term.
            sql_context: The context the clause renders in.

        Returns:
            The term itself by default.
        """
        return term

    def get_assigned_decimal_term(
        self, term: Term, max_digits: int, decimal_places: int, value_scale: int | None
    ) -> Term:
        """What an UPDATE sets a decimal column to for an expression.

        Args:
            term: The expression.
            max_digits: The column's ``max_digits``.
            decimal_places: The column's ``decimal_places``.
            value_scale: The places of a Decimal expression's result; None for any other one.

        Returns:
            The term itself by default - the column type rounds it to its scale.
        """
        return term

    def get_assigned_enum_term(self, term: Term, member_numbers: list[int]) -> Term:
        """What an UPDATE sets an integer enum column to for an expression.

        Args:
            term: The expression.
            member_numbers: The numbers of the enum's members.

        Returns:
            The term itself by default - the column holds any number, the field refuses one of no
            member when it reads it.
        """
        return term

    def get_enum_number_term(self, term: Term) -> Term:
        """The column of an integer enum field where its number is computed with - an operand of
        arithmetic, an argument of a function of numbers.

        Args:
            term: The column.

        Returns:
            The term itself by default - for a database keeping the members as integers.
        """
        return term

    def get_widened_integer_sum_argument(self, term: Term) -> Term:
        """The integer argument of a ``SUM`` or ``AVG`` - summed exactly, into a type wider than the
        integers.

        Args:
            term: The argument.

        Returns:
            The term itself by default - for a database summing integers exactly, or refusing an
            overflow.
        """
        return term

    def get_exact_integer_operand(self, term: Term, may_be_null: bool) -> Term:
        """An operand of an integer sum, difference or product a query reads - widened where the
        database's arithmetic wraps around, so that the range check of the result
        (``get_checked_integer_result()``) sees the exact result.

        Args:
            term: The operand.
            may_be_null: Whether the operand can be NULL.

        Returns:
            The term itself by default - for a database refusing an overflow itself, or keeping
            the exact result.
        """
        return term

    def get_checked_integer_result(self, term: Term, minimum: int, maximum: int, type_name: str) -> Term:
        """An integer result of arithmetic read, refused out of the range given - past 64 bits, which
        a database turns into a float or wraps around.

        Args:
            term: The result, of exact operands (``get_exact_integer_operand()``).
            minimum: The smallest value of the type.
            maximum: The largest value of the type.
            type_name: The type, for the message.

        Returns:
            The term itself by default - for a database refusing an overflow itself.
        """
        return term

    def get_decimal_operation_term(
        self, operator_name: str, left: Term, right: Term, scales: list[int | None]
    ) -> Term | None:
        """A sum, difference, product or quotient of Decimals the dialect works out itself.

        Args:
            operator_name: The arithmetic operator's name - ``add``, ``sub``, ``mul`` or ``truediv``.
            left: The left operand.
            right: The right operand, a divisor checked already.
            scales: Each operand's scale - 0 of an integer, None where unknown.

        Returns:
            None by default - the database's own arithmetic, exact for its decimals.
        """
        return None

    def get_decimal_aggregate_argument(self, term: Term, scale: int) -> Term:
        """The Decimal values a ``SUM`` or an ``AVG`` adds up.

        Args:
            term: The values.
            scale: Their scale.

        Returns:
            The term itself by default - for a database adding up its decimals exactly.
        """
        return term

    def get_decimal_aggregate_name(self, function_name: str) -> str | None:
        """The aggregate summing or averaging Decimals in place of the database's own.

        Args:
            function_name: The SQL aggregate's name - ``SUM`` or ``AVG``.

        Returns:
            None by default - the database's own aggregate, exact for its decimals.
        """
        return None

    def get_float_operand(self, term: Term) -> Term:
        """The integer operand of an arithmetic with a float.

        Args:
            term: The integer operand.

        Returns:
            The term itself by default.
        """
        return term

    def get_decimal_dividend(self, term: Term) -> Term:
        """The dividend of a division of Decimals.

        Args:
            term: The dividend.

        Returns:
            The term itself by default.
        """
        return term

    def get_duration_quotient(self, dividend: Term, divisor: Term) -> Term:
        """The microseconds of a duration divided by an integer - rounded to the nearest, a half to
        the even one, as Python divides a timedelta.

        Args:
            dividend: The microseconds.
            divisor: The divisor, checked already.

        Returns:
            The quotient - the integer one, moved away from zero when its remainder is past a half.
        """
        # Local imports: hare.sql's terms render through the dialect.
        from hare.sql.terms.case.case import Case
        from hare.sql.terms.functions.function import Function
        from hare.sql.terms.functions.mod import Mod

        # The dividend less its remainder divides exactly - an aggregate of integers may be a decimal.
        signed_remainder = Mod(dividend, divisor, integer=True)
        quotient = (dividend - signed_remainder) / divisor
        remainder = Function("ABS", signed_remainder)
        rest = Function("ABS", divisor) - remainder
        rounds_away = (remainder > rest) | ((remainder == rest) & (Mod(quotient, 2, integer=True) != 0))
        step = Case().when(((dividend < 0) & (divisor > 0)) | ((dividend > 0) & (divisor < 0)), -1).else_(1)
        return Case().when(rounds_away, quotient + step).else_(quotient)

    def get_datetime_part_comparand(self, value: datetime.date | datetime.time) -> Any:
        """What a datetime's date or time of day (``created__date=``, ``created__time__lt=``) is
        compared with.

        Args:
            value: The date, or the naive time of day.

        Returns:
            The value itself by default.
        """
        return value
