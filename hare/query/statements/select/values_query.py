from __future__ import annotations

import contextlib
from collections.abc import (
    AsyncGenerator,
    Callable,
    Collection,
    Iterable,
    Mapping,
    Sequence,
)
from copy import copy
from dataclasses import replace
from typing import TYPE_CHECKING, Any, ClassVar, Self, TypeVar, cast

from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.base.results.statement_result import StatementResult
from hare.exceptions import (
    QueryError,
)
from hare.query.enums import RowShape
from hare.query.expressions import Expression
from hare.query.expressions.aggregate_paths.aggregated_multi_valued_paths import AggregatedMultiValuedPaths
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.enums import PlanKeyForm
from hare.query.plans.statement.declared_plan_slots import DeclaredPlanSlots
from hare.query.plans.statement.statement_plan_descriptions import StatementPlanDescriptions
from hare.query.queryset.row_multiplication import RowMultiplication
from hare.query.queryset.selection.values_selection import ValuesSelection
from hare.query.queryset.specification_copying import SpecificationCopying
from hare.query.rows.values_rows.dict_rows import DictRows
from hare.query.rows.values_rows.flat_rows import FlatRows
from hare.query.rows.values_rows.named_rows import NamedRows
from hare.query.rows.values_rows.tuple_rows import TupleRows
from hare.query.rows.values_rows.value_field import ValueField
from hare.query.rows.values_rows.values_rows import ValuesRows
from hare.query.statements.building.query_grouping import QueryGrouping
from hare.query.statements.building.query_joins import QueryJoins
from hare.query.statements.building.query_ordering import QueryOrdering
from hare.query.statements.constants import (
    DISTINCT_FIRST_OCCURRENCE_ORDERING_ALIAS_PREFIX,
    DISTINCT_FIRST_OCCURRENCE_ROW_NUMBER_ALIAS,
    WINDOW_FILTER_COLUMN_ALIAS_PREFIX,
)
from hare.query.statements.select.select_query import SelectQuery
from hare.query.statements.select.values.distinct_first_occurrence import DistinctFirstOccurrence
from hare.query.statements.select.values.values_grouping import ValuesGrouping
from hare.query.statements.select.values.values_output import ValuesOutput
from hare.query.statements.select.values.values_reading import ValuesReading
from hare.query.statements.select.values.window_filter_wrapping import WindowFilterWrapping
from hare.sql import Order
from hare.sql.analytics.row_number import RowNumber
from hare.sql.enums import Equality
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.field import Field
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field as ModelField
    from hare.models import Model
    from hare.query.plans.statement.statement_plan import StatementPlan
    from hare.query.queryset.queryset import QuerySet
    from hare.query.statements.summary.aggregate_query import AggregateQuery
    from hare.query.statements.summary.count_query import CountQuery
    from hare.query.statements.summary.exists_query import ExistsQuery
    from hare.query.statements.write.update_query import UpdateQuery

TModel = TypeVar("TModel", bound="Model")


