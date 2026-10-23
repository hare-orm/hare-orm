from __future__ import annotations

from collections.abc import Collection
from typing import TYPE_CHECKING, ClassVar, TypeVar, cast

from hare.query.expressions.value_references.expression_arguments import ExpressionArguments
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.statements.building.query_annotations import QueryAnnotations
from hare.query.statements.building.query_conditions import QueryConditions
from hare.query.statements.building.query_ctes import QueryCtes
from hare.query.statements.building.query_grouping import QueryGrouping
from hare.query.statements.building.row_locks import RowLocks
from hare.query.statements.select.rows_query import RowsQuery

if TYPE_CHECKING:
    from hare.models import Model

TModel = TypeVar("TModel", bound="Model")


class SelectQuery(RowsQuery[TModel], abstract=True):
    """Builds the SELECT of a queryset - the steps of the build and their order. A subclass says which
    columns are selected, which relations are joined for loading and how a row is read.
    """

    binds_expression_term_values: ClassVar[bool] = True
    plan_binds_slice: ClassVar[bool] = True

    def _build_statement(
        self, value_wrapper_references: RecordedValueReferences | None, *, records_for_caller: bool
    ) -> bool:
        self._select_columns()
        self._apply_ordering()
        QueryConditions.get_filters(
            self, self._get_selected_sql_fields(), value_wrapper_references=value_wrapper_references
        )
        # The loaded relations' columns follow the model's own - an ordering column a plain DISTINCT
        # selects goes after them.
        self._join_loaded_relations(value_wrapper_references)
        # Before the DISTINCT: an ordering column a plain DISTINCT selects stays selected.
        self._prune_unselected_annotations()
        QueryConditions.get_distinct(self, self._distinct, self._distinct_on, self._orderings, self._annotations)
        if self._limit is not None:
            self.query._limit = self.query._wrapper_class(self._limit)
        if self._offset is not None:
            self.query._offset = self.query._wrapper_class(self._offset)
        QueryGrouping.apply_auto_group_by(self)
        group_bys = self._get_explicit_group_bys()
        if group_bys:
            self.query._groupbys = QueryGrouping.get_group_by_clause_terms(self, group_bys)
            QueryGrouping.group_columns_read(self)
        QueryCtes.apply_with_ctes(self, value_wrapper_references=value_wrapper_references)
        if self._select_for_update:
            # After every JOIN is built: a JOIN across a relation is LEFT OUTER, which a bare FOR
            # UPDATE rejects - the lock is narrowed to the base table unless of=(...) says more.
            RowLocks.apply_select_for_update(self)
        if records_for_caller:
            recorded_references = cast("RecordedValueReferences", value_wrapper_references)
            recorded_references.extend(QueryAnnotations.get_expression_term_value_references(self))
            # A query built into another one has its slice among its values - the enclosing
            # query binds it.
            for attribute, slice_bound_term in (("_limit", self.query._limit), ("_offset", self.query._offset)):
                if slice_bound_term is not None:
                    ExpressionArguments.record_literal(recorded_references, self, attribute, slice_bound_term)
        if self.reads_rows_in_derived_table():
            self._apply_extension_calls_to_row_source()
        if self._window_filter_criterion is not None:
            # The wrapping moves the terms of the values as they are - their references still bind.
            self._wrap_query_for_window_filter()
        if not records_for_caller:
            self._finish_statement()
        return True

    def _select_columns(self) -> None:
        """Starts ``self.query`` with the columns the rows are read from."""
        raise NotImplementedError()

    def _apply_ordering(self) -> None:
        """Orders ``self.query`` - by the names the selected columns allow."""
        raise NotImplementedError()

    def _get_selected_sql_fields(self) -> Collection[str] | None:
        """The fields ``QueryConditions.get_filters()`` selects, None for its default."""
        return None

    def _join_loaded_relations(self, value_wrapper_references: RecordedValueReferences | None) -> None:
        """Joins the relations loaded together with the rows, selecting their columns. None by
        default.

        Args:
            value_wrapper_references: The list the value references of the joins' conditions go into.
        """

    def _prune_unselected_annotations(self) -> None:
        """Drops from the SELECT list the annotations the rows don't return. Drops none by
        default."""

    def _get_explicit_group_bys(self) -> tuple[str, ...]:
        """The ``.group_by()`` fields the rows are grouped by - none by default: model instances
        ignore them."""
        return ()

    def _wrap_query_for_window_filter(self) -> None:
        """Applies the filters reading a window function to ``self.query`` wrapped as a derived
        table."""
        raise NotImplementedError()

    def _finish_statement(self) -> None:
        """Works out what reading the rows of the statement just built needs - for a query run by
        itself, not one built into another."""
