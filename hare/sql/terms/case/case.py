from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from hare.sql.builder_methods import BuilderMethods
from hare.sql.exceptions import CaseException
from hare.sql.sql_context import SqlContext
from hare.sql.terms.node import TNode
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.builder.tables.table import Table


class Case(Term):
    def __init__(self, alias: str | None = None) -> None:
        super().__init__(alias=alias)
        self._cases: list[tuple[Any, Term]] = []
        self._else: Term | None = None

    @staticmethod
    def _branch_sql(term: Term, sql_context: SqlContext) -> str:
        """A branch's SQL - a parameterized literal cast to its type where the dialect can't tell it
        from the other branches (a boolean one is cast by ValueWrapper itself)."""
        sql = term.get_sql(sql_context)
        if (
            sql_context.parameterizer is None
            or not isinstance(term, ValueWrapper)
            or isinstance(term.value, bool)
            or not term.allow_parametrize
            or not sql_context.parameterizer.should_parameterize(term.value)
        ):
            return sql
        return sql_context.dialect.parameters.get_cast_parameter_sql(sql, term.value)

    def __copy__(self) -> Self:
        # _cases is copied - when() appends to it.
        new_term = super().__copy__()
        new_term._cases = list(self._cases)
        return new_term

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]

        for criterion, term in self._cases:
            yield from criterion.nodes_()
            yield from term.nodes_()

        if self._else is not None:
            yield from self._else.nodes_()

    @property
    def is_aggregate(self) -> bool | None:  # type:ignore[override]
        # Branches are alternatives: the CASE is aggregate if any branch is; None only when every
        # branch abstains.
        values = [
            aggregate_vote
            for criterion, term in self._cases
            for aggregate_vote in (criterion.is_aggregate, term.is_aggregate)
        ]
        if self._else is not None:
            values.append(self._else.is_aggregate)

        non_none_values = [aggregate_vote for aggregate_vote in values if aggregate_vote is not None]
        if not non_none_values:
            return None
        return any(non_none_values)

    @BuilderMethods.builder
    def when(self, criterion: Any, term: Any) -> Self:
        self._cases.append((criterion, self.wrap_constant(term)))
        return self

    @BuilderMethods.builder
    def replace_table(self, current_table: Table | None, new_table: Table | None) -> Self:
        """Replaces all occurrences of the specified table with the new table.

        Useful when reusing fields across queries.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            A copy of the term with the tables replaced.
        """
        self._cases = [
            (
                criterion.replace_table(current_table, new_table),
                term.replace_table(current_table, new_table),
            )
            for criterion, term in self._cases
        ]
        self._else = self._else.replace_table(current_table, new_table) if self._else else None
        return self

    @BuilderMethods.builder
    def else_(self, term: Any) -> Self:
        self._else = self.wrap_constant(term)
        return self

    def get_sql(self, sql_context: SqlContext) -> str:
        if not self._cases:
            raise CaseException("At least one 'when' case is required for a CASE statement.")

        when_then_else_context = sql_context.copy(with_alias=False)
        cases = " ".join(
            f"WHEN {criterion.get_sql(when_then_else_context)} THEN {self._branch_sql(term, when_then_else_context)}"
            for criterion, term in self._cases
        )
        else_ = f" ELSE {self._branch_sql(self._else, when_then_else_context)}" if self._else else ""
        case_sql = f"CASE {cases}{else_} END"

        if sql_context.with_alias:
            return sql_context.format_alias_sql(case_sql, self.alias)

        return case_sql
