from __future__ import annotations

import datetime
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.caching.cache import Cache
from hare.core.registries import Registries

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.sql.sql_context import SqlContext
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

    def get_never_null_column_count_argument(self, term: Term) -> Term:
        """What ``COUNT`` takes for a column of the queried table that holds no NULL - every row
        has it, so a dialect may count the rows instead of reading the column.

        Args:
            term: The column.

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

    def get_connection_only_function(self, sql: str) -> str | None:
        """A function in ``sql`` that exists only on hare's own connections - none can be part of
        DDL, which the database runs on its own.

        Args:
            sql: The SQL text.

        Returns:
            The function's name, None when there's none.
        """
        return None

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

    def get_ordering_term(self, term: Term, sql_context: SqlContext) -> Term:
        """What ``ORDER BY`` sorts by for a term.

        Args:
            term: The ordered term.
            sql_context: The context the clause renders in.

        Returns:
            The term itself by default.
        """
        return term

    def get_assigned_decimal_term(self, term: Term, max_digits: int, decimal_places: int) -> Term:
        """What an UPDATE sets a decimal column to for an expression.

        Args:
            term: The expression.
            max_digits: The column's ``max_digits``.
            decimal_places: The column's ``decimal_places``.

        Returns:
            The term itself by default - the column type rounds it to its scale.
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

    def get_datetime_part_comparand(self, value: datetime.date | datetime.time) -> Any:
        """What a datetime's date or time of day (``created__date=``, ``created__time__lt=``) is
        compared with.

        Args:
            value: The date, or the naive time of day.

        Returns:
            The value itself by default.
        """
        return value
