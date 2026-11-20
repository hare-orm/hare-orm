from __future__ import annotations

from collections.abc import Coroutine
from copy import copy
from typing import Any

from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.enums import PlanKeyForm
from hare.query.plans.statement.declared_plan_slots import DeclaredPlanSlots
from hare.query.plans.statement.statement_plan_descriptions import StatementPlanDescriptions
from hare.query.statements.projections.derived_projection import DerivedProjection
from hare.query.statements.select.rows_query import RowsQuery
from hare.sql import Order
from hare.sql.terms.term import Term


class IteratorPageQuery(RowsQuery[Any]):
    """Page a complete logical source without replacing its boundaries or row identity.

    The nested source owns extensions, scopes, DISTINCT and window wrapping. Only the
    enclosing query owns page LIMIT/OFFSET; its ordinary plan describes the source and
    binds the two sets of inputs through their separate origins.
    """

    __slots__ = ("_source", "_bound_source")

    plan_binds_slice = True
    plan_slots: DeclaredPlanSlots = (
        *StatementPlanDescriptions.HEAD_SLOTS,
        ("_get_source_description", PlanKeyForm.DESCRIBED),
        ("_limit", PlanKeyForm.BOUND_INTO_ANOTHER),
        ("_offset", PlanKeyForm.BOUND_INTO_ANOTHER),
    )

    def __init__(self, source: RowsQuery[Any]) -> None:
        super().__init__(source.model, binding=source._binding, context=source._context)
        self._source = source
        self._bound_source: RowsQuery[Any] = source
        source._share_connection(self)
        source._share_default_scope(self)
        self._execution_dependencies = source._execution_dependencies

    def _prepare_build(self) -> None:
        self._bound_source = self._source._get_scoped_copy()
        self._bound_source._apply_connection(self._connection)

    def _get_source_description(self) -> PlanDescription | None:
        return self._source.get_plan_description(PlanContext.EMPTY)

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        return self._describe_statement(True)

    def _build_statement(self, references: RecordedValueReferences | None, *, records_for_caller: bool) -> bool:
        source = self._bound_source
        source._make_query(value_wrapper_references=references)
        source._apply_none_as_subquery()
        projection = DerivedProjection(source.query)
        aliases = tuple(projection.by_alias)
        ordering: list[tuple[Term, Order | None]] = []
        for term, direction in source.query._orderbys:
            column = projection.find(term)
            if column is None:
                if source.query._distinct or source.query._groupbys:
                    continue
                column = projection.include(term)
            ordering.append((projection.query[column.name], direction))
        if not ordering:
            for name, direction in source._get_default_iteration_orderings():
                field = source._binding.fields_map.get(name)  # type: ignore[union-attr]
                if field is None:
                    continue
                term = source._binding.basetable[field.source_field or name]  # type: ignore[union-attr]
                column = projection.find(term)
                if column is None and not source.query._distinct and not source.query._groupbys:
                    column = projection.include(term)
                if column is not None:
                    ordering.append((projection.query[column.name], direction))
        if not ordering and (source.query._distinct or source.query._groupbys):
            ordering = [(projection.query[column.name], Order.ASC) for column in projection.columns]
        self.query = self._connection.query_class.from_(projection.query).select(*projection.get_output(aliases))
        for term, direction in ordering:
            self.query = self.query.orderby(term, order=direction)
        terms = self._prepare_slice_terms(self.query, self._offset)
        self.query._limit, self.query._offset = terms
        if records_for_caller and references is not None:
            self._record_slice_values(references, terms)
        return True

    def _prepare_plan_reading(self) -> Any:
        return self._bound_source._prepare_plan_reading()

    def _execute(self, statement: tuple[str, list[Any]]) -> Coroutine[Any, Any, Any]:
        source = copy(self._bound_source)
        source._apply_connection(self._connection)
        source._statement_plan = self._statement_plan
        source._single = False
        source._raise_does_not_exist = False
        source._applies_result_transform = False
        return source._execute(statement)

    def _prepare_paging(self) -> None:
        return None
