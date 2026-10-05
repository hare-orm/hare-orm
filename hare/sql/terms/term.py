from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from hare.sql.builder_methods import BuilderMethods
from hare.sql.enums import Arithmetic, Equality, Matching
from hare.sql.sql_context import DEFAULT_SQL_CONTEXT, SqlContext
from hare.sql.terms.node import TNode

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.builder.queries.query_builder import QueryBuilder
    from hare.sql.builder.tables.table import Table
    from hare.sql.terms.json import JSON
    from hare.sql.terms.negative import Negative
    from hare.sql.terms.values.literal_value import LiteralValue
    from hare.sql.terms.values.null_value import NullValue
    from hare.sql.terms.values.value_wrapper import ValueWrapper
from hare.sql.terms.node import Node


class Term(Node):
    is_aggregate: bool | None = False
    #: Whether the term writes its subquery without correlation itself where the database has no
    #: correlated subqueries (``Features.supports_correlated_subqueries``) - an ``EXISTS``.
    rewrites_own_correlation = False
    #: Whether the term is a query built apart from the statement it is written into - its query is
    #: ``get_nested_query()``.
    holds_nested_query = False
    #: Whether the term's value is a ``sensitive=True`` field's - its parameter is never shown
    #: (``QueryParameters``).
    sensitive = False

    @staticmethod
    def get_combined_is_aggregate(values: list[bool | None]) -> bool | None:
        """Returns the ``is_aggregate`` of an expression made of several terms: each term is an
        aggregate (True), is not (False) or fits either way (None, a constant).

        Args:
            values: The terms' ``is_aggregate`` values.

        Returns:
            True when every term that isn't None is an aggregate, False when one of them isn't,
            None when all of them are None.
        """
        decided = [value for value in values if value is not None]
        if decided:
            return all(decided)
        return None

    def __init__(self, alias: str | None = None) -> None:
        self.alias = alias

    def __copy__(self) -> Self:
        # A shallow __dict__ copy without copy.copy()'s generic path - a hot path.
        new_term = type(self).__new__(type(self))
        new_term.__dict__.update(self.__dict__)
        return new_term

    @BuilderMethods.builder
    def as_(self, alias: str) -> Self:
        self.alias = alias
        return self

    @property
    def tables_(self) -> set[Table]:
        from hare.sql import Table

        return set(self.find_(Table))

    def fields_(self) -> set[field_terms.Field]:
        return set(self.find_(field_terms.Field))

    @staticmethod
    def wrap_constant(
        value: Any, wrapper_class: type[Term] | None = None
    ) -> TNode | LiteralValue | array_terms.Array | tuple_terms.Tuple | ValueWrapper:
        """Wraps raw inputs such as numbers so they can be used in Criterions and Operator.

        For example, the expression F('abc')+1 stores the integer part in a ValueWrapper object.

        Args:
            value: Any value.
            wrapper_class: A hare.sql class which wraps a constant value so it can be handled as
                a component of the query.

        Returns:
            Raw string, number, or decimal values are returned in a ValueWrapper. Fields and
            other parts of the querybuilder are returned as inputted.
        """
        if isinstance(value, Node):
            return cast("TNode", value)
        if value is None:
            return null_value_terms.NullValue()
        if isinstance(value, list):
            return array_terms.Array(*value)
        if isinstance(value, tuple):
            return tuple_terms.Tuple(*value)

        # Need to default here to avoid the recursion. ValueWrapper extends this class.
        wrapper_class = wrapper_class or value_wrapper_terms.ValueWrapper
        return wrapper_class(value)  # type:ignore[return-value]

    @staticmethod
    def wrap_json(
        value: Term | QueryBuilder | str | int | bool | dict[str, Any] | list[Any] | None,
        wrapper_class: type[ValueWrapper] | None = None,
    ) -> Term | QueryBuilder | NullValue | ValueWrapper | JSON:
        # Imported here: the modules import each other.
        from hare.sql.builder.queries.query_builder import QueryBuilder
        from hare.sql.terms.json import JSON
        from hare.sql.terms.values.null_value import NullValue
        from hare.sql.terms.values.value_wrapper import ValueWrapper

        if isinstance(value, (Term, QueryBuilder)):
            return value
        if value is None:
            return NullValue()
        if isinstance(value, (str, int, bool)):
            wrapper_class = wrapper_class or ValueWrapper
            return wrapper_class(value)

        return JSON(value)

    def replace_table(self, current_table: Table | None, new_table: Table | None) -> Self:
        """Replaces all occurrences of the specified table with the new table.

        Useful when reusing fields across queries. The base implementation returns self because
        not all terms have a table property.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            Self.
        """
        return self

    def eq(self, other: Any) -> criteria_terms.BasicCriterion:
        return self == other

    def isnull(self) -> criteria_terms.NullCriterion:

        return criteria_terms.NullCriterion(self)

    def notnull(self) -> criteria_terms.Not:
        return self.isnull().negate()

    def is_not_true(self) -> criteria_terms.IsNotTrueCriterion:

        return criteria_terms.IsNotTrueCriterion(self)

    def is_distinct_from(self, other: Any) -> criteria_terms.IsDistinctFromCriterion:

        return criteria_terms.IsDistinctFromCriterion(self, self.wrap_constant(other))

    def gt(self, other: Any) -> criteria_terms.BasicCriterion:
        return self > other

    def gte(self, other: Any) -> criteria_terms.BasicCriterion:
        return self >= other

    def lt(self, other: Any) -> criteria_terms.BasicCriterion:
        return self < other

    def lte(self, other: Any) -> criteria_terms.BasicCriterion:
        return self <= other

    def ne(self, other: Any) -> criteria_terms.BasicCriterion:
        return self != other

    def like(self, expression: str) -> criteria_terms.BasicCriterion:

        return criteria_terms.BasicCriterion(Matching.LIKE, self, self.wrap_constant(expression))

    def regex(self, pattern: str) -> criteria_terms.BasicCriterion:

        return criteria_terms.BasicCriterion(Matching.REGEX, self, self.wrap_constant(pattern))

    def between(self, lower: Any, upper: Any) -> criteria_terms.BetweenCriterion:

        return criteria_terms.BetweenCriterion(self, self.wrap_constant(lower), self.wrap_constant(upper))

    def isin(self, arg: list[Any] | tuple[Any, ...] | set[Any] | Term) -> criteria_terms.ContainsCriterion:

        if isinstance(arg, (list, tuple, set)):
            return criteria_terms.ContainsCriterion(self, tuple_terms.Tuple(*arg))
        return criteria_terms.ContainsCriterion(self, arg)

    def notin(self, arg: list[Any] | tuple[Any, ...] | set[Any] | Term) -> criteria_terms.ContainsCriterion:
        return self.isin(arg).negate()

    def negate(self) -> criteria_terms.Not:

        return criteria_terms.Not(self)

    def __invert__(self) -> criteria_terms.Not:

        return criteria_terms.Not(self)

    def __pos__(self) -> Self:
        return self

    def __neg__(self) -> Negative:
        return negative_terms.Negative(self)

    def __add__(self, other: Any) -> arithmetic_terms.ArithmeticExpression:

        return arithmetic_terms.ArithmeticExpression(Arithmetic.ADD, self, self.wrap_constant(other))

    def __sub__(self, other: Any) -> arithmetic_terms.ArithmeticExpression:

        return arithmetic_terms.ArithmeticExpression(Arithmetic.SUB, self, self.wrap_constant(other))

    def __mul__(self, other: Any) -> arithmetic_terms.ArithmeticExpression:

        return arithmetic_terms.ArithmeticExpression(Arithmetic.MUL, self, self.wrap_constant(other))

    def __truediv__(self, other: Any) -> arithmetic_terms.ArithmeticExpression:

        return arithmetic_terms.ArithmeticExpression(Arithmetic.DIV, self, self.wrap_constant(other))

    def __pow__(self, other: Any) -> function_terms.Pow:
        return function_terms.Pow(self, other)

    def __mod__(self, other: Any) -> function_terms.Mod:

        return function_terms.Mod(self, other)

    def __radd__(self, other: Any) -> arithmetic_terms.ArithmeticExpression:

        return arithmetic_terms.ArithmeticExpression(Arithmetic.ADD, self.wrap_constant(other), self)

    def __rsub__(self, other: Any) -> arithmetic_terms.ArithmeticExpression:

        return arithmetic_terms.ArithmeticExpression(Arithmetic.SUB, self.wrap_constant(other), self)

    def __rmul__(self, other: Any) -> arithmetic_terms.ArithmeticExpression:

        return arithmetic_terms.ArithmeticExpression(Arithmetic.MUL, self.wrap_constant(other), self)

    def __rtruediv__(self, other: Any) -> arithmetic_terms.ArithmeticExpression:

        return arithmetic_terms.ArithmeticExpression(Arithmetic.DIV, self.wrap_constant(other), self)

    def __eq__(self, other: Any) -> criteria_terms.BasicCriterion | criteria_terms.NullCriterion:  # type:ignore[override]
        # `field = NULL`/`field <> NULL` are always NULL (never TRUE) in SQL - a criterion built
        # that way matches nothing, silently. `field == None`/`field != None` must render as
        # IS NULL/IS NOT NULL instead.

        if other is None:
            return self.isnull()
        return criteria_terms.BasicCriterion(Equality.EQ, self, self.wrap_constant(other))

    def __ne__(self, other: Any) -> criteria_terms.BasicCriterion | criteria_terms.Not:  # type:ignore[override]

        if other is None:
            return self.isnull().negate()
        return criteria_terms.BasicCriterion(Equality.NE, self, self.wrap_constant(other))

    def __gt__(self, other: Any) -> criteria_terms.BasicCriterion:

        return criteria_terms.BasicCriterion(Equality.GT, self, self.wrap_constant(other))

    def __ge__(self, other: Any) -> criteria_terms.BasicCriterion:

        return criteria_terms.BasicCriterion(Equality.GTE, self, self.wrap_constant(other))

    def __lt__(self, other: Any) -> criteria_terms.BasicCriterion:

        return criteria_terms.BasicCriterion(Equality.LT, self, self.wrap_constant(other))

    def __le__(self, other: Any) -> criteria_terms.BasicCriterion:

        return criteria_terms.BasicCriterion(Equality.LTE, self, self.wrap_constant(other))

    def __getitem__(self, item: slice) -> criteria_terms.BetweenCriterion:
        if not isinstance(item, slice):
            raise TypeError("Field' object is not subscriptable")
        return self.between(item.start, item.stop)

    def __str__(self) -> str:
        return self.get_sql(DEFAULT_SQL_CONTEXT)

    def __hash__(self) -> int:
        # Term.__eq__ builds a criterion, so the hash is of the rendered SQL - cached, as a builder
        # returns copies and never changes an instance.
        cached = self.__dict__.get("_hash_cache")
        if cached is not None:
            return cached
        sql_context = DEFAULT_SQL_CONTEXT.copy(with_alias=True)
        computed = hash(self.get_sql(sql_context))
        self.__dict__["_hash_cache"] = computed
        return computed

    def get_sql(self, sql_context: SqlContext) -> str:
        raise NotImplementedError()


# After Term is defined: these packages build on it, and Term's operators build their
# terms - read off the modules when an operator runs.
from hare.sql.terms import (  # noqa: E402
    arithmetic_expression as arithmetic_terms,
    array as array_terms,
    criteria as criteria_terms,
    field as field_terms,
    functions as function_terms,
    negative as negative_terms,
    tuple as tuple_terms,
)
from hare.sql.terms.values import null_value as null_value_terms, value_wrapper as value_wrapper_terms  # noqa: E402
