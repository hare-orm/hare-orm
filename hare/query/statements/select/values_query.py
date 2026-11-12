from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Collection, Coroutine, Iterable, Mapping, Sequence
from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar, Self, TypeVar

from hare.dialects.base.client.database_client import DatabaseClient
from hare.exceptions import (
    QueryError,
)
from hare.query.enums import RowShape
from hare.query.execution.query_execution import QueryExecution
from hare.query.expressions.aggregate_paths.aggregated_multi_valued_paths import AggregatedMultiValuedPaths
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.enums import PlanKeyForm
from hare.query.plans.statement.declarations import ValuesReading
from hare.query.plans.statement.declared_plan_slots import DeclaredPlanSlots
from hare.query.plans.statement.statement_plan_descriptions import StatementPlanDescriptions
from hare.query.queryset.extensions.query_set_extension_result import QuerySetExtensionResult
from hare.query.queryset.extensions.query_set_extensions import QuerySetExtensions
from hare.query.queryset.row_multiplication import RowMultiplication
from hare.query.queryset.selection.values_selection import ValuesSelection
from hare.query.queryset.specification_copying import SpecificationCopying
from hare.query.rows.values_rows.dict_rows import DictRows
from hare.query.rows.values_rows.flat_rows import FlatRows
from hare.query.rows.values_rows.named_rows import NamedRows
from hare.query.rows.values_rows.tuple_rows import TupleRows
from hare.query.rows.values_rows.values_rows import ValuesRows
from hare.query.statements.building.keyset_bounds import KeysetBounds
from hare.query.statements.building.query_joins import QueryJoins
from hare.query.statements.building.query_ordering import QueryOrdering
from hare.query.statements.constants import (
    WINDOW_FILTER_COLUMN_ALIAS_PREFIX,
)
from hare.query.statements.projections.derived_projection import DerivedProjection
from hare.query.statements.select.select_query import SelectQuery
from hare.query.statements.select.values.values_distinct import ValuesDistinct
from hare.query.statements.select.values.values_grouping import ValuesGrouping
from hare.query.statements.select.values.values_output import ValuesOutput
from hare.query.statements.select.values.window_filter_wrapping import WindowFilterWrapping
from hare.query.statements.summary.summary_source import SummarySource
from hare.sql import Order

if TYPE_CHECKING:
    from hare.fields.field import Field as ModelField
    from hare.models import Model
    from hare.query.plans.statement.declarations import CallPlanHit
    from hare.query.plans.statement.statement_plan import StatementPlan
    from hare.query.queryset.queryset import QuerySet
    from hare.query.statements.write.update_query import UpdateQuery

TModel = TypeVar("TModel", bound="Model")


