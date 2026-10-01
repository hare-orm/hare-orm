from __future__ import annotations

from collections.abc import Collection
from typing import TYPE_CHECKING, ClassVar, TypeVar, cast

from hare.query.expressions.enums import ValueRefOrigin
from hare.query.expressions.value_refs.literal_value_ref import LiteralValueRef
from hare.query.expressions.value_refs.value_ref_types import RecordedValueRefs
from hare.query.statements.select.rows_query import RowsQuery

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model

TModel = TypeVar("TModel", bound="Model")


class SelectQuery(RowsQuery[TModel], abstract=True):
    """Builds the SELECT of a queryset - the steps of the build and their order. A subclass says which
    columns are selected, which relations are joined for loading and how a row is read.
    """

    binds_expression_term_values: ClassVar[bool] = True
    plan_binds_slice: ClassVar[bool] = True

    #: Whether a plain ``.distinct()`` of this build keeps, per combination of the selected
    #: columns, the first row in the ordering instead of running ``SELECT DISTINCT`` - worked out
    #: by ``_prepare_build()``.
    _distinct_requires_first_occurrence_rows: bool = False

    def _build_statement(self, value_wrapper_refs: RecordedValueRefs | None, *, records_for_caller: bool) -> bool:
        self._select_columns()
        self._apply_ordering()
        self.get_filters(self._get_selected_sql_fields(), value_wrapper_refs=value_wrapper_refs)
        keeps_first_occurrence_rows = self._distinct_requires_first_occurrence_rows
        if keeps_first_occurrence_rows:
            # No DISTINCT here - get_distinct() would add the ordering columns to SELECT DISTINCT;
            # _wrap_query_for_distinct_first_occurrence() keeps the first row of each combination
            # of the selected columns instead, and slices those rows itself.
            self.query._distinct = False
            self.query._distinct_on = []
        else:
            self.get_distinct(self._distinct, self._distinct_on, self._orderings, self._annotations)
            if self._limit is not None:
                self.query._limit = self.query._wrapper_cls(self._limit)
            if self._offset is not None:
                self.query._offset = self.query._wrapper_cls(self._offset)
        self._join_loaded_relations(value_wrapper_refs)
        self._prune_unselected_annotations()
        self._apply_auto_group_by()
        group_bys = self._get_explicit_group_bys()
        if group_bys:
            self.query._groupbys = self._get_group_bys(*group_bys)
        self._apply_with_ctes(value_wrapper_refs=value_wrapper_refs)
        if self._select_for_update:
            # After every JOIN is built: a JOIN across a relation is LEFT OUTER, which a bare FOR
            # UPDATE rejects - the lock is narrowed to the base table unless of=(...) says more.
            self._apply_select_for_update()
        if records_for_caller and not keeps_first_occurrence_rows:
            recorded_refs = cast("RecordedValueRefs", value_wrapper_refs)
            recorded_refs.extend(self._get_expression_term_value_refs())
            # A query built into another one has its slice among its values - the enclosing
            # query binds it.
            for slice_bound_term in (self.query._limit, self.query._offset):
                if slice_bound_term is not None:
                    recorded_refs.append((ValueRefOrigin.SUBQUERY, LiteralValueRef(slice_bound_term)))
        keeps_plan = True
        if self._window_filter_criterion is not None:
            # Never kept as a plan - a plan hit refreshes only the values of the query's own WHERE.
            if records_for_caller:
                cast("RecordedValueRefs", value_wrapper_refs).append((ValueRefOrigin.SUBQUERY, None))
            self._wrap_query_for_window_filter()
            keeps_plan = False
        if keeps_first_occurrence_rows:
            self._wrap_query_for_distinct_first_occurrence()
        if not records_for_caller:
            self._finish_statement()
        return keeps_plan

    def _select_columns(self) -> None:
        """Starts ``self.query`` with the columns the rows are read from."""
        raise NotImplementedError()  # pragma: nocoverage

    def _apply_ordering(self) -> None:
        """Orders ``self.query`` - by the names the selected columns allow."""
        raise NotImplementedError()  # pragma: nocoverage

    def _get_selected_sql_fields(self) -> Collection[str] | None:
        """The fields ``get_filters()`` selects, None for its default."""
        return None

    def _join_loaded_relations(self, value_wrapper_refs: RecordedValueRefs | None) -> None:
        """Joins the relations loaded together with the rows, selecting their columns. None by
        default.

        Args:
            value_wrapper_refs: The list the value references of the joins' conditions go into.
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
        raise NotImplementedError()  # pragma: nocoverage

    def _wrap_query_for_distinct_first_occurrence(self) -> None:
        """Keeps, per distinct combination of the selected columns, the first row in the
        ordering, then orders and slices those rows."""
        raise NotImplementedError()  # pragma: nocoverage

    def _finish_statement(self) -> None:
        """Works out what reading the rows of the statement just built needs - for a query run by
        itself, not one built into another."""
