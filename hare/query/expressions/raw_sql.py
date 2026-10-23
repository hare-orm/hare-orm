from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, ClassVar

from hare.dialects.enums import ParameterPosition
from hare.exceptions import (
    QueryError,
    UnSupportedError,
)
from hare.query.expressions.arithmetic.arithmetic_operators import ArithmeticOperators
from hare.query.expressions.arithmetic.combined_expression import CombinedExpression
from hare.query.expressions.constants import RAW_SQL_TOKEN_RE
from hare.query.expressions.enums import ArithmeticOperator
from hare.query.expressions.term_expression import TermExpression
from hare.query.expressions.value import Value
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.description.plannable import Plannable
from hare.query.plans.enums import PlanPartType
from hare.sql import SqlContext
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from hare.sql.terms.node import TNode


class RawSQL(ArithmeticOperators, Plannable, Term):  # type: ignore[misc]
    """A literal SQL fragment put into the query as written (``.annotate(idp=RawSQL("id + %s", [1]))``,
    ``.filter(id__in=RawSQL('SELECT ...'))``).

    A value that varies per call belongs in ``params``, never interpolated into ``sql`` - that is an
    SQL injection hole. ``params`` are bound at the ``%s`` placeholders, left to right. A literal
    ``%`` is written ``%%`` (``LIKE '%%stuff%%'``).
    """

    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("sql", PlanPartType.KEY),
        ("parameters", PlanPartType.PARAMETERS),
        # The name a query selects it under - its annotation's key.
        ("alias", PlanPartType.NONE),
    )

    def __init__(self, sql: str, parameters: Sequence[Any] = ()) -> None:
        super().__init__()
        if not isinstance(sql, str):
            raise QueryError(f"RawSQL takes the SQL as a str, got {sql!r}")
        if not sql.strip():
            raise QueryError("RawSQL takes SQL, got an empty string")
        if not isinstance(parameters, (list, tuple)):
            raise QueryError(f"RawSQL takes its parameters as a list, got {parameters!r}")
        placeholder_count = sum(1 for token in RAW_SQL_TOKEN_RE.findall(sql) if token == "%s")  # nosec B105
        if placeholder_count != len(parameters):
            raise QueryError(
                f"RawSQL: sql has {placeholder_count} '%s' placeholder(s) but {len(parameters)} "
                f"param(s) were given - counts must match (a literal '%' that isn't a "
                f"placeholder must be written as '%%')"
            )
        for parameter in parameters:
            if isinstance(parameter, (dict, set, frozenset)):
                raise QueryError(
                    f"RawSQL: a {type(parameter).__name__} can't be a parameter - pass a list for an array "
                    "parameter, or json.dumps(...) of it for JSON"
                )
            if isinstance(parameter, (list, tuple)):
                Value.raise_if_nested_too_deeply(parameter, "RawSQL")
        self.sql = sql
        self.parameters = [ValueWrapper(self.get_parameter_value(parameter)) for parameter in parameters]

    @staticmethod
    def get_parameter_value(parameter: Any) -> Any:
        """The value a parameter binds as - a tuple is an array parameter like a list, one shape
        every driver binds.

        Args:
            parameter: The parameter given.

        Returns:
            The value.
        """
        return list(parameter) if isinstance(parameter, tuple) else parameter

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        for parameter in self.parameters:
            yield from parameter.nodes_()

    def _combine(self, other: Any, connector: ArithmeticOperator, right_hand: bool) -> CombinedExpression:
        """Builds an arithmetic expression with this SQL fragment embedded as is.

        Args:
            other: The other operand.
            connector: The arithmetic operator.
            right_hand: Whether this fragment is the right operand.

        Returns:
            The expression.
        """
        return TermExpression(self)._combine(other, connector, right_hand)

    @staticmethod
    def _get_parameter_sql(parameter: ValueWrapper, sql_context: SqlContext) -> str:
        """One parameter's SQL - explicitly cast, where the dialect casts one, for a Python number,
        whose type a bare bind parameter doesn't tell.

        Args:
            parameter: The parameter.
            sql_context: The SQL rendering context.

        Returns:
            The parameter's SQL.

        Raises:
            UnSupportedError: The parameter is a list and the database has no array parameters.
            QueryError: Written into the SQL, the parameter is of a type no literal is written for.
        """
        value = parameter.value
        if isinstance(value, list):
            if not sql_context.dialect.features.binds_array_parameters:
                raise UnSupportedError(
                    f"RawSQL: a list parameter is bound as an array, which the {sql_context.dialect.name} database "
                    "has no parameter for"
                )
            if sql_context.parameterizer is None:
                return RawSQL._get_array_literal_sql(value, sql_context)
        try:
            return Value.get_typed_term(parameter, value, ParameterPosition.RAW_SQL, sql_context.dialect).get_sql(
                sql_context
            )
        except TypeError as error:
            raise QueryError(f"RawSQL: {error}") from None

    @staticmethod
    def _get_array_literal_sql(value: list[Any] | tuple[Any, ...], sql_context: SqlContext) -> str:
        """A list parameter written into the SQL - the database's array literal, as it binds one.

        Args:
            value: The list; a list in it is a dimension of the array.
            sql_context: The SQL rendering context.

        Returns:
            The literal.
        """
        return sql_context.dialect.literals.get_array_literal_sql(
            [
                RawSQL._get_array_literal_sql(item, sql_context)
                if isinstance(item, list | tuple)
                else ValueWrapper.get_formatted_value(item, sql_context)
                for item in value
            ]
        )

    def get_sql(self, sql_context: SqlContext) -> str:
        parameter_iter = iter(self.parameters)

        def substitute(match: re.Match[str]) -> str:
            if match.group(0) == "%%":
                return "%"
            return self._get_parameter_sql(next(parameter_iter), sql_context)

        rendered_sql = RAW_SQL_TOKEN_RE.sub(substitute, self.sql)
        # Parenthesized as a subquery operand (`field IN (...)`), like a query builder.
        sql = f"({rendered_sql})" if sql_context.subquery else rendered_sql
        if sql_context.with_alias:
            return sql_context.format_alias_sql(sql, self.alias)
        return sql
