from __future__ import annotations

import contextlib
from collections.abc import (
    AsyncGenerator,
    Callable,
    Collection,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from copy import copy
from dataclasses import replace
from typing import TYPE_CHECKING, Any, ClassVar, Self, TypeVar, cast

from hare.dialects.base.client.database_client import DatabaseClient
from hare.dialects.base.client.transaction_client import TransactionClient
from hare.exceptions import (
    FieldError,
    QueryError,
)
from hare.fields.encrypted.encrypted_field_mixin import EncryptedFieldMixin
from hare.models.enums import FieldBucket
from hare.query.constants import (
    DISTINCT_FIRST_OCCURRENCE_ORDERING_ALIAS_PREFIX,
    DISTINCT_FIRST_OCCURRENCE_ROW_NUMBER_ALIAS,
    WINDOW_FILTER_COLUMN_ALIAS_PREFIX,
)
from hare.query.enums import RowShape
from hare.query.expressions import Expression
from hare.query.expressions.aggregate_paths.aggregated_multi_valued_paths import AggregatedMultiValuedPaths
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.queryset.query_spec import QuerySpec
from hare.query.queryset.values_selection import ValuesSelection
from hare.query.rows.dict_rows import DictRows
from hare.query.rows.flat_rows import FlatRows
from hare.query.rows.named_rows import NamedRows
from hare.query.rows.tuple_rows import TupleRows
from hare.query.rows.value_field import ValueField
from hare.query.rows.values_rows import ValuesRows
from hare.query.statements.awaitable_query import AwaitableQuery
from hare.query.statements.select.select_query import SelectQuery
from hare.sql import Order
from hare.sql.analytics.declarations import RowNumber
from hare.sql.enums import Equality
from hare.sql.queries.tables.selectable import Selectable
from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.field import Field
from hare.sql.terms.functions.aggregate_function import AggregateFunction
from hare.sql.terms.functions.function import Function
from hare.sql.terms.tuple import Tuple

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.base.field import Field as ModelField
    from hare.models import Model
    from hare.query.queryset.queryset import QuerySet
    from hare.query.statements.summary.aggregate_query import AggregateQuery
    from hare.query.statements.summary.count_query import CountQuery
    from hare.query.statements.summary.exists_query import ExistsQuery
    from hare.query.statements.write.update_query import UpdateQuery
    from hare.sql.context import SqlContext

TModel = TypeVar("TModel", bound="Model")


class ValuesQuery(SelectQuery[Any]):
    """Builds and runs the SQL of a queryset returning the values ``.values()``/``.values_list()``
    select - made from the queryset for one build. A row is read by the shape the call asked for:
    a dict by output key, a tuple, the one value alone or a namedtuple."""

    #: The class reading a row of each shape.
    ROWS_CLASSES_BY_SHAPE: ClassVar[dict[RowShape, type[ValuesRows]]] = {
        RowShape.DICT: DictRows,
        RowShape.TUPLE: TupleRows,
        RowShape.FLAT: FlatRows,
        RowShape.NAMED: NamedRows,
    }

    #: Criterion classes that are operands (a column, a function call, a value list) rather than
    #: conditions - rewritten as a whole into a column of that derived table.
    WINDOW_FILTER_OPERAND_CRITERION_TYPES: tuple[type, ...] = (Field, Function, Tuple)

    #: values()/values_list() group an aggregate added by or after them by exactly the fields named
    #: - see AwaitableQuery.group_by_must_include_primary_key and _aggregates_per_model_row().
    group_by_must_include_primary_key: ClassVar[bool] = False
    #: A filter on a window function applies to the query wrapped as a derived table - see
    #: _wrap_query_for_window_filter().
    window_filter_wrapping_supported: ClassVar[bool] = True

    # pylint: disable=W0223

    @staticmethod
    def _get_composite_key_components(model: type[Model], name: str, annotations: dict[str, Any]) -> tuple[str, ...]:
        """The field paths of a selected name reading a composite key - ``pk`` of a composite
        primary key, or a relation to one (``target``, ``target__pk``, ``tags``).

        Args:
            model: The queried model.
            name: The selected name.
            annotations: The query's annotations.

        Returns:
            The key's field paths in key order, empty when the name reads one column.
        """
        if name in annotations:
            return ()
        field_paths = AwaitableQuery.get_concrete_field_paths(model, name)
        return field_paths if len(field_paths) > 1 else ()

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
        QuerySpec.copy_spec(source_queryset, self)
        self._init_build_state()
        self._annotations = annotations
        self._alias_keys = alias_keys
        self._selection = None
        # Set fresh by _make_query() on every call - see _distinct_needs_first_occurrence_rows().
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
            components = self._get_composite_key_components(self.model, field_name, annotations) or (field_name,)
            column_aliases = rows_class.get_column_aliases(
                output_name, len(self._selected_fields_by_alias), len(components)
            )
            self._selected_fields_by_alias.update(zip(column_aliases, components, strict=True))
            output_column_aliases.append(column_aliases)
        #: How a row is read - by the shape the call asked for.
        self._rows: ValuesRows = rows_class(
            tuple(output_name for output_name, _field_name in outputs), output_column_aliases
        )
        returns_dicts = rows_class.keyed_by_output_name
        # What get_filters() selects for a tuple row: the selected fields and every positional
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

    def _get_grouped_source_queryset(self) -> QuerySet[Any]:
        """The source queryset, explicitly grouped by the selected fields an aggregate implicitly
        groups this query by.

        Returns:
            The queryset.
        """
        source_queryset = self._source_queryset
        if self._group_bys or not self._is_grouped():
            return source_queryset
        group_key_names = self._get_group_key_names()
        return source_queryset.group_by(*group_key_names) if group_key_names else source_queryset

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
        source_queryset = self._source_queryset
        if reverse and self._distinct and not self._distinct_on and not self._is_grouped():
            ordering_field_names = [
                field_name for field_name, _order in self._apply_default_ordering(self._orderings, self._annotations)
            ]
            if not ordering_field_names:
                ordering_field_names = list(self.model._meta.pk_attr_names)
                source_queryset = source_queryset._with_orderings(*ordering_field_names)
            output_field_names = {self._get_concrete_field_path(name) for name in self._get_output_field_names()}
            if any(field_name not in output_field_names for field_name in ordering_field_names):
                # Reversing the ordering would pick another row of each combination of the selected
                # columns - the last row is the first-occurrence rows' last one instead.
                self._raise_if_sliced_rows_differ_from_source("last")
                queryset = source_queryset._with_selection(
                    replace(cast("ValuesSelection", self._selection), first_occurrence_rows_reversed=True)
                )
                return cast("QuerySet[Any, Any]", queryset._as_single())
        if not self._apply_default_ordering(self._orderings, self._annotations) and self._is_grouped():
            group_key_names = self._get_group_key_names()
            ordering_names = [f"-{name}" if reverse else name for name in group_key_names]
            if reverse:
                self._raise_if_sliced_rows_differ_from_source("last")
            queryset = self._apply_values_to(
                source_queryset._with_orderings(*ordering_names) if ordering_names else source_queryset
            )
            return cast("QuerySet[Any, Any]", queryset._as_single())
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
        if (self._limit is not None or self._offset) and not self._returns_one_row_per_source_row():
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

    def _is_grouped(self) -> bool:
        """Whether the rows are groups - an explicit ``.group_by()``, or an aggregate added after
        ``.values()``/``.values_list()``, which groups by the selected columns. An aggregate
        annotated before them is computed per model row, like Django.

        Returns:
            True for a grouped query.
        """
        if self._group_bys:
            return True
        unused_alias_keys = self._get_unused_alias_keys(self._get_output_field_names())
        return any(
            self._annotation_is_aggregate(annotation)
            for name, annotation in self._annotations.items()
            if name in self._grouping_annotation_names and name not in unused_alias_keys
        )

    def _aggregates_per_model_row(self) -> bool:
        """Whether every aggregate the query computes was annotated before
        ``.values()``/``.values_list()`` or passed to them - the rows are then model rows, grouped by
        the primary key too, like Django.

        Returns:
            True when the aggregates are computed per model row.
        """
        return not self._group_bys and not self._is_grouped()

    def _annotation_is_aggregate(self, annotation: Expression | Term) -> bool:
        """Whether an annotation resolves to an aggregate term.

        Args:
            annotation: The annotation.

        Returns:
            True for an aggregate.
        """
        return self._get_annotation_term(annotation).contains_aggregate

    def _get_annotation_term(self, annotation: Expression | Term) -> Term:
        """The term an annotation resolves to.

        Args:
            annotation: The annotation.

        Returns:
            The term.
        """
        if isinstance(annotation, Term) and not isinstance(annotation, Expression):
            return annotation
        return annotation.get_result(self._get_probe_expression_context(self._annotations)).term

    def _annotation_is_group_key(self, annotation: Expression | Term) -> bool:
        """Whether a selected annotation is part of the group key - neither an aggregate nor a
        window function, which are computed per group.

        Args:
            annotation: The annotation.

        Returns:
            True for a group key.
        """
        term = self._get_annotation_term(annotation)
        return not term.contains_aggregate and not self._term_reads_window_function(term)

    def _get_group_key_names(self) -> list[str]:
        """The names the rows are grouped by - the ``.group_by()`` fields, else the selected
        fields and the annotations that are neither aggregates nor window functions.

        Returns:
            The names.
        """
        if self._group_bys:
            return list(self._group_bys)
        return [
            name
            for name in self._get_output_field_names()
            if name not in self._annotations or self._annotation_is_group_key(self._annotations[name])
        ]

    def _returns_one_row_per_source_row(self) -> bool:
        """Whether each row stands for one row of the source queryset - not grouped, not
        deduplicated over the selected columns, and no selected field, ordering or selected
        annotation crossing a to-many relation.

        Returns:
            True when the rows are the source queryset's own rows.
        """
        if (self._distinct and not self._distinct_on) or self._is_grouped():
            return False
        selected_field_lookups = self._get_field_lookups(
            name for name in self._get_output_field_names() if name not in self._annotations
        )
        if any(
            AggregatedMultiValuedPaths.get_multi_valued_paths(self.model, lookup)
            for lookup in (*selected_field_lookups, *self._get_ordering_lookups())
        ):
            return False
        return not self._get_row_multiplying_annotation_names(selected_only=False)

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
            and self._returns_one_row_per_source_row()
            and not self._reads_window_function()
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
        if self._is_grouped():
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
        pk_attr_names = list(self.model._meta.pk_attr_names)
        output_field_names = [self._get_concrete_field_path(name) for name in self._get_output_field_names()]
        if self._is_grouped():
            tie_breaker_names = self._get_group_key_names()
        elif self._distinct or self._distinct_on:
            tie_breaker_names = pk_attr_names if set(pk_attr_names) <= set(output_field_names) else output_field_names
        else:
            multi_valued_names = [
                name
                for name in output_field_names
                if (
                    name in self._annotations
                    and name in self._get_row_multiplying_annotation_names(selected_only=True)
                )
                or (
                    name not in self._annotations
                    and AggregatedMultiValuedPaths.get_multi_valued_paths(self.model, name)
                )
            ]
            ordering_is_unique = self._ordering_field_names_are_unique(ordering_field_names)
            tie_breaker_names = [*([] if ordering_is_unique else pk_attr_names), *multi_valued_names]
        return orderings + [
            (name, Order.ASC) for name in dict.fromkeys(tie_breaker_names) if name not in ordering_field_names
        ]

    def _get_keyset_readers(self) -> list[Callable[[Any], Any]] | None:
        """Readers of each ordering field's value off an output row, when every ordering field is
        a selected column of the model itself.

        Returns:
            The readers, or None for ``OFFSET`` paging.
        """
        if self._distinct_on or self._reads_window_function() or self._selects_composite_key():
            # A keyset condition would change the rows a window function is computed over; a
            # composite key doesn't line up with the selected columns.
            return None
        readers: list[Callable[[Any], Any]] = []
        for field_name, _order in self._orderings:
            if field_name in self._annotations or field_name not in self.model._meta.direct_fields:
                return None
            reader = self._get_output_reader(field_name)
            if reader is None:
                return None
            readers.append(reader)
        return readers

    def _get_default_iteration_orderings(self) -> list[tuple[str, Order]]:
        """``Meta.ordering``, else the group key of a grouped query, else the primary key - like
        Django."""
        return list(self._apply_default_ordering(self._orderings, self._annotations)) or [
            (name, Order.ASC)
            for name in (self._get_group_key_names() if self._is_grouped() else self.model._meta.pk_attr_names)
        ]

    async def _stream_batches(self, db: TransactionClient, chunk_size: int) -> AsyncGenerator[list[Any]]:
        value_fields = self._get_value_fields()
        column_converters = self._get_column_converters(value_fields)
        async with contextlib.aclosing(
            db.stream_batches(*self._get_parameterized_sql(), chunk_size=chunk_size)
        ) as batches:
            async for batch in batches:
                rows = batch if type(batch) is list else list(batch)
                yield self._rows.convert_batch(db, rows, column_converters, self.model, value_fields)

    def _get_value_fields(self) -> tuple[ValueField, ...]:
        """What each selected column is read as.

        Returns:
            The value fields, in output order.
        """
        return tuple(self.get_value_field(self.model, name) for name in self._get_output_field_names())

    def _get_column_converters(
        self, value_fields: tuple[ValueField, ...] | None = None
    ) -> list[tuple[str, Callable[[Any], Any] | None]]:
        """Each selected column's alias and the function decoding its value, None when the raw
        value is used as-is.

        Args:
            value_fields: What each column is read as, when already known.

        Returns:
            The converters, in output order.
        """
        if value_fields is None:
            value_fields = self._get_value_fields()
        python_reader = self.dialect.types.get_python_reader
        return [
            (alias, None if value_field.is_native or value_field.field is None else python_reader(value_field.field))
            for alias, value_field in zip(self._get_output_aliases(), value_fields, strict=True)
        ]

    async def _execute(self) -> Any:
        value_fields = self._get_value_fields()
        rows = await self._fetch_rows(
            self._db,
            *self._get_parameterized_sql(),
            column_converters=self._get_column_converters(value_fields),
            value_fields=value_fields,
        )
        return self._get_single_or_list_result(rows)

    def _get_output_value_field(self, field_name: str) -> ModelField[Any] | None:
        """The field a selected field or annotation decodes through, once the query is built.

        Args:
            field_name: The selected name.

        Returns:
            The field, or None when unknown.
        """
        from hare.query.functions.aggregates.count import Count

        field_name = self._get_concrete_field_path(field_name)
        if field_name in self._annotations:
            if (output_field := self._annotation_output_fields.get(field_name)) is not None:
                return output_field
            if isinstance(self._annotations[field_name], Count):
                return Count.COUNT_OUTPUT_FIELD  # type:ignore[arg-type]
            return None
        return self._get_field_object_by_path(field_name)

    def _get_selected_field_names(self) -> Collection[str]:
        return self._get_output_field_names()

    def _get_row_join_lookups(self) -> list[str]:
        return [
            *(
                field_name
                for field_name in (*self._get_output_field_names(), *self._group_bys)
                if isinstance(field_name, str) and field_name not in self._annotations
            ),
            *self._get_ordering_lookups(),
        ]

    def _get_group_key_lookups(self) -> list[str]:
        """The explicit ``.group_by()`` fields, or else the selected fields an aggregate is
        implicitly grouped by.

        Returns:
            The lookups, annotations left out.
        """
        if self._group_bys:
            return super()._get_group_key_lookups()
        return self._get_field_lookups(
            field_name for field_name in self._get_output_field_names() if isinstance(field_name, str)
        )

    def _drop_ordering_of_unordered_subquery(self) -> None:
        """Unsliced, the order of a subquery's rows doesn't matter, so a ``.distinct()`` ordered by
        a field it doesn't select runs as a plain ``SELECT DISTINCT`` over the selected columns."""
        is_sliced = self._limit is not None or bool(self._offset)
        if (
            not is_sliced
            and not self._cursor_values
            and not self._before_cursor_values
            and self._distinct_needs_first_occurrence_rows(self._get_output_field_names())
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
            The description, None for a query that keeps no plan - one with a ``select_related()``
            extra condition, or a ``.distinct()`` built into a derived table after the build.
        """
        query = self._get_scoped_copy()
        if query._select_related_extra_conditions:
            return None
        query._drop_ordering_of_unordered_subquery()
        output_aliases = query._get_selected_fields_by_alias()
        if query._distinct_needs_first_occurrence_rows(output_aliases.values()):
            return None
        query._register_selected_annotations(output_aliases)
        description = query._get_values_plan_description(connection_bound=False)
        if description is None:
            return None
        slice_bounds = [bound for bound in (query._limit, query._offset) if bound is not None]
        return PlanDescription((*description.structure, query._is_none), description.values + slice_bounds)

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
            output_aliases = list(self._get_output_aliases())
            if self._distinct_over_ordering_columns and len(self.query._selects) > len(output_aliases):
                # A copy - selecting from it sets its alias, and it can be the query a plan
                # keeps.
                inner_query = copy(self.query)
                self.query = self._db.query_class.from_(inner_query).select(
                    *(cast("Field", inner_query[alias]).as_(alias) for alias in output_aliases)
                )
        finally:
            self._orderings, self._default_ordering_disabled = orderings, default_ordering_disabled
        self._apply_none_as_subquery()

    def _distinct_needs_first_occurrence_rows(self, output_field_names: Collection[str]) -> bool:
        """Whether a plain ``.distinct()`` is ordered by a field it doesn't select. ``SELECT DISTINCT``
        would then deduplicate over the ordering columns too - instead the query keeps the first row
        of each combination of the selected columns.

        Args:
            output_field_names: The field/annotation names visible to the caller.

        Returns:
            True when the query needs the first-occurrence rows.
        """
        if not self._distinct or self._distinct_on or self._distinct_over_ordering_columns:
            return False
        orderings = self._apply_default_ordering(self._orderings, self._annotations)
        return any(field_name not in output_field_names for field_name, _order in orderings)

    def _wrap_query_for_distinct_first_occurrence(self) -> None:
        """Keeps, per combination of the selected columns, the first row in the ordering, then orders
        and slices those rows: a derived table numbers the rows with ``ROW_NUMBER() OVER (PARTITION
        BY <selected columns> ORDER BY ...)`` and the outer query keeps number 1. A WITH clause
        moves to the outer query.
        """
        query_class = self._db.query_class
        inner_query = copy(self.query)
        output_aliases = list(self._get_output_aliases())
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
            outer_query._limit = outer_query._wrapper_cls(self._limit)
        if self._offset is not None:
            outer_query._offset = outer_query._wrapper_cls(self._offset)
        outer_query._with = with_clauses
        self.query = outer_query

    def _raise_if_distinct_over_encrypted_field(self, output_field_names: Collection[str]) -> None:
        """Rejects ``.distinct()`` over a selected encrypted field, unless the primary key is
        selected too (it already makes every row distinct).

        Args:
            output_field_names: The field/annotation names actually visible to the caller.

        Raises:
            FieldError: DISTINCT would compare an encrypted field's ciphertext.
        """
        if not self._distinct:
            return
        pk_attr = self.model._meta.pk_attr
        selected_field_names = set(output_field_names)
        if isinstance(pk_attr, tuple):
            if set(pk_attr) <= selected_field_names:
                return
        elif selected_field_names & {pk_attr, "pk"}:
            return
        for field_name in output_field_names:
            EncryptedFieldMixin.raise_if_encrypted(self._get_field_object_by_path(field_name), "DISTINCT")

    def _annotation_renders_as_expression(self, annotation_name: str) -> bool:
        """An annotation left out of values()/values_list() isn't selected either.

        Args:
            annotation_name: The annotation name.

        Returns:
            True when the annotation isn't among the selected fields.
        """
        selected_field_names = self._get_output_field_names()
        return annotation_name not in selected_field_names or super()._annotation_renders_as_expression(
            annotation_name
        )

    def _get_concrete_field_path(self, field: str) -> str:
        """The field path a selected name reads - an annotation name itself, else a trailing
        ``pk`` or relation translated like ``AwaitableQuery.get_concrete_field_path()`` does
        (``pk`` -> ``id``, ``dept`` -> ``dept_id``, ``tags`` -> ``tags__id``).

        Args:
            field: The selected name.

        Returns:
            The field path or annotation name.

        Raises:
            QueryError: The name reads a composite primary key or foreign key.
        """
        if field in self._annotations:
            return field
        return self.get_concrete_field_path(self.model, field)

    def add_field_to_select_query(self, field: str, return_as: str) -> None:
        field = self._get_concrete_field_path(field)
        table = self._effective_basetable()

        if field in self._annotations:
            # A dict of its own: the annotations are the originating queryset's.
            self._annotations = {**self._annotations, return_as: self._annotations[field]}
            return

        if field in self.model._meta.fields_db_projection:
            db_field = self.model._meta.fields_db_projection[field]
            self.query._select_field(table[db_field].as_(return_as))
            return

        field_, __, forwarded_fields = field.partition("__")
        if field_ in self.model._meta.fetch_fields:
            related_table, related_db_field = self._join_table_with_forwarded_fields(
                model=self.model,
                table=table,
                field=field_,
                forwarded_fields=forwarded_fields,
            )
            self.query._select_field(related_table[related_db_field].as_(return_as))
            return

        raise FieldError(f'Unknown field "{field}" for model "{self.model.__name__}"')

    def get_value_field(self, model: type[TModel], field: str) -> ValueField:
        """What a selected name's value is read as.

        Args:
            model: The model the name is a path from.
            field: The name.

        Returns:
            The model and name of a model field (the model None for an annotation), with the field
            the value is decoded through - None for a value used as the driver returns it.

        Raises:
            FieldError: The name is neither a field nor an annotation.
        """
        if model is self.model:
            field = self._get_concrete_field_path(field)

        if field in model._meta.fetch_fields:
            # return as is to get whole model objects
            return ValueField(None, field, None, is_native=True)

        layout_entry = model._meta.get_hydration_layout(self._db).entry_by_field_name.get(field)
        if layout_entry is not None and layout_entry[2] == FieldBucket.NATIVE:
            return ValueField(model, field, layout_entry[1], is_native=True)

        if field in self._annotations:
            # Read from this build's own dict - the annotation expression is shared between queries.
            field_object = self._annotation_output_fields.get(field)
            return ValueField(None, field, field_object or None, is_native=not field_object)

        if field in model._meta.fields_map:
            return ValueField(model, field, model._meta.fields_map[field], is_native=False)

        field_, __, forwarded_fields = field.partition("__")
        if field_ in model._meta.fetch_fields:
            new_model = model._meta.fields_map[field_].related_model  # type: ignore[attr-defined]
            return self.get_value_field(new_model, forwarded_fields)

        raise FieldError(f'Unknown field "{field}" for model "{model}"')

    def get_value_reader(self, model: type[TModel], field: str) -> Callable[[Any], Any]:
        """The function decoding a selected name's value.

        Args:
            model: The model the name is a path from.
            field: The name.

        Returns:
            The reader - a lambda returning the value itself for a value used as the driver
            returns it.
        """
        value_field = self.get_value_field(model, field)
        if value_field.is_native or value_field.field is None:
            return lambda x: x
        return self.dialect.types.get_python_reader(value_field.field)

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
        namespaced_context = inner_query._sql_context_with_namespace(inner_query.QUERY_CLS.SQL_CONTEXT)
        aliasless_context = namespaced_context.copy(with_alias=False)
        selects = inner_query._mutable("_selects")
        alias_by_sql: dict[str, str] = {}
        for position, select_term in enumerate(selects):
            if not select_term.alias:
                select_term = copy(select_term).as_(f"{WINDOW_FILTER_COLUMN_ALIAS_PREFIX}{position}")
                selects[position] = select_term
            alias_by_sql.setdefault(
                self._get_aliasless_sql(select_term, aliasless_context), cast("str", select_term.alias)
            )
        selected_aliases = {cast("str", select_term.alias) for select_term in selects}
        grouped_sql = {self._get_group_by_sql(term, namespaced_context) for term in inner_query._groupbys}

        def get_column(term: Term) -> Field:
            if isinstance(term, Field) and term.table is None and term.name in selected_aliases:
                return cast("Field", inner_query[term.name])
            term_sql = self._get_aliasless_sql(term, aliasless_context)
            alias = alias_by_sql.get(term_sql)
            if alias is None:
                alias = f"{WINDOW_FILTER_COLUMN_ALIAS_PREFIX}{len(selects)}"
                alias_by_sql[term_sql] = alias
                selects.append(copy(term).as_(alias))
                if inner_query._groupbys:
                    for group_by_term in term.get_group_by_terms():
                        group_by_sql = self._get_group_by_sql(group_by_term, namespaced_context)
                        if group_by_sql not in grouped_sql:
                            grouped_sql.add(group_by_sql)
                            aliasless_term = copy(group_by_term)
                            aliasless_term.alias = None
                            inner_query._mutable("_groupbys").append(aliasless_term)
            return cast("Field", inner_query[alias])

        outer_criterion = self._get_derived_table_criterion(
            cast("Criterion", self._window_filter_criterion), get_column
        )
        order_columns = [(get_column(term), order) for term, order in inner_query._orderbys]
        distinct_on_columns = [get_column(term) for term in inner_query._distinct_on]
        output_aliases = list(self._get_output_aliases())
        outer_query = self._db.query_class.from_(inner_query).select(
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

    @staticmethod
    def _get_aliasless_sql(term: Term, aliasless_context: SqlContext) -> str:
        """A term's SQL without its alias, to recognize the same term selected twice.

        Args:
            term: The term.
            aliasless_context: The query's namespaced SQL context, rendering no alias.

        Returns:
            The SQL text.
        """
        aliasless_term = term
        if term.alias is not None:
            aliasless_term = copy(term)
            aliasless_term.alias = None
        return aliasless_term.get_sql(aliasless_context)

    @classmethod
    def _get_derived_table_criterion(cls, term: Term, get_column: Callable[[Term], Field]) -> Term:
        """Rewrites a criterion of the inner query into one over its derived table's columns -
        every operand reading a row becomes the column selecting it.

        Args:
            term: The criterion, or an operand of it.
            get_column: Returns the derived table's column selecting an inner query term.

        Returns:
            The rewritten term.
        """
        if (
            isinstance(term, Criterion)
            and not isinstance(term, ValuesQuery.WINDOW_FILTER_OPERAND_CRITERION_TYPES)
            and not term.is_subquery
        ):
            rewritten_criterion = copy(term)
            for attribute_name, attribute_value in vars(term).items():
                if isinstance(attribute_value, Term):
                    setattr(
                        rewritten_criterion,
                        attribute_name,
                        cls._get_derived_table_criterion(attribute_value, get_column),
                    )
            return rewritten_criterion
        if not cls._term_reads_row(term):
            return term
        return get_column(term)

    @staticmethod
    def _term_reads_row(term: Term) -> bool:
        """Whether a term's value depends on the row - a column, an aggregate, a window function
        or a correlated subquery - rather than being a constant.

        Args:
            term: The term.

        Returns:
            True when the term reads the row.
        """
        boundary_node_ids: set[int] = set()
        nodes: Iterator[Any] = term.nodes_()
        for node in nodes:
            if id(node) in boundary_node_ids:
                continue
            if node.is_subquery or isinstance(node, Selectable):
                if getattr(node, "outer_reference_terms", ()):
                    return True
                boundary_node_ids.update(map(id, node.nodes_()))
            elif isinstance(node, (Field, AggregateFunction)) or node.is_analytic:
                return True
        return False

    def _register_selected_annotations(self, output_aliases: Mapping[str, str]) -> None:
        """Registers each selected annotation again under its alias - the part of
        ``add_field_to_select_query()`` that builds nothing, in the same order, so the query can be
        described before the selected fields are resolved.

        Args:
            output_aliases: Each selected field name by its alias.
        """
        for alias, field in output_aliases.items():
            field = self._get_concrete_field_path(field)
            if field in self._annotations:
                self._annotations = {**self._annotations, alias: self._annotations[field]}

    def _prepare_build(self) -> None:
        output_aliases = self._get_selected_fields_by_alias()
        # Decided by the plan key's parts alone (distinct/distinct_on/orderings/the selected
        # fields), so it's the same running on a plan and built in full - see
        # _distinct_needs_first_occurrence_rows()'s own docstring.
        self._distinct_requires_first_occurrence_rows = self._distinct_needs_first_occurrence_rows(
            output_aliases.values()
        )
        self._raise_if_distinct_over_encrypted_field(output_aliases.values())
        # Kept as given: the registration below registers each selected annotation again under
        # its alias - the build resolves the selected fields from the original ones.
        self._own_annotations = self._annotations
        # Before the query is described: the build records the values against the annotations
        # after this registration, so the description lists them against the same ones. The
        # registration is a pure function of the selected fields and the originating annotations.
        self._register_selected_annotations(output_aliases)

    def _keeps_plan_built_into_another(self) -> bool:
        return (
            not self._select_related_extra_conditions
            and not self._distinct_requires_first_occurrence_rows
            and super()._keeps_plan_built_into_another()
        )

    def _get_build_plan_description(self) -> PlanDescription | None:
        # A select_related() extra condition is no part of the key.
        if self._select_related_extra_conditions:
            return None
        return self._get_values_plan_description()

    def _select_columns(self) -> None:
        # The selected fields are resolved from the annotations as given: the registration runs
        # again, in order, alongside each field's own column.
        self._annotations = self._own_annotations
        self.query = self._get_base_query()
        self._apply_effective_basetable()
        for alias, field in self._get_selected_fields_by_alias().items():
            self.add_field_to_select_query(field, alias)

    def _apply_ordering(self) -> None:
        own_annotations = self._own_annotations
        # get_ordering() needs each selected annotation's alias, so an ORDER BY reference uses
        # the name that's actually in SELECT.
        annotation_output_aliases = {
            field: alias for alias, field in self._get_selected_fields_by_alias().items() if field in own_annotations
        }
        self.get_ordering(
            model=self.model,
            table=self._effective_basetable(),
            orderings=self._orderings,
            annotations=self._annotations,
            fields_for_select=self._get_ordering_field_names(annotation_output_aliases),
            annotation_output_aliases=annotation_output_aliases,
        )

    def _get_explicit_group_bys(self) -> tuple[str, ...]:
        return self._group_bys

    def _get_selection_plan_description(
        self, query_class: type, *field_parts: Any, connection_bound: bool = True
    ) -> PlanDescription | None:
        """Describes a ``.values()``/``.values_list()`` query on top of
        ``_get_query_plan_description()``: the selected fields, the grouping, the ordering, whether
        the rows are sliced and the ``select_for_update()`` settings.

        Args:
            query_class: ``ValuesQuery`` or ``ValuesListQuery``.
            field_parts: The selected fields, as the class keeps them.
            connection_bound: False for a subquery.

        Returns:
            The description, None when a part of the query keeps no plan.
        """
        return self._get_query_plan_description(
            query_class,
            *field_parts,
            self._group_bys,
            tuple(sorted(self._grouping_annotation_names.intersection(self._annotations))),
            tuple(self._orderings),
            self._distinct_over_ordering_columns,
            # last() of a first-occurrence .distinct() orders the outer query backwards.
            self._first_occurrence_rows_reversed,
            self._default_ordering_disabled,
            self._limit is not None,
            self._offset is not None,
            self._select_for_update,
            self._select_for_update_nowait,
            self._select_for_update_skip_locked,
            tuple(sorted(self._select_for_update_of)),
            self._select_for_update_no_key,
            describes_cursor=True,
            connection_bound=connection_bound,
        )

    def _get_output_field_names(self) -> Collection[str]:
        """The field/annotation names visible to the caller, one per selected column."""
        return self._selected_fields_by_alias.values()

    def _selects_composite_key(self) -> bool:
        """Whether a selected name returns a composite key, combined from several columns."""
        return self._rows.has_composite_outputs

    def _get_output_aliases(self) -> Collection[str]:
        """The SELECT aliases of the caller-visible columns, in output order."""
        return self._selected_fields_by_alias.keys()

    def _get_output_names_for_set_operation(self) -> list[str]:
        """The names a set operation's rows are ordered by - the dict keys, or the selected
        names of a ``values_list()``.

        Returns:
            The names, in output order.
        """
        if self._rows.keyed_by_output_name:
            return list(self._selected_fields_by_alias)
        return list(self._selected_fields_by_alias.values())

    def _get_selected_fields_by_alias(self) -> Mapping[str, str]:
        """Each selected field or annotation name by the alias its column is selected under."""
        return self._selected_fields_by_alias

    def _get_values_plan_description(self, connection_bound: bool = True) -> PlanDescription | None:
        """Describes this query (``_get_selection_plan_description()``).

        Args:
            connection_bound: False for a subquery - see ``_get_query_plan_description()``.

        Returns:
            The description, None when a part of the query keeps no plan.
        """
        return self._get_selection_plan_description(
            ValuesQuery,
            self._rows.plan_shape,
            tuple(self._selected_fields_by_alias.items()),
            connection_bound=connection_bound,
        )

    def _get_ordering_field_names(self, annotation_output_aliases: Mapping[str, str]) -> Collection[str]:
        """The selected names ``get_ordering()`` may order by.

        Args:
            annotation_output_aliases: Each selected annotation's name -> the alias it is SELECTed
                under.

        Returns:
            The names.
        """
        if self._rows.keyed_by_output_name:
            # A renamed annotation (.values(renamed="annotation")) is SELECTed under its new name.
            return [*self._selected_fields_by_alias, *annotation_output_aliases]
        return self._selected_fields_by_alias.values()

    def _get_selected_sql_fields(self) -> Collection[str] | None:
        """The fields ``get_filters()`` selects, None for its default."""
        return self._fields_to_select_sql

    def _prune_unselected_annotations(self) -> None:
        """Drops from the SELECT list the annotations a dict row doesn't return - one not named in
        ``.values()`` is filtered or ordered by, never returned."""
        if self._rows.keyed_by_output_name:
            aliases = self._selected_fields_by_alias
            self.query._selects = [select for select in self.query._selects if select.alias in aliases]

    def _get_output_reader(self, field_name: str) -> Callable[[Any], Any] | None:
        """Reads the value of a selected field off one output row.

        Args:
            field_name: The field name.

        Returns:
            The reader, or None when the field isn't selected on its own.
        """
        for position, (alias, selected_name) in enumerate(self._selected_fields_by_alias.items()):
            if self._get_concrete_field_path(selected_name) == field_name:
                return self._rows.get_reader(position, alias)
        return None

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
                self._get_output_value_field(field_name),
            )
            for alias, field_name in self._selected_fields_by_alias.items()
        ]

    async def _fetch_rows(
        self,
        db: DatabaseClient,
        sql: str,
        params: list[Any],
        column_converters: list[tuple[str, Callable[[Any], Any] | None]],
        value_fields: tuple[ValueField, ...] | None = None,
    ) -> list[Any]:
        """Runs a query selecting this query's columns and builds the output rows.

        Args:
            db: The connection.
            sql: The SQL.
            params: Its parameters.
            column_converters: Each column's alias and decoder.
            value_fields: What each column is read as - for reading the rows in one
                ``rust.native.rows`` call; None reads them in Python.

        Returns:
            The output rows.
        """
        return await self._rows.fetch(db, sql, params, column_converters, self.model, value_fields)

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
