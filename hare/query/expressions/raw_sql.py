from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from hare.dialects.enums import ParameterPosition
from hare.exceptions import (
    QueryError,
    UnSupportedError,
)
from hare.query.expressions.base.arithmetic_operators_mixin import ArithmeticOperatorsMixin
from hare.query.expressions.base.combined_expression import CombinedExpression
from hare.query.expressions.base.term_expression import TermExpression
from hare.query.expressions.base.value import Value
from hare.query.expressions.constants import RAW_SQL_TOKEN_RE
from hare.query.expressions.enums import ArithmeticOperator
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.description.plannable import Plannable
from hare.sql import SqlContext
from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from collections.abc import Iterator, Sequence

    from hare.sql.terms.base.node import TNode


class RawSQL(ArithmeticOperatorsMixin, Plannable, Term):  # type: ignore[misc]
    """A literal SQL fragment put into the query as written (``.annotate(idp=RawSQL("id + %s", [1]))``,
    ``.filter(id__in=RawSQL('SELECT ...'))``).

    A value that varies per call belongs in ``params``, never interpolated into ``sql`` - that is an
    SQL injection hole. ``params`` are bound at the ``%s`` placeholders, left to right. A literal
    ``%`` is written ``%%`` (``LIKE '%%stuff%%'``).
    """

    def __init__(self, sql: str, params: Sequence[Any] = ()) -> None:
        super().__init__()
        placeholder_count = sum(1 for token in RAW_SQL_TOKEN_RE.findall(sql) if token == "%s")  # nosec B105
        if placeholder_count != len(params):
            raise QueryError(
                f"RawSQL: sql has {placeholder_count} '%s' placeholder(s) but {len(params)} "
                f"param(s) were given - counts must match (a literal '%' that isn't a "
                f"placeholder must be written as '%%')"
            )
        for param in params:
            if isinstance(param, (dict, set, frozenset)):
                raise QueryError(
                    f"RawSQL: a {type(param).__name__} can't be a parameter - pass a list for an array "
                    "parameter, or json.dumps(...) of it for JSON"
                )
        self.sql = sql
        # A tuple is an array parameter like a list - one shape every driver binds.
        self.params = [ValueWrapper(list(param) if isinstance(param, tuple) else param) for param in params]

    def get_plan_description(self, context: PlanContext) -> PlanDescription:
        """The SQL text and the number of its parameters, and the parameters' values, bound.

        Args:
            context: The context the fragment is used in.

        Returns:
            The description.
        """
        return PlanDescription((RawSQL, self.sql, len(self.params)), [param.value for param in self.params])

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        for param in self.params:
            yield from param.nodes_()

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

    def _get_param_sql(self, param: ValueWrapper, ctx: SqlContext) -> str:
        """One parameter's SQL - explicitly cast on Postgres for a Python number, whose type a bare
        bind parameter doesn't tell.

        Args:
            param: The parameter.
            ctx: The SQL rendering context.

        Returns:
            The parameter's SQL.

        Raises:
            UnSupportedError: The parameter is a list and the database has no array parameters.
        """
        if isinstance(param.value, list) and not ctx.dialect.binds_array_parameters:
            raise UnSupportedError(
                f"RawSQL: a list parameter is bound as an array, which the {ctx.dialect.name} database has no "
                "parameter for"
            )
        return Value.get_typed_term(param, param.value, ParameterPosition.RAW_SQL, ctx.dialect).get_sql(ctx)

    def get_sql(self, ctx: SqlContext) -> str:
        param_iter = iter(self.params)

        def substitute(match: re.Match[str]) -> str:
            if match.group(0) == "%%":
                return "%"
            return self._get_param_sql(next(param_iter), ctx)

        rendered_sql = RAW_SQL_TOKEN_RE.sub(substitute, self.sql)
        # Parenthesized as a subquery operand (`field IN (...)`), like a query builder.
        sql = f"({rendered_sql})" if ctx.subquery else rendered_sql
        if ctx.with_alias:
            return ctx.format_alias_sql(sql, self.alias)
        return sql
