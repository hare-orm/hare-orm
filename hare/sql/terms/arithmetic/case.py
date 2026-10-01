from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from hare.sql.context import SqlContext
from hare.sql.exceptions import CaseException
from hare.sql.terms.base.node import TNode
from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper
from hare.sql.utils import builder

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.queries.tables.table import Table


class Case(Term):
    def __init__(self, alias: str | None = None) -> None:
        super().__init__(alias=alias)
        self._cases: list[tuple[Any, Term]] = []
        self._else: Term | None = None

    def _branch_sql(self, term: Term, ctx: SqlContext) -> str:
        """A branch's SQL - a parameterized literal cast to its type where the dialect can't tell it
        from the other branches (a boolean one is cast by ValueWrapper itself)."""
        sql = term.get_sql(ctx)
        if (
            ctx.parameterizer is None
            or not isinstance(term, ValueWrapper)
            or isinstance(term.value, bool)
            or not term.allow_parametrize
            or not ctx.parameterizer.should_parameterize(term.value)
        ):
            return sql
        return ctx.dialect.get_cast_parameter_sql(sql, term.value)

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
        values = [v for criterion, term in self._cases for v in (criterion.is_aggregate, term.is_aggregate)]
        if self._else is not None:
            values.append(self._else.is_aggregate)

        non_none_values = [v for v in values if v is not None]
        if not non_none_values:
            return None
        return any(non_none_values)

    @builder
    def when(self, criterion: Any, term: Any) -> Self:  # type:ignore[return]
        self._cases.append((criterion, self.wrap_constant(term)))

    @builder
    def replace_table(  # type:ignore[return]
        self, current_table: Table | None, new_table: Table | None
    ) -> Self:
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

    @builder
    def else_(self, term: Any) -> Self:
        self._else = self.wrap_constant(term)
        return self

    def get_sql(self, ctx: SqlContext) -> str:
        if not self._cases:
            raise CaseException("At least one 'when' case is required for a CASE statement.")

        when_then_else_ctx = ctx.copy(with_alias=False)
        cases = " ".join(
            f"WHEN {criterion.get_sql(when_then_else_ctx)} THEN {self._branch_sql(term, when_then_else_ctx)}"
            for criterion, term in self._cases
        )
        else_ = f" ELSE {self._branch_sql(self._else, when_then_else_ctx)}" if self._else else ""
        case_sql = f"CASE {cases}{else_} END"

        if ctx.with_alias:
            return ctx.format_alias_sql(case_sql, self.alias)

        return case_sql