class ValuesQuery(SelectQuery[Any]):
    """Builds and runs the SQL of a queryset returning the values ``.values()``/``.values_list()``
    select - made from the queryset for one build. A row is read by the shape the call asked for:
    a dict by output key, a tuple, the one value alone or a namedtuple."""

    #: The shape the rows are read in, the selected fields, the grouping, the ordering and the slice -
    #: bound when built into another query.
    plan_slots: ClassVar[DeclaredPlanSlots] = (
        *StatementPlanDescriptions.HEAD_SLOTS,
        ("_rows.plan_shape", PlanKeyForm.VALUE),
        ("_get_selected_fields_plan_key", PlanKeyForm.METHOD),
        ("_get_grouping_plan_key", PlanKeyForm.METHOD),
        ("_orderings", PlanKeyForm.TUPLE),
        ("_limit", PlanKeyForm.BOUND_INTO_ANOTHER),
        ("_offset", PlanKeyForm.BOUND_INTO_ANOTHER),
        ("_is_none", PlanKeyForm.VALUE),
        *StatementPlanDescriptions.ROWS_SLOTS,
        *StatementPlanDescriptions.CURSOR_SLOTS,
        *StatementPlanDescriptions.EXPRESSION_TERMS_SLOTS,
    )

    #: The class reading a row of each shape.
    ROWS_CLASSES_BY_SHAPE: ClassVar[dict[RowShape, type[ValuesRows]]] = {
        RowShape.DICT: DictRows,
        RowShape.TUPLE: TupleRows,
        RowShape.FLAT: FlatRows,
        RowShape.NAMED: NamedRows,
    }

    #: values()/values_list() group an aggregate added by or after them by exactly the fields named
    #: - see AwaitableQuery.group_by_must_include_primary_key and _aggregates_per_model_row().
    group_by_must_include_primary_key: ClassVar[bool] = False
    #: A filter on a window function applies to the query wrapped as a derived table - see
    #: _wrap_query_for_window_filter().
    window_filter_wrapping_supported: ClassVar[bool] = True

    def __init__(
        self,
        source_queryset: QuerySet[Any],
        selection: ValuesSelection,
        annotations: dict[str, Any],
        alias_keys: Collection[str],
        selected_fields: Mapping[str, str] | Sequence[str],
    ) -> None:
        """Takes over the filters, ordering, pagination, locking, keyset boundary, CTEs and
        connection of the queryset whose ``.values()``/``.values_list()`` rows this query selects.

        Args:
            source_queryset: That queryset.
            selection: What the rows select.
            annotations: The annotations the query can select - the queryset's own and the
                paths into a field's value it selects.
            alias_keys: The ``.alias()`` keys the query doesn't select.
            selected_fields: ``.values()``: each output key with the field or annotation name it
                selects. ``.values_list()``: the selected names, in order.

        Raises:
            QueryError: ``flat=True`` with other than one selected name.
        """
        # Every setting of the queryset - the rows are its rows, selected otherwise.
        SpecificationCopying.copy_specification(source_queryset, self)
        self._init_build_state()
        self._annotations = annotations
        self._alias_keys = alias_keys
        self._selection = None
        self._selecting_queryset: QuerySet[Any] = source_queryset
        self._model_rows_queryset: QuerySet[Any] | None = None
        self._selection = selection
        # The annotations added after .values()/.values_list() - an aggregate among them groups the
        # rows by the selected fields; one annotated before it, or passed to it as a keyword
        # argument, is computed per model row, like Django.
        self._grouping_annotation_names: frozenset[str] = selection.grouping_names
        rows_class = self.ROWS_CLASSES_BY_SHAPE[selection.shape]
        if selection.shape is RowShape.FLAT and len(selected_fields) != 1:
            raise QueryError(".values_list(flat=True) selects exactly one field")
        outputs = (
            list(selected_fields.items())  # type: ignore[union-attr]
            if rows_class.keyed_by_output_name
            else [(name, name) for name in selected_fields]
        )
        #: Each selected column's alias with the field or annotation name it selects - a dict's
        #: columns are aliased by their output key, a tuple's by their position. A composite key
        #: selects one column per key field, returned combined as one tuple.
        self._selected_fields_by_alias: dict[str, str] = {}
        output_column_aliases: list[tuple[str, ...]] = []
        for output_name, field_name in outputs:
            components = ValuesOutput.get_composite_key_components(self.model, field_name, annotations) or (
                field_name,
            )
            column_aliases = rows_class.get_column_aliases(
                output_name, len(self._selected_fields_by_alias), len(components)
            )
            self._selected_fields_by_alias.update(zip(column_aliases, components, strict=True))
            output_column_aliases.append(column_aliases)
        #: Prepared conversion shared by SQL-equivalent row shapes.
        self._column_converters: list[tuple[str, Callable[[Any], Any] | None]] | None = None
        #: How a row is read - by the shape the call asked for.
        self._rows: ValuesRows = rows_class(
            tuple(output_name for output_name, _field_name in outputs), output_column_aliases
        )
        returns_dicts = rows_class.keyed_by_output_name
        # What QueryConditions.get_filters() selects for a tuple row: the selected fields and every positional
        # alias. An annotation's own name is left out - it would be selected twice.
        self._fields_to_select_sql: set[str] | None = (
            None
            if returns_dicts
            else {
                *(field for field in self._selected_fields_by_alias.values() if field not in annotations),
                *self._selected_fields_by_alias,
            }
        )

    def _apply_values_to(self, queryset: QuerySet[Any]) -> QuerySet[Any, Any]:
        """``queryset`` - the source queryset changed - selecting this query's values.

        Args:
            queryset: The queryset.

        Returns:
            The queryset.
        """
        return queryset._with_selection(self._selection)

    @property
    def _source_queryset(self) -> QuerySet[Any]:
        """The queryset whose rows this query selects values of, returning model instances - made
        the first time it is asked for."""
        queryset = self._model_rows_queryset
        if queryset is None:
            queryset = self._model_rows_queryset = self._selecting_queryset._with_selection(None)
        return queryset

    def _get_first(self, *, reverse: bool) -> QuerySet[Any, Any]:
        """The first (or last) row - of the ordering, else of the group key for a grouped query,
        else of the primary key.

        Args:
            reverse: Take the last row.

        Returns:
            The single-row queryset.

        Raises:
            QueryError: ``last()`` of a sliced query whose rows aren't one per model row.
        """
        # Local import: the queryset package imports the query statements.
        from hare.query.queryset.single_rows.single_row import SingleRow

        source_queryset = self._source_queryset
        if not self._apply_default_ordering(self._orderings) and ValuesGrouping.is_grouped(self):
            group_key_names = ValuesGrouping.get_group_key_names(self)
            ordering_names = [f"-{name}" if reverse else name for name in group_key_names]
            if reverse:
                self._raise_if_sliced_rows_differ_from_source("last")
            queryset = self._apply_values_to(
                source_queryset._with_orderings(*ordering_names) if ordering_names else source_queryset
            )
            return SingleRow.as_single(queryset)  # type: ignore[return-value]
        if not reverse:
            return self._apply_values_to(source_queryset.first())  # type: ignore[arg-type]
        self._raise_if_sliced_rows_differ_from_source("last")
        return self._apply_values_to(source_queryset.last())  # type: ignore[arg-type]

    def _raise_if_sliced_rows_differ_from_source(self, method_name: str) -> None:
        """Rejects re-selecting within a slice whose rows aren't one per row of the source
        queryset - the slice is taken over the grouped or deduplicated rows, which the queryset
        can't narrow to.

        Args:
            method_name: The calling method, for the message.

        Raises:
            QueryError: The query is sliced and grouped, distinct or multiplied by a to-many
                relation.
        """
        if (self._limit is not None or self._offset) and not ValuesGrouping.returns_one_row_per_source_row(self):
            raise QueryError(
                f"{method_name}() can't be used on a sliced grouped/.distinct() .values()/.values_list() query, "
                "or one selecting or ordering by a to-many relation - its slice is taken over rows the "
                f"queryset can't narrow to. Call {method_name}() before slicing."
            )

    def _apply_default_ordering(self, orderings: Iterable[tuple[str, Order]]) -> Iterable[tuple[str, Order]]:
        """Falls back to ``Meta.ordering`` when no explicit ordering was given - never for a query
        whose rows are groups (``.group_by()``, an aggregate added by or after ``.values()``): its
        columns would split the groups.

        Args:
            orderings: The explicit ordering.

        Returns:
            The ordering to apply.
        """
        if (
            not orderings
            and self.model._meta.ordering
            and (self._group_bys or (self._annotations and ValuesGrouping.is_grouped(self)))
        ):
            return orderings
        return super()._apply_default_ordering(orderings)

    def _aggregates_per_model_row(self) -> bool:
        """Whether every aggregate the query computes was annotated before
        ``.values()``/``.values_list()`` or passed to them - the rows are then model rows, grouped by
        the primary key too, like Django.

        Returns:
            True when the aggregates are computed per model row.
        """
        return not self._group_bys and not ValuesGrouping.is_grouped(self)

    def _get_rows_query(self, *, sliced: bool) -> Self:
        """A copy of this query to select from as a derived table - unlocked, returning a list.

        Args:
            sliced: Keep the slice; without it, the ordering is dropped too, unless a keyset
                boundary, the distinct by fields or a plain ``DISTINCT``, which runs over the ordering
                columns, depends on it, or its joins multiply the selected rows.

        Returns:
            The copy.
        """
        rows_query = copy(self)
        rows_query._single = False
        rows_query._raise_does_not_exist = False
        rows_query._select_for_update = False
        rows_query._reverse_result_order = False
        if not sliced:
            rows_query._limit = None
            rows_query._offset = None
            if not (
                self._cursor_values
                or self._before_cursor_values
                or self._distinct_on
                or self._distinct
                or RowMultiplication.ordering_joins_multi_valued_relation(self)
            ):
                rows_query._orderings = []
                rows_query._default_ordering_disabled = True
        return rows_query

    def _get_count_source(self) -> SummarySource:
        """Describe the derived rows counted by count(), keeping the outer slice separate."""
        return SummarySource(self, self._get_rows_query(sliced=False))

    def _get_exists_source(self) -> SummarySource:
        """Describe the derived rows searched by exists(), keeping the outer slice separate."""
        return SummarySource(self, self._get_rows_query(sliced=False))

    def _get_aggregate_source(self) -> SummarySource:
        """Describe aggregate input as source model rows or a sliced derived table."""

        if (
            self._limit is None
            and not self._offset
            and ValuesGrouping.returns_one_row_per_source_row(self)
            and not RowMultiplication.reads_window_function(self)
        ):
            return self._source_queryset._get_aggregate_source()
        return SummarySource(self, self._get_rows_query(sliced=True))

    def _get_update_query(self, **kwargs: Any) -> UpdateQuery:
        """Updates the model rows the source queryset matches, like Django - the selected fields
        don't change which rows are updated.

        Raises:
            QueryError: The rows are groups (an aggregate annotation or ``.group_by()``), or the
                query is sliced over rows that aren't one per model row - which model rows to
                update would be ambiguous.
        """
        if ValuesGrouping.is_grouped(self):
            raise QueryError(
                "update() can't be used on a grouped .values()/.values_list() query (an aggregate "
                "annotation or .group_by()) - its rows are groups, not the model rows an UPDATE writes. "
                "Call update() on the queryset before .values()/.values_list()."
            )
        self._raise_if_sliced_rows_differ_from_source("update")
        return self._source_queryset.update(**kwargs)

    def _get_orderings_with_tie_breaker(self) -> list[tuple[str, Order]]:
        """The ordering with the columns making it unique per row: the group key of a grouped query,
        the selected fields of a ``.distinct()`` one, else the primary key and every selected field
        crossing a to-many relation.

        Returns:
            The ordering ``iterator()`` pages by.
        """
        orderings = list(self._orderings)
        ordering_field_names = {field_name for field_name, _order in orderings}
        primary_key_attribute_names = list(self.model._meta.primary_key_attribute_names)
        output_field_names = [
            ValuesOutput.get_concrete_field_path(self, name) for name in self._selected_fields_by_alias.values()
        ]
        if ValuesGrouping.is_grouped(self):
            tie_breaker_names = ValuesGrouping.get_group_key_names(self)
        elif self._distinct or self._distinct_on:
            tie_breaker_names = (
                primary_key_attribute_names
                if set(primary_key_attribute_names) <= set(output_field_names)
                else output_field_names
            )
        else:
            multi_valued_names = []
            annotation_names: set[str] | None = None
            for name in output_field_names:
                if name in self._annotations:
                    if annotation_names is None:
                        annotation_names = set(
                            RowMultiplication.get_row_multiplying_annotation_names(self, selected_only=True)
                        )
                    if name in annotation_names:
                        multi_valued_names.append(name)
                elif AggregatedMultiValuedPaths.get_multi_valued_paths(self.model, name):
                    multi_valued_names.append(name)
            ordering_is_unique = self._ordering_field_names_are_unique(ordering_field_names)
            tie_breaker_names = [*([] if ordering_is_unique else primary_key_attribute_names), *multi_valued_names]
        return orderings + [
            (name, Order.ASC) for name in dict.fromkeys(tie_breaker_names) if name not in ordering_field_names
        ]

    def _get_keyset_readers(self) -> list[Callable[[Any], Any]] | None:
        """Readers of each ordering field's value off an output row, when every ordering field is
        a selected column of the model itself.

        Returns:
            The readers, or None for ``OFFSET`` paging - also an ordering by a field whose values aren't
            compared by ``<``/``>`` (an ``HStoreField``), and rows that can repeat their values (a filter
            through a to-many relation, a ``Lateral``), which a keyset boundary would skip.
        """
        if (
            self._distinct_on
            or RowMultiplication.reads_window_function(self)
            or ValuesOutput.selects_composite_key(self)
            or (self._extension_calls and QuerySetExtensions.changes_rows(self))
            or (not ValuesGrouping.is_grouped(self) and RowMultiplication.rows_repeat_per_primary_key(self))
            or self._can_tie_without_primary_key()
        ):
            # A keyset condition would change the rows a window function is computed over, or an extension
            # call picks from (limit_by()); a composite key doesn't line up with the selected columns.
            return None
        readers: list[Callable[[Any], Any]] = []
        for field_name, _order in self._orderings:
            if field_name in self._annotations or field_name not in self.model._meta.direct_fields:
                return None
            if not KeysetBounds.compares_by_order(self.model._meta.fields_map[field_name]):
                return None
            reader = ValuesOutput.get_output_reader(self, field_name)
            if reader is None:
                return None
            readers.append(reader)
        return readers

    def _can_tie_without_primary_key(self) -> bool:
        """Whether rows of a model without a primary key can tie on the ordering - neither grouped nor
        distinct, its fields not unique: rows past a keyset boundary equal to it would be skipped.

        Returns:
            True when they can.
        """
        return (
            not self.model._meta.has_primary_key
            and not ValuesGrouping.is_grouped(self)
            and not self._distinct
            and not self._ordering_field_names_are_unique({field_name for field_name, _order in self._orderings})
        )

    def _get_default_iteration_orderings(self) -> list[tuple[str, Order]]:
        """``Meta.ordering``, else the group key of a grouped query or the selected fields of a distinct
        one, else the primary key - like Django."""
        row_key_names = self._get_row_key_names()
        return list(self._apply_default_ordering(self._orderings)) or [
            (name, Order.ASC)
            for name in (row_key_names if row_key_names is not None else self.model._meta.primary_key_attribute_names)
        ]

    def _get_row_key_names(self) -> list[str] | None:
        """The names telling one output row from another where a model row isn't one - the group key of a
        grouped query, the selected fields of a ``distinct()`` one: an unordered query pages by them, as the
        primary key would add a column to ``SELECT DISTINCT`` and repeat its rows.

        Returns:
            The names, None for rows of model rows.
        """
        if ValuesGrouping.is_grouped(self):
            return ValuesGrouping.get_group_key_names(self)
        if self._distinct and not self._distinct_on:
            return [
                ValuesOutput.get_concrete_field_path(self, name) for name in self._selected_fields_by_alias.values()
            ]
        return None

    def _stream_batches(
        self, connection: DatabaseClient, chunk_size: int, statement: tuple[str, list[Any]]
    ) -> AsyncGenerator[list[Any]]:
        return self._rows.stream_batches(connection, *statement, self._get_column_converters(), chunk_size)

    def _get_column_converters(self) -> list[tuple[str, Callable[[Any], Any] | None]]:
        """Use the plan's readers, the completed build's readers, or prepare an uncached selection.

        Execution and cursor streaming select readers identically; this method retains no
        fallback result that could survive a later build with different output columns.
        """
        column_converters = (
            self._statement_plan.result_reading if self._statement_plan is not None else self._column_converters
        )
        return ValuesOutput.get_column_converters(self) if column_converters is None else column_converters

    def _execute(self, statement: tuple[str, list[Any]]) -> Coroutine[Any, Any, Any]:
        column_converters = self._get_column_converters()
        if self._extension_calls and self._applies_result_transform:
            # A dialect's result transform reads the driver's result too.
            return QueryExecution.read(
                self, self._connection, self._read_with_extension_result(statement, column_converters)
            )
        return self._rows.read(self, self._connection, *statement, column_converters, retry_read=self.is_read_only)

    async def _read_with_extension_result(
        self, statement: tuple[str, list[Any]], column_converters: list[tuple[str, Any]]
    ) -> Any:
        """Reads the rows and keeps the driver's result for the dialect's result transform."""
        statement_result = await self._rows.fetch_result(
            self._connection,
            *statement,
            retry_read=self.is_read_only,
        )
        rows = self._rows.convert_batch(self._connection, statement_result, column_converters)
        result = self._get_single_or_list_result(rows)
        reader = (
            self._rows.get_result_reader(self._connection, statement_result, column_converters)
            if statement_result.rows or statement_result.column_names
            else None
        )
        self._extension_result = QuerySetExtensionResult(result, statement_result, reader)
        return result

    def _get_call_signature_type_part(self) -> tuple[Any, Sequence[Any]]:
        # SQL-equivalent row shapes share conversion metadata; the calls tell them apart.
        return (self._rows.plan_shape,), ()

    @staticmethod
    def get_queryset_type_part(queryset: QuerySet[Any, Any]) -> tuple[Any, Sequence[Any]]:
        selection: ValuesSelection = queryset._selection  # type: ignore[assignment]
        return (ValuesQuery.ROWS_CLASSES_BY_SHAPE[selection.shape].plan_shape,), ()

    def _get_call_plan_reading(self, plan: StatementPlan) -> Any:
        # The reader of the rows' shape - the calls select the fields and the shape - and the
        # converters of the columns, kept with the plan.
        by_position = self._connection.features.supports_positional_rows
        layout = (
            self._rows.get_layout(plan.result_reading, plan.result_column_names if by_position else None)
            if plan.result_column_names or not by_position
            else None
        )
        return ValuesReading(self._rows, plan.result_reading, layout)

    @staticmethod
    def read_call_plan(
        queryset: QuerySet[Any, Any], connection: DatabaseClient, hit: CallPlanHit
    ) -> Coroutine[Any, Any, Any]:
        """Reads the values of a queryset on the plan of its calls - the reader of their shape kept
        with the plan. Nothing is copied.

        Args:
            queryset: The queryset.
            connection: This execution's connection.
            hit: The plan found (``StatementPlanRuns.find_call_plan()``).

        Returns:
            The read of the rows, or of the one row of a single-row queryset.
        """
        reading: ValuesReading = hit.reading
        return reading.reader.read(
            queryset, connection, hit.sql, hit.parameters, reading.converters, retry_read=True, layout=reading.layout
        )

    def _get_plan_record(self) -> dict[str, Any]:
        self._column_converters = ValuesOutput.get_column_converters(self)
        return {
            "result_reading": self._column_converters,
            "result_column_names": self._get_result_column_names(),
        }

    def _get_selected_field_names(self) -> Collection[str]:
        return self._selected_fields_by_alias.values()

    def _get_row_join_lookups(self) -> list[str]:
        return [
            *(
                field_name
                for field_name in (*self._selected_fields_by_alias.values(), *self._group_bys)
                if isinstance(field_name, str) and field_name not in self._annotations
            ),
            *QueryJoins.get_ordering_lookups(self),
        ]

    def _get_group_key_lookups(self) -> list[str]:
        """The explicit ``.group_by()`` fields, or else the selected fields an aggregate is
        implicitly grouped by.

        Returns:
            The lookups, annotations left out.
        """
        if self._group_bys:
            return super()._get_group_key_lookups()
        return QueryJoins.get_field_lookups(
            self,
            (field_name for field_name in self._selected_fields_by_alias.values() if isinstance(field_name, str)),
        )

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        """Describes this query built into another one (``_get_scoped_copy()``) - its slice is
        among its values.

        Args:
            context: The enclosing query's - this query resolves names against its own
                annotations.

        Returns:
            The description, None for a query that keeps no plan - one reading a sample.
        """
        if self._options.keeps_no_plan():
            # Its percent and seed are written into FROM, not bound.
            return None
        query = self._get_scoped_copy()
        output_aliases = query._selected_fields_by_alias
        ValuesOutput.register_selected_annotations(query, output_aliases)
        return query._describe_statement(True)

    def _make_subquery(self, **kwargs: Any) -> None:
        """Builds ``self.query`` to be embedded in an enclosing query - selecting the requested columns
        alone, a plain ``.distinct()`` ordered by another one run over it in a derived table.

        Args:
            kwargs: Passed on to ``_build_query()``.
        """
        self._make_query(**kwargs)
        output_aliases = list(self._selected_fields_by_alias.keys())
        if len(self.query._selects) > len(output_aliases):
            # A copy - selecting from it sets its alias, and it can be the query a plan keeps.
            inner_query = copy(self.query)
            self.query = self._connection.query_class.from_(inner_query).select(
                *(inner_query[alias].as_(alias) for alias in output_aliases)
            )
        self._apply_none_as_subquery()

    def _annotation_renders_as_expression(self, annotation_name: str) -> bool:
        """An annotation left out of values()/values_list() isn't selected either.

        Args:
            annotation_name: The annotation name.

        Returns:
            True when the annotation isn't among the selected fields.
        """
        selected_field_names = self._selected_fields_by_alias.values()
        return annotation_name not in selected_field_names or super()._annotation_renders_as_expression(
            annotation_name
        )

    def _wrap_query_for_window_filter(self) -> None:
        """Applies the filters reading a window function to the built query wrapped as a derived table
        - SQL takes no window function in WHERE/HAVING. The outer query gets the window filters,
        ordering, DISTINCT and LIMIT/OFFSET, and the WITH clause.

        Raises:
            QueryError: The query is locked with select_for_update().
        """
        if self._select_for_update:
            raise QueryError(
                "select_for_update() can't be combined with a filter on a window function (Window(...)) - "
                "the filter applies to the query wrapped in a subquery, whose rows can't be locked."
            )
        output_aliases = list(self._selected_fields_by_alias.keys())
        projection = DerivedProjection(
            self.query,
            column_prefix=WINDOW_FILTER_COLUMN_ALIAS_PREFIX,
            output_fields=self._annotation_output_fields,
        )
        inner_query = projection.query
        outer_criterion = WindowFilterWrapping.get_derived_table_criterion(
            self._window_filter_criterion,  # type: ignore[arg-type]
            projection.get_reference,
        )
        order_columns = [(projection.get_reference(term), order) for term, order in inner_query._orderbys]
        distinct_on_columns = [projection.get_reference(term) for term in inner_query._distinct_on]
        output_columns = {alias: projection.by_alias[alias].name for alias in output_aliases}
        distinct, inner_query._distinct = inner_query._distinct, False
        inner_query._distinct_on = []
        inner_query._orderbys = []
        limit, offset = inner_query._limit, inner_query._offset
        inner_query._limit = inner_query._offset = None
        with_clauses, inner_query._with = inner_query._with, []
        # FROM captures a copy; move outer clauses before embedding the inner query.
        outer_query = self._connection.query_class.from_(inner_query).select(*projection.get_output(output_aliases))
        outer_query = outer_query.where(outer_criterion)
        if distinct:
            outer_query._distinct = True
            selected_columns = set(output_columns.values())
            for order_column, _order in order_columns:
                # SELECT DISTINCT must select what it orders by, as the inner query did.
                if order_column.name not in selected_columns:
                    selected_columns.add(order_column.name)
                    outer_query = outer_query.select(order_column)
        if distinct_on_columns:
            outer_query = outer_query.distinct_on(*distinct_on_columns)
        for order_column, order in order_columns:
            outer_query = outer_query.orderby(order_column, order=order)
        outer_query._limit, outer_query._offset = limit, offset
        outer_query._with = with_clauses
        self.query = outer_query

    def _orders_rows(self) -> bool:
        return True

    def _prepare_build(self) -> None:
        output_aliases = self._selected_fields_by_alias
        ValuesDistinct.raise_if_over_encrypted_field(self, output_aliases.values())
        # Kept as given: the registration below registers each selected annotation again under
        # its alias - the build resolves the selected fields from the original ones.
        self._given_annotations = self._annotations
        # Before the query is described: the build records the values against the annotations
        # after this registration, so the description lists them against the same ones. The
        # registration is a pure function of the selected fields and the originating annotations.
        ValuesOutput.register_selected_annotations(self, output_aliases)

    def _get_build_plan_description(self) -> PlanDescription | None:
        return self._describe_statement(False)

    def _get_selected_fields_plan_key(self) -> tuple[tuple[str, str], ...]:
        """The selected fields - each column's alias with the field or annotation it selects."""
        return tuple(self._selected_fields_by_alias.items())

    def _get_grouping_plan_key(self) -> tuple[str, ...]:
        """The annotations added after ``.values()`` that group the rows by the selected fields."""
        return tuple(sorted(self._grouping_annotation_names.intersection(self._annotations)))

    def _select_columns(self) -> None:
        # The selected fields are resolved from the annotations as given: the registration runs
        # again, in order, alongside each field's own column.
        self._annotations = dict(self._given_annotations)
        self.query = self._get_base_query()
        QueryJoins.apply_effective_basetable(self)
        for alias, field in self._selected_fields_by_alias.items():
            ValuesOutput.add_field_to_select_query(self, field, alias)

    def _apply_ordering(self) -> None:
        given_annotations = self._given_annotations
        # QueryOrdering.get_ordering() needs each selected annotation's alias, so an ORDER BY reference uses
        # the name that's actually in SELECT.
        annotation_output_aliases = {
            field: alias for alias, field in self._selected_fields_by_alias.items() if field in given_annotations
        }
        QueryOrdering.get_ordering(
            self,
            model=self.model,
            table=self._effective_basetable(),
            orderings=self._orderings,
            annotations=self._annotations,
            fields_for_select=ValuesOutput.get_ordering_field_names(self, annotation_output_aliases),
            annotation_output_aliases=annotation_output_aliases,
        )

    def _get_explicit_group_bys(self) -> tuple[str, ...]:
        return self._group_bys

    def _get_selected_sql_fields(self) -> Collection[str] | None:
        """The fields ``QueryConditions.get_filters()`` selects, None for its default."""
        return self._fields_to_select_sql

    def _prune_unselected_annotations(self) -> None:
        """Drops from the SELECT list the annotations a dict row doesn't return - one not named in
        ``.values()`` is filtered or ordered by, never returned."""
        if self._rows.keyed_by_output_name:
            aliases = self._selected_fields_by_alias
            self.query._selects = [select for select in self.query._selects if select.alias in aliases]

    def _get_output_columns(self) -> list[tuple[str, str, str, ModelField[Any] | None]]:
        """The selected columns of the built query, to read from it as a derived table.

        Returns:
            Per column: its output name, the field or annotation name it selects, its alias and
            the field its value decodes through.
        """
        return [
            (
                alias if self._rows.keyed_by_output_name else field_name,
                field_name,
                alias,
                ValuesOutput.get_output_value_field(self, field_name),
            )
            for alias, field_name in self._selected_fields_by_alias.items()
        ]

    async def _fetch_rows(
        self,
        connection: DatabaseClient,
        sql: str,
        parameters: list[Any],
        column_converters: Sequence[tuple[str, Callable[[Any], Any] | None]],
    ) -> list[Any]:
        """Runs a query selecting this query's columns and builds the output rows.

        Args:
            connection: The connection.
            sql: The SQL.
            parameters: Its parameters.
            column_converters: Each column's alias and decoder.

        Returns:
            The output rows.
        """
        return await self._rows.fetch(connection, sql, parameters, column_converters, retry_read=self.is_read_only)