class ValuesQuery(SelectQuery[Any]):
    """Builds and runs the SQL of a queryset returning the values ``.values()``/``.values_list()``
    select - made from the queryset for one build. A row is read by the shape the call asked for:
    a dict by output key, a tuple, the one value alone or a namedtuple."""

    #: The shape the rows are read in, the selected fields, the grouping, the ordering, the
    #: first-occurrence settings of a .distinct() and the slice - bound when built into another query.
    plan_slots: ClassVar[DeclaredPlanSlots] = (
        *StatementPlanDescriptions.HEAD_SLOTS,
        ("_rows.plan_shape", PlanKeyForm.VALUE),
        ("_get_selected_fields_plan_key", PlanKeyForm.METHOD),
        ("_get_grouping_plan_key", PlanKeyForm.METHOD),
        ("_orderings", PlanKeyForm.TUPLE),
        ("_distinct_over_ordering_columns", PlanKeyForm.VALUE),
        # last() of a first-occurrence .distinct() orders the outer query backwards.
        ("_first_occurrence_rows_reversed", PlanKeyForm.VALUE),
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

    # pylint: disable=W0223

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
        # Set fresh by _make_query() on every call - see
        # DistinctFirstOccurrence.distinct_needs_first_occurrence_rows().
        self._distinct_requires_first_occurrence_rows = False
        self._distinct_over_ordering_columns = selection.distinct_over_ordering_columns
        self._first_occurrence_rows_reversed = selection.first_occurrence_rows_reversed
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
            list(cast("Mapping[str, str]", selected_fields).items())
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
        #: How a row is read - by the shape the call asked for.
        #: How the rows are read, taken from the plan the query runs on - None until then, or when
        #: the plan hasn't read this row shape yet.
        self._values_reading: ValuesReading | None = None
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
        if reverse and self._distinct and not self._distinct_on and not ValuesGrouping.is_grouped(self):
            ordering_field_names = [
                field_name for field_name, _order in self._apply_default_ordering(self._orderings, self._annotations)
            ]
            if not ordering_field_names:
                ordering_field_names = list(self.model._meta.primary_key_attribute_names)
                source_queryset = source_queryset._with_orderings(*ordering_field_names)
            output_field_names = {
                ValuesOutput.get_concrete_field_path(self, name) for name in ValuesOutput.get_output_field_names(self)
            }
            if any(field_name not in output_field_names for field_name in ordering_field_names):
                # Reversing the ordering would pick another row of each combination of the selected
                # columns - the last row is the first-occurrence rows' last one instead.
                self._raise_if_sliced_rows_differ_from_source("last")
                queryset = source_queryset._with_selection(
                    replace(cast("ValuesSelection", self._selection), first_occurrence_rows_reversed=True)
                )
                return cast("QuerySet[Any, Any]", SingleRow.as_single(queryset))
        if not self._apply_default_ordering(self._orderings, self._annotations) and ValuesGrouping.is_grouped(self):
            group_key_names = ValuesGrouping.get_group_key_names(self)
            ordering_names = [f"-{name}" if reverse else name for name in group_key_names]
            if reverse:
                self._raise_if_sliced_rows_differ_from_source("last")
            queryset = self._apply_values_to(
                source_queryset._with_orderings(*ordering_names) if ordering_names else source_queryset
            )
            return cast("QuerySet[Any, Any]", SingleRow.as_single(queryset))
        if not reverse:
            return self._apply_values_to(cast("QuerySet[Any]", source_queryset.first()))
        self._raise_if_sliced_rows_differ_from_source("last")
        return self._apply_values_to(cast("QuerySet[Any]", source_queryset.last()))

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

    def _apply_default_ordering(
        self,
        orderings: Iterable[tuple[str, Order]],
        annotations: dict[str, Term | Expression],
    ) -> Iterable[tuple[str, Order]]:
        """Falls back to ``Meta.ordering`` when no explicit ordering was given - never for a
        ``.group_by()`` query, whose rows are groups (``Meta.ordering`` columns aren't grouped).

        Args:
            orderings: The explicit ordering.
            annotations: The query's annotations.

        Returns:
            The ordering to apply.
        """
        if self._group_bys:
            return orderings
        return super()._apply_default_ordering(orderings, annotations)

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
                boundary or ``DISTINCT ON`` depends on it.

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
            if not (self._cursor_values or self._before_cursor_values or self._distinct_on):
                rows_query._orderings = []
                rows_query._default_ordering_disabled = True
        return rows_query

    def _get_count_query(self) -> CountQuery:
        """The number of rows the query returns, counted over the query as a derived table."""
        from hare.query.statements.summary.count_query import CountQuery

        return CountQuery(self, rows_query=self._get_rows_query(sliced=False))

    def _get_exists_query(self) -> ExistsQuery:
        """Whether the query returns any row."""
        from hare.query.statements.summary.exists_query import ExistsQuery

        return ExistsQuery(self, rows_query=self._get_rows_query(sliced=False))

    def _get_aggregate_query(self, **kwargs: Expression | Term) -> AggregateQuery:
        """Aggregates over the rows the query returns. When each row is a row of the source queryset,
        this is the queryset's own ``aggregate()``; otherwise it runs over the query as a derived
        table and reads only the selected columns.

        Raises:
            QueryError: A metric over the derived table reads a column that isn't selected.
        """
        from hare.query.statements.summary.aggregate_query import AggregateQuery

        if (
            self._limit is None
            and not self._offset
            and ValuesGrouping.returns_one_row_per_source_row(self)
            and not RowMultiplication.reads_window_function(self)
        ):
            return self._source_queryset.aggregate(**kwargs)
        return AggregateQuery(self, kwargs, rows_query=self._get_rows_query(sliced=True))

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
            ValuesOutput.get_concrete_field_path(self, name) for name in ValuesOutput.get_output_field_names(self)
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
            multi_valued_names = [
                name
                for name in output_field_names
                if (
                    name in self._annotations
                    and name in RowMultiplication.get_row_multiplying_annotation_names(self, selected_only=True)
                )
                or (
                    name not in self._annotations
                    and AggregatedMultiValuedPaths.get_multi_valued_paths(self.model, name)
                )
            ]
            ordering_is_unique = self._ordering_field_names_are_unique(ordering_field_names)
            tie_breaker_names = [*([] if ordering_is_unique else primary_key_attribute_names), *multi_valued_names]
        return orderings + [
            (name, Order.ASC) for name in dict.fromkeys(tie_breaker_names) if name not in ordering_field_names
        ]

    def _get_keyset_readers(self) -> list[Callable[[Any], Any]] | None:
        """Readers of each ordering field's value off an output row, when every ordering field is
        a selected column of the model itself.

        Returns:
            The readers, or None for ``OFFSET`` paging.
        """
        if (
            self._distinct_on
            or RowMultiplication.reads_window_function(self)
            or ValuesOutput.selects_composite_key(self)
        ):
            # A keyset condition would change the rows a window function is computed over; a
            # composite key doesn't line up with the selected columns.
            return None
        readers: list[Callable[[Any], Any]] = []
        for field_name, _order in self._orderings:
            if field_name in self._annotations or field_name not in self.model._meta.direct_fields:
                return None
            reader = ValuesOutput.get_output_reader(self, field_name)
            if reader is None:
                return None
            readers.append(reader)
        return readers

    def _get_default_iteration_orderings(self) -> list[tuple[str, Order]]:
        """``Meta.ordering``, else the group key of a grouped query, else the primary key - like
        Django."""
        return list(self._apply_default_ordering(self._orderings, self._annotations)) or [
            (name, Order.ASC)
            for name in (
                ValuesGrouping.get_group_key_names(self)
                if ValuesGrouping.is_grouped(self)
                else self.model._meta.primary_key_attribute_names
            )
        ]

    async def _stream_batches(self, connection: DatabaseClient, chunk_size: int) -> AsyncGenerator[list[Any]]:
        value_fields = ValuesOutput.get_value_fields(self)
        column_converters = ValuesOutput.get_column_converters(self, value_fields)
        async with contextlib.aclosing(
            connection.stream_batches(*self._get_parameterized_sql(), chunk_size=chunk_size)
        ) as batches:
            async for rows in batches:
                # A batch of the driver's rows, read by name too.
                yield self._rows.convert_batch(
                    connection, StatementResult(len(rows), rows), column_converters, self.model, value_fields
                )

    async def _execute(self) -> Any:
        values_reading = self._values_reading
        if values_reading is None:
            values_reading = self.get_values_reading()
            plan = self._statement_plan
            if plan is not None and isinstance(plan.result_reading, dict):
                # A plan another row shape recorded (the same SQL text) - read this shape's rows from
                # it from now on too.
                plan.result_reading.setdefault(cast("ValuesSelection", self._selection).shape, values_reading)
        rows = await self._fetch_rows(
            self._connection,
            *self._get_parameterized_sql(),
            column_converters=values_reading.column_converters,
            value_fields=values_reading.value_fields,
        )
        return self._get_single_or_list_result(rows)

    def get_values_reading(self) -> ValuesReading:
        """How the rows of this query, built, are read.

        Returns:
            The reading.
        """
        value_fields = ValuesOutput.get_value_fields(self)
        return ValuesReading(self._rows, ValuesOutput.get_column_converters(self, value_fields), value_fields)

    def _get_call_signature_type_part(self) -> tuple[Any, list[Any]]:
        # The shape the rows are read in - the same calls read with another shape (named=True)
        # build the same SQL text, but the plan keeps how its rows are read.
        return (cast("ValuesSelection", self._selection).shape,), []

    def _restore_from_plan(self, plan: StatementPlan) -> None:
        # How each row shape the plan was run with reads its rows - another shape may share the SQL.
        readings_by_shape = plan.result_reading
        self._values_reading = readings_by_shape.get(cast("ValuesSelection", self._selection).shape)

    def _get_plan_record(self) -> dict[str, Any]:
        return {"result_reading": {cast("ValuesSelection", self._selection).shape: self.get_values_reading()}}

    def _get_selected_field_names(self) -> Collection[str]:
        return ValuesOutput.get_output_field_names(self)

    def _get_row_join_lookups(self) -> list[str]:
        return [
            *(
                field_name
                for field_name in (*ValuesOutput.get_output_field_names(self), *self._group_bys)
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
            (field_name for field_name in ValuesOutput.get_output_field_names(self) if isinstance(field_name, str)),
        )

    def _drop_ordering_of_unordered_subquery(self) -> None:
        """Unsliced, the order of a subquery's rows doesn't matter, so a ``.distinct()`` ordered by
        a field it doesn't select runs as a plain ``SELECT DISTINCT`` over the selected columns."""
        is_sliced = self._limit is not None or bool(self._offset)
        if (
            not is_sliced
            and not self._cursor_values
            and not self._before_cursor_values
            and DistinctFirstOccurrence.distinct_needs_first_occurrence_rows(
                self, ValuesOutput.get_output_field_names(self)
            )
        ):
            self._orderings = []
            self._default_ordering_disabled = True

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        """Describes this query built into another one (``_get_scoped_copy()``) - its slice is
        among its values.

        Args:
            context: The enclosing query's - this query resolves names against its own
                annotations.

        Returns:
            The description, None for a query that keeps no plan - one reading a sample, or a
            ``.distinct()`` built into a derived table after the build.
        """
        if self._options.keeps_no_plan():
            # Its percent and seed are written into FROM, not bound.
            return None
        query = self._get_scoped_copy()
        query._drop_ordering_of_unordered_subquery()
        output_aliases = ValuesOutput.get_selected_fields_by_alias(query)
        if DistinctFirstOccurrence.distinct_needs_first_occurrence_rows(query, output_aliases.values()):
            return None
        ValuesOutput.register_selected_annotations(query, output_aliases)
        return query._describe_statement(True)

    def _make_subquery(self, **kwargs: Any) -> None:
        """Builds ``self.query`` to be embedded in an enclosing query - see
        ``_drop_ordering_of_unordered_subquery()``.

        Args:
            kwargs: Passed on to ``_build_query()``.
        """
        orderings, default_ordering_disabled = self._orderings, self._default_ordering_disabled
        self._drop_ordering_of_unordered_subquery()
        try:
            self._make_query(**kwargs)
            output_aliases = list(ValuesOutput.get_output_aliases(self))
            if self._distinct_over_ordering_columns and len(self.query._selects) > len(output_aliases):
                # A copy - selecting from it sets its alias, and it can be the query a plan
                # keeps.
                inner_query = copy(self.query)
                self.query = self._connection.query_class.from_(inner_query).select(
                    *(cast("Field", inner_query[alias]).as_(alias) for alias in output_aliases)
                )
        finally:
            self._orderings, self._default_ordering_disabled = orderings, default_ordering_disabled
        self._apply_none_as_subquery()

    def _wrap_query_for_distinct_first_occurrence(self) -> None:
        """Keeps, per combination of the selected columns, the first row in the ordering, then orders
        and slices those rows: a derived table numbers the rows with ``ROW_NUMBER() OVER (PARTITION
        BY <selected columns> ORDER BY ...)`` and the outer query keeps number 1. A WITH clause
        moves to the outer query.
        """
        query_class = self._connection.query_class
        inner_query = copy(self.query)
        output_aliases = list(ValuesOutput.get_output_aliases(self))
        selected_aliases = {select_term.alias for select_term in inner_query._selects if select_term.alias}
        ordering_columns: list[tuple[str, Order | None]] = []
        for index, (term, order) in enumerate(inner_query._orderbys):
            if isinstance(term, Field) and term.table is None and term.name in selected_aliases:
                ordering_columns.append((term.name, order))
                continue
            ordering_alias = f"{DISTINCT_FIRST_OCCURRENCE_ORDERING_ALIAS_PREFIX}{index}"
            inner_query = inner_query.select(copy(term).as_(ordering_alias))
            ordering_columns.append((ordering_alias, order))
        inner_query._orderbys = []
        inner_query._limit = None
        inner_query._offset = None
        with_clauses, inner_query._with = inner_query._with, []

        row_number = RowNumber().over(*(inner_query[alias] for alias in output_aliases))
        for ordering_alias, order in ordering_columns:
            row_number = row_number.orderby(inner_query[ordering_alias], order=order)
        numbered_aliases = list(dict.fromkeys([*output_aliases, *(alias for alias, _order in ordering_columns)]))
        numbered_query = query_class.from_(inner_query).select(
            *(cast("Field", inner_query[alias]).as_(alias) for alias in numbered_aliases),
            row_number.as_(DISTINCT_FIRST_OCCURRENCE_ROW_NUMBER_ALIAS),
        )
        outer_query = (
            query_class.from_(numbered_query)
            .select(*(cast("Field", numbered_query[alias]).as_(alias) for alias in output_aliases))
            .where(
                BasicCriterion(
                    Equality.EQ,
                    numbered_query[DISTINCT_FIRST_OCCURRENCE_ROW_NUMBER_ALIAS],
                    ValueWrapper(1, allow_parametrize=False),
                )
            )
        )
        for ordering_alias, order in ordering_columns:
            outer_order = order.get_reversed() if self._first_occurrence_rows_reversed and order else order
            outer_query = outer_query.orderby(numbered_query[ordering_alias], order=outer_order)
        if self._limit is not None:
            outer_query._limit = outer_query._wrapper_class(self._limit)
        if self._offset is not None:
            outer_query._offset = outer_query._wrapper_class(self._offset)
        outer_query._with = with_clauses
        self.query = outer_query

    def _annotation_renders_as_expression(self, annotation_name: str) -> bool:
        """An annotation left out of values()/values_list() isn't selected either.

        Args:
            annotation_name: The annotation name.

        Returns:
            True when the annotation isn't among the selected fields.
        """
        selected_field_names = ValuesOutput.get_output_field_names(self)
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
        inner_query = self.query
        namespaced_context = inner_query._sql_context_with_namespace(inner_query.query_class.SQL_CONTEXT)
        aliasless_context = namespaced_context.copy(with_alias=False)
        selects = inner_query._mutable("_selects")
        alias_by_sql: dict[str, str] = {}
        for position, select_term in enumerate(selects):
            if not select_term.alias:
                select_term = copy(select_term).as_(f"{WINDOW_FILTER_COLUMN_ALIAS_PREFIX}{position}")
                selects[position] = select_term
            alias_by_sql.setdefault(
                WindowFilterWrapping.get_aliasless_sql(select_term, aliasless_context), cast("str", select_term.alias)
            )
        selected_aliases = {cast("str", select_term.alias) for select_term in selects}
        grouped_sql = {QueryGrouping.get_group_by_sql(term, namespaced_context) for term in inner_query._groupbys}

        def get_column(term: Term) -> Field:
            if isinstance(term, Field) and term.table is None and term.name in selected_aliases:
                return cast("Field", inner_query[term.name])
            term_sql = WindowFilterWrapping.get_aliasless_sql(term, aliasless_context)
            alias = alias_by_sql.get(term_sql)
            if alias is None:
                alias = f"{WINDOW_FILTER_COLUMN_ALIAS_PREFIX}{len(selects)}"
                alias_by_sql[term_sql] = alias
                selects.append(copy(term).as_(alias))
                if inner_query._groupbys:
                    for group_by_term in term.get_group_by_terms():
                        group_by_sql = QueryGrouping.get_group_by_sql(group_by_term, namespaced_context)
                        if group_by_sql not in grouped_sql:
                            grouped_sql.add(group_by_sql)
                            aliasless_term = copy(group_by_term)
                            aliasless_term.alias = None
                            inner_query._mutable("_groupbys").append(aliasless_term)
            return cast("Field", inner_query[alias])

        outer_criterion = WindowFilterWrapping.get_derived_table_criterion(
            cast("Criterion", self._window_filter_criterion), get_column
        )
        order_columns = [(get_column(term), order) for term, order in inner_query._orderbys]
        distinct_on_columns = [get_column(term) for term in inner_query._distinct_on]
        output_aliases = list(ValuesOutput.get_output_aliases(self))
        outer_query = self._connection.query_class.from_(inner_query).select(
            *(cast("Field", inner_query[alias]).as_(alias) for alias in output_aliases)
        )
        outer_query = outer_query.where(outer_criterion)
        if inner_query._distinct:
            outer_query._distinct = True
            outer_aliases = set(output_aliases)
            for order_column, _order in order_columns:
                # SELECT DISTINCT must select what it orders by, as the inner query did.
                if order_column.name not in outer_aliases:
                    outer_aliases.add(order_column.name)
                    outer_query = outer_query.select(order_column)
            inner_query._distinct = False
        if distinct_on_columns:
            outer_query = outer_query.distinct_on(*distinct_on_columns)
            inner_query._distinct_on = []
        for order_column, order in order_columns:
            outer_query = outer_query.orderby(order_column, order=order)
        inner_query._orderbys = []
        outer_query._limit, outer_query._offset = inner_query._limit, inner_query._offset
        inner_query._limit = inner_query._offset = None
        outer_query._with, inner_query._with = inner_query._with, []
        self.query = outer_query

    def _prepare_build(self) -> None:
        output_aliases = ValuesOutput.get_selected_fields_by_alias(self)
        # Decided by the plan key's parts alone (distinct/distinct_on/orderings/the selected
        # fields), so it's the same running on a plan and built in full - see
        # DistinctFirstOccurrence.distinct_needs_first_occurrence_rows()'s own docstring.
        self._distinct_requires_first_occurrence_rows = DistinctFirstOccurrence.distinct_needs_first_occurrence_rows(
            self, output_aliases.values()
        )
        DistinctFirstOccurrence.raise_if_distinct_over_encrypted_field(self, output_aliases.values())
        # Kept as given: the registration below registers each selected annotation again under
        # its alias - the build resolves the selected fields from the original ones.
        self._own_annotations = self._annotations
        # Before the query is described: the build records the values against the annotations
        # after this registration, so the description lists them against the same ones. The
        # registration is a pure function of the selected fields and the originating annotations.
        ValuesOutput.register_selected_annotations(self, output_aliases)

    def _keeps_plan_built_into_another(self) -> bool:
        return not self._distinct_requires_first_occurrence_rows and super()._keeps_plan_built_into_another()

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
        self._annotations = self._own_annotations
        self.query = self._get_base_query()
        QueryJoins.apply_effective_basetable(self)
        for alias, field in ValuesOutput.get_selected_fields_by_alias(self).items():
            ValuesOutput.add_field_to_select_query(self, field, alias)

    def _apply_ordering(self) -> None:
        own_annotations = self._own_annotations
        # QueryOrdering.get_ordering() needs each selected annotation's alias, so an ORDER BY reference uses
        # the name that's actually in SELECT.
        annotation_output_aliases = {
            field: alias
            for alias, field in ValuesOutput.get_selected_fields_by_alias(self).items()
            if field in own_annotations
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
        column_converters: list[tuple[str, Callable[[Any], Any] | None]],
        value_fields: tuple[ValueField, ...] | None = None,
    ) -> list[Any]:
        """Runs a query selecting this query's columns and builds the output rows.

        Args:
            connection: The connection.
            sql: The SQL.
            parameters: Its parameters.
            column_converters: Each column's alias and decoder.
            value_fields: What each column is read as - for reading the rows in one
                ``rust.native.rows`` call; None reads them in Python.

        Returns:
            The output rows.
        """
        return await self._rows.fetch(connection, sql, parameters, column_converters, self.model, value_fields)

    def _convert_row(
        self, row: dict[str, Any], column_converters: list[tuple[str, Callable[[Any], Any] | None]]
    ) -> Any:
        """Builds one output row.

        Args:
            row: The fetched row, by column alias.
            column_converters: Each column's alias and decoder.

        Returns:
            The output row.
        """
        return self._rows.convert(row, column_converters)
