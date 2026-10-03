from __future__ import annotations

from collections.abc import (
    Collection,
    Coroutine,
    Iterable,
    Iterator,
    Sequence,
)
from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar, Generic, Self, TypeVar, cast

from hare.core.lookup_path import LookupPath
from hare.dialects.base.client.database_client import DatabaseClient, retryable_read_query_active
from hare.dialects.base.client.transaction_client import TransactionClient
from hare.dialects.enums import ParameterPosition
from hare.dialects.identifiers import Identifiers
from hare.exceptions import (
    ConfigurationError,
    FieldError,
    MultipleObjectsReturned,
    QueryError,
    UnSupportedError,
)
from hare.fields.base.field import Field as ModelField
from hare.fields.data.json.json_field import JSONField
from hare.fields.encrypted.encrypted_field_mixin import EncryptedFieldMixin
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.relational_field import RelationalField
from hare.query.constants import (
    DISTINCT_ORDERING_COLUMN_ALIAS_PREFIX,
    NULL_REJECTING_LOOKUP_SUFFIXES,
)
from hare.query.enums import Lookup
from hare.query.expressions import (
    Expression,
    ExpressionContext,
    ExpressionResult,
    F,
    Q,
    Value,
)
from hare.query.expressions.aggregate_paths.aggregated_multi_valued_paths import AggregatedMultiValuedPaths
from hare.query.expressions.base.expression_result import TableCriterionTuple
from hare.query.expressions.enums import ValueRefOrigin
from hare.query.expressions.modifier import QueryModifier
from hare.query.expressions.outer_query_state import outer_expression_context, outer_extra_joins
from hare.query.expressions.raw_sql import RawSQL
from hare.query.expressions.value_refs.cursor_value_ref import CursorValueRef
from hare.query.expressions.value_refs.literal_value_ref import LiteralValueRef
from hare.query.expressions.value_refs.value_ref_types import RecordedValueRefs
from hare.query.lookup_paths import LookupPaths
from hare.query.plans.call_signature_plans import CallSignaturePlans
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.description.plannable import Plannable
from hare.query.plans.join_condition_recording import JoinConditionRecording
from hare.query.plans.recorded_join_condition import RecordedJoinCondition
from hare.query.plans.statement_plan import StatementPlan
from hare.query.plans.statement_plans import StatementPlans
from hare.query.queryset.calls_before_setup import CallsBeforeSetup
from hare.query.queryset.extensions.query_set_extensions import QuerySetExtensions
from hare.query.queryset.query_options import QueryOptions
from hare.query.queryset.query_spec import QuerySpec
from hare.query.scopes.row_scopes import RowScopes
from hare.sql import JoinType, Order, Table
from hare.sql.enums import Equality
from hare.sql.functions.declarations import JsonSortKey
from hare.sql.queries.builder.query_builder import QueryBuilder
from hare.sql.queries.joins.join import Join
from hare.sql.queries.tables.selectable import Selectable
from hare.sql.terms.base.select_reference import SelectReference
from hare.sql.terms.base.term import Term
from hare.sql.terms.base.value_wrapper import ValueWrapper
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.field import Field
from hare.utils import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.sql.context import SqlContext

TModel = TypeVar("TModel", bound="Model")

QUERY: QueryBuilder = QueryBuilder()


class AwaitableQuery(QuerySpec[TModel], Generic[TModel], abstract=True):
    #: Whether this type of query only reads - a read is retried on a lost connection, a write
    #: never: retried blindly it could take effect twice.
    is_read_only: ClassVar[bool] = False

    #: Whether an implicit GROUP BY always includes the primary key - one group per row. True for
    #: every query standing for model rows; False for .values()/.values_list(), which group by the
    #: fields they name.
    group_by_must_include_primary_key: ClassVar[bool] = True

    #: Whether get_ordering() may reference a selected annotation by its SELECT alias. False for
    #: UPDATE/DELETE, whose ordered pk subquery selects only the primary key.
    ordering_can_reference_annotation_alias: ClassVar[bool] = True

    #: Whether an annotated aggregate's value is never read - only whether the query returned a row
    #: (exists()). Two aggregated to-many relations in one query, which inflate each other's counts,
    #: are fine then.
    aggregate_value_is_unused: ClassVar[bool] = False

    #: Whether a filter reading a window function is applied to the query wrapped as a derived
    #: table instead of rejected - True for .values()/.values_list() (ValuesQuery).
    window_filter_wrapping_supported: ClassVar[bool] = False

    __slots__ = (
        "query",
        "_joined_tables",
        "_joined_tables_set",
        # One resolved term per annotation name and table for the current build, so a GROUP BY
        # and an ORDER BY of the same unselected annotation share one term - see
        # _get_annotation_expression_term().
        "_annotation_expression_terms",
        # The value references recorded while each annotation name was resolved again for one of
        # those clauses - see _get_expression_term_value_refs().
        "_expression_term_refs",
        # Populated fresh by _get_annotate() on every _make_query() call, read back by
        # ValuesQuery.get_value_reader() - see both for why this lives here instead
        # of on the (possibly shared/reused-across-queries) annotation expression object itself.
        "_annotation_output_fields",
        # Set by get_filters() (via _get_annotate()), read back by _apply_auto_group_by()
        # once the caller's own select list is fully finalized - see that method's own docstring
        # for why the two can't just be one step.
        "_has_aggregate",
        # Set by get_filters(): the filters (and keyset cursor) reading a window function, which
        # ValuesQuery applies to the built query wrapped as a derived table.
        "_window_filter_criterion",
        # Set on the copy a query makes of itself to run once - see _get_execution_query().
        "_is_execution_query",
        # The SQL text and parameters a plan hit of a running query left - see _run_on_plan().
        "_compiled_statement",
        # The plan the last build found for this query, or recorded - None for a query that keeps
        # no plan.
        "_statement_plan",
        # The arguments of _record_plan() for a query whose dialect QuerySet method calls
        # are applied after the build - recorded once they are (see _make_query()).
        "_deferred_plan_record",
        # Set by _make_query_to_run(): the query is built to be run by itself, not shown or
        # embedded in another one.
        "_runs_compiled_statement",
        # The calls the queryset this query runs was made with, when it was made by simple calls
        # alone (QuerySet._call_signature) - None otherwise - and the values of their filters.
        "_call_signature",
        "_call_values",
        # The key of the plan of the calls, the keys of its values and how many values the query
        # class binds after them, set when no plan was kept under the key yet - the plan this run
        # builds is kept under it too.
        "_call_signature_record",
    )

    #: The statement being built.
    query: QueryBuilder

    def _apply_db(self, db: DatabaseClient | None) -> None:
        """Binds this query to the connection it runs on - the statement being built starts
        over in that connection's builder.

        Args:
            db: The database connection to use for this query.
        """
        super()._apply_db(db)
        if db is not None and hasattr(self, "query"):
            self.query = db.query_class.get_empty_builder()

    def __init__(self, model: type[TModel]) -> None:
        super().__init__(model)
        self._init_build_state()

    def _init_build_state(self) -> None:
        """Sets what a build of the query fills in."""
        self._joined_tables: list[Table] = []
        self._joined_tables_set: set[Table] = set()
        self._annotation_expression_terms: dict[tuple[str, Table], Term] = {}
        self._expression_term_refs: dict[str, RecordedValueRefs] = {}
        self.query = QUERY
        self._annotation_output_fields: dict[str, ModelField[Any] | None] = {}
        self._has_aggregate: bool = False
        self._window_filter_criterion: Criterion | None = None
        self._is_execution_query: bool = False
        self._compiled_statement: tuple[str, list[Any]] | None = None
        self._statement_plan: StatementPlan | None = None
        self._deferred_plan_record: dict[str, Any] | None = None
        self._runs_compiled_statement: bool = False
        self._call_signature: tuple[Any, ...] | None = None
        self._call_values: tuple[Any, ...] = ()
        self._call_signature_record: tuple[tuple[Any, ...], tuple[str, ...], int] | None = None

    def _get_single_or_list_result(self, rows: list[Any]) -> Any:
        """The fetched rows as returned - reversed back for a ``.before_cursor()`` query, or the
        one row of a single-row query.

        Args:
            rows: The fetched rows.

        Returns:
            The rows, or the one row (None when there is none).

        Raises:
            DoesNotExist: A ``.get()`` query matched no row.
            MultipleObjectsReturned: A single-row query matched more than one row.
        """
        if self._single:
            if len(rows) == 1:
                return rows[0]
            if not rows:
                if self._raise_does_not_exist:
                    self._raise_object_does_not_exist()
                return None
            raise MultipleObjectsReturned(self.model)
        if self._reverse_result_order:
            return rows[::-1]
        return rows

    def _nulls_sort_first(self, order: Order) -> bool:
        """Whether NULLs of an ordering column sort first on this query's dialect: an explicit ``NULLS
        FIRST``/``NULLS LAST`` decides, else the dialect's default.
        """
        if order.nulls_first is not None:
            return order.nulls_first
        sorts_nulls_first = self.dialect.sorts_nulls_first
        return sorts_nulls_first if order.is_ascending else not sorts_nulls_first

    def _get_cursor_criterion(
        self,
        *,
        value_wrapper_refs: RecordedValueRefs | None = None,
    ) -> Criterion | None:
        """Builds the keyset criterion of ``.after_cursor(...)`` - the seek method: an OR of
        AND-chains, one per ordering field. A row-value comparison would assume one direction for
        every column.

        NULL-aware, by the placement of NULLs in each ordering: a NULL boundary is followed by every
        non-NULL row when NULLs sort first, and by nothing when they sort last; a non-NULL boundary
        is also followed by the NULL rows when NULLs sort after the ordinary values.

        ``value_wrapper_refs``, when a plan is recorded, gets one ``("cursor", CursorValueRef |
        None)`` entry per ordering field - None for a NULL boundary, which no plan can bind.

        The upper boundary of a window (``_before_cursor_values``) is "strictly after" the same
        values in the reversed ordering - the same formula with every order reversed; its references
        follow the lower boundary's.
        """
        if not self._cursor_values and not self._before_cursor_values:
            return None

        orderings = getattr(self, "_orderings", None)
        if not orderings:
            return None

        criterion: Criterion | None = None
        if self._cursor_values:
            criterion = self._get_cursor_bound_criterion(orderings, self._cursor_values, value_wrapper_refs)
        if self._before_cursor_values:
            reversed_orderings = [(field_name, order.get_reversed()) for field_name, order in orderings]
            before_criterion = self._get_cursor_bound_criterion(
                reversed_orderings, self._before_cursor_values, value_wrapper_refs
            )
            criterion = before_criterion if criterion is None else criterion & before_criterion
        return criterion

    def _get_cursor_bound_criterion(
        self,
        orderings: Sequence[tuple[str, Order]],
        cursor_values: tuple[Any, ...],
        value_wrapper_refs: RecordedValueRefs | None,
    ) -> Criterion | None:
        """Builds the "strictly after ``cursor_values`` in ``orderings``" criterion.

        Args:
            orderings: The ordering the boundary is relative to.
            cursor_values: One boundary value per leading ordering field - ``iterator()`` bounds a
                page only by the caller's own cursor fields while ordering by a primary key
                tie-breaker after them.
            value_wrapper_refs: See ``_get_cursor_criterion()``.

        Returns:
            The criterion, ``None`` only for an empty ordering.
        """
        table = self._effective_basetable()
        criterion: Criterion | None = None
        equalities: list[Criterion] = []
        last_index = len(cursor_values) - 1
        for index, ((field_name, order), value) in enumerate(
            zip(orderings[: len(cursor_values)], cursor_values, strict=True)
        ):
            term, joins, field = LookupPaths.get_nested_field(
                self.model,
                table,
                field_name,
                visibility=self._visibility,
                select_related_extra_conditions=self._select_related_extra_conditions,
                dialect=self.dialect,
                connection=self._db,
            )
            # An ordering across a relation compares a joined column - its JOINs are added to the
            # query.
            for join in joins:
                self._join_table(join)
            # A None boundary is not converted: it may come from a LEFT JOIN miss, and the target
            # field's validation would reject it.
            db_value = (
                self.dialect.types.get_db_value(field, value, self.model)
                if field is not None and value is not None
                else value
            )
            nulls_first = self._nulls_sort_first(order)
            if db_value is None:
                # A None boundary holds no value - which boundaries are None is part of the plan
                # key (_get_cursor_plan_description()); a value converting to NULL keeps no plan.
                if value_wrapper_refs is not None and value is not None:
                    value_wrapper_refs.append((ValueRefOrigin.CURSOR, None))
                if nulls_first:
                    comparison: Criterion = term.notnull()
                else:
                    comparison = BasicCriterion(
                        Equality.EQ,
                        ValueWrapper(1, allow_parametrize=False),
                        ValueWrapper(0, allow_parametrize=False),
                    )
                # `term = NULL` is never true - a row tying with a NULL boundary needs IS NULL.
                equality = term.isnull()
            else:
                ordinary = term > db_value if order.is_ascending else term < db_value
                comparison = ordinary if nulls_first else (ordinary | term.isnull())
                equality = term == db_value
                if value_wrapper_refs is not None:
                    # The comparison and the equality hold two separate ValueWrappers of one value -
                    # both are referenced. A raw ordering column has no field to convert a new value
                    # by. The last field's equality is in no AND-chain.
                    comparison_wrapper = ordinary.right
                    equality_wrapper = equality.right if index != last_index else None
                    if (
                        field is not None
                        and isinstance(comparison_wrapper, ValueWrapper)
                        and (equality_wrapper is None or isinstance(equality_wrapper, ValueWrapper))
                    ):
                        value_wrapper_refs.append(
                            (ValueRefOrigin.CURSOR, CursorValueRef(comparison_wrapper, equality_wrapper, field))
                        )
                    else:
                        value_wrapper_refs.append((ValueRefOrigin.CURSOR, None))
            step = comparison & Criterion.all(equalities) if equalities else comparison
            criterion = step if criterion is None else criterion | step
            equalities.append(equality)
        return criterion

    def _apply_effective_basetable(self) -> None:
        """Aliases ``self.query``'s base table to ``_effective_basetable()`` - for a query built
        straight from the model's base query. A no-op unless a self-referential subquery needs the
        alias.
        """
        effective_table = self._effective_basetable()
        if effective_table != self.model._meta.basetable:
            self.query = self.query.replace_table(self.model._meta.basetable, effective_table)

    def _get_annotation_fields(self) -> dict[str, ModelField[Any]]:
        """The field each ``populate_field_object`` annotation's value is decoded through, by key - a
        model instance decodes such an annotation the way ``.values()`` does.
        """
        return {
            key: output_field
            for key, output_field in self._annotation_output_fields.items()
            if output_field is not None
        }

    def get_filters(
        self,
        fields_for_select: Collection[str] | None = None,
        *,
        value_wrapper_refs: (RecordedValueRefs | None) = None,
    ) -> None:
        """Builds the query's filters. ``value_wrapper_refs``, when a plan is recorded, collects the
        value references in order: the annotations', the filters', then the keyset boundaries'.
        """
        # Shared with _get_annotate(), which fills it first - a filter splitting an aggregated
        # to-many relation into a second JOIN is detected through it.
        aggregated_multi_valued_paths = AggregatedMultiValuedPaths()
        # Every to-many JOIN outside an aggregate - a non-aggregate annotation's, a filter's, a
        # values() field's or the ordering's - counts towards the fan-out check below.
        aggregated_multi_valued_paths.is_recording_row_joins = True
        self._has_aggregate = self._get_annotate(
            fields_for_select,
            value_wrapper_refs=value_wrapper_refs,
            aggregated_multi_valued_paths=aggregated_multi_valued_paths,
        )

        modifier = QueryModifier()
        # Built once, not once per node - every field here is identical across the whole loop, and
        # Q.get_result() never mutates a ExpressionContext (frozen dataclass) it's handed.
        expression_context = ExpressionContext(
            model=self.model,
            dialect=self.dialect,
            connection=self._db,
            table=self._effective_basetable(),
            annotations=self._annotations,
            value_wrapper_refs=value_wrapper_refs,
            # A filter crossing a Select(relation, extra_condition=...) relation builds its JOIN
            # with that condition.
            select_related_extra_conditions=self._select_related_extra_conditions,
            multi_valued_join_generations={},
            aggregated_multi_valued_paths=aggregated_multi_valued_paths,
            visibility=self._visibility,
            window_function_filter_allowed=self.window_filter_wrapping_supported,
        )
        window_filter_criterion: Criterion | None = None
        for node in self._q_objects:
            node_modifier = node.get_result(expression_context)
            node_criterion = node_modifier._and_criterion()
            if isinstance(node_criterion, Criterion) and self._term_reads_window_function(node_criterion):
                if not self.window_filter_wrapping_supported:
                    raise QueryError(
                        "Cannot filter on a window function (Window(...)) - SQL does not allow window "
                        "functions in WHERE or HAVING. Filter a .values()/.values_list() query instead, "
                        "which applies the filter to the query wrapped in a subquery, e.g. "
                        "Model.objects.filter(pk__in=Subquery(queryset.filter(...).values(<pk field>)))."
                    )
                window_filter_criterion = (
                    node_criterion if window_filter_criterion is None else window_filter_criterion & node_criterion
                )
                modifier &= QueryModifier(joins=node_modifier.joins)
            else:
                modifier &= node_modifier

        # A non-distinct aggregate is inflated by every other to-many JOIN of the query - rejected,
        # unless only the query's truthiness is read.
        if not self.aggregate_value_is_unused and aggregated_multi_valued_paths.aggregate_crossings:
            for lookup in self._get_row_join_lookups():
                aggregated_multi_valued_paths.record_lookup(self.model, lookup)
            for lookup in self._get_non_null_filtered_lookups():
                aggregated_multi_valued_paths.record_non_null_lookup(self.model, lookup)
            for lookup in self._get_group_key_lookups():
                aggregated_multi_valued_paths.record_group_key_lookup(self.model, lookup)
            aggregated_multi_valued_paths.record_row_pinning_conditions(
                self.model,
                self._get_top_level_conditions_by_generation(),
                expression_context.multi_valued_join_generations or {},
            )
            if fan_out_message := aggregated_multi_valued_paths.get_fan_out_error_message():
                raise QueryError(fan_out_message)

        for join in modifier.joins:
            if join[0] not in self._joined_tables_set:
                self.query = self.query.join(join[0], how=JoinType.LEFT_OUTER).on(join[1])
                self._joined_tables.append(join[0])
                self._joined_tables_set.add(join[0])

        where_criterion = modifier.where_criterion
        if cursor_criterion := self._get_cursor_criterion(value_wrapper_refs=value_wrapper_refs):
            if window_filter_criterion is not None:
                # Pagination applies to the rows left after the window filter.
                window_filter_criterion = window_filter_criterion & cursor_criterion
            else:
                where_criterion = where_criterion & cursor_criterion if where_criterion else cursor_criterion

        self.query._havings = modifier.having_criterion
        self.query._wheres = where_criterion
        self._window_filter_criterion = window_filter_criterion

    def _get_row_join_lookups(self) -> list[str]:
        """Lookups this query joins outside ``get_filters()`` - its ordering and selected fields -
        whose to-many hops count towards the aggregate fan-out check.

        Returns:
            The lookups.
        """
        return []

    def _get_group_key_lookups(self) -> list[str]:
        """Field lookups of this query's GROUP BY keys - the explicit ``.group_by()`` fields.

        Returns:
            The lookups.
        """
        return self._get_field_lookups(self._group_bys)

    def _get_field_lookups(self, field_names: Iterable[str]) -> list[str]:
        """The field lookup each name reads - itself, or the lookup of a plain ``F("a__b")``
        annotation.

        Args:
            field_names: Field or annotation names.

        Returns:
            The lookups, other annotations left out.
        """
        lookups: list[str] = []
        for field_name in field_names:
            seen_names: set[str] = set()
            while field_name in self._annotations and field_name not in seen_names:
                seen_names.add(field_name)
                annotation = self._annotations[field_name]
                if type(annotation) is not F:
                    break
                field_name = annotation.name
            if field_name not in self._annotations:
                lookups.append(field_name)
        return lookups

    def _get_ordering_lookups(self) -> list[str]:
        """The effective ordering's field lookups, annotations left out.

        Returns:
            The lookups.
        """
        return [
            field_name
            for field_name, _order in self._apply_default_ordering(self._orderings, self._annotations)
            if field_name not in self._annotations
        ]

    def _get_non_null_filtered_lookups(self) -> set[str]:
        """The fields through a to-many relation that a null-rejecting filter (``tags__code="x"``,
        ``__isnull=False``, ``__in``) at the top of the first ``.filter()`` over the relation keeps
        from being NULL.

        Returns:
            The field lookups, without a lookup suffix.
        """
        conditions_by_generation: dict[int, list[tuple[str, Any]]] = {}
        for q_object in self._q_objects:
            conditions_by_generation.setdefault(q_object._filter_call_generation, []).extend(
                self._get_top_level_conditions(q_object)
            )
        first_generation_by_path: dict[str, int] = {}
        for q_object in self._q_objects:
            for key, _value in self._get_top_level_conditions(q_object, including_alternatives=True):
                for path in AggregatedMultiValuedPaths.get_multi_valued_paths(self.model, key):
                    first_generation = first_generation_by_path.get(path)
                    if first_generation is None or q_object._filter_call_generation < first_generation:
                        first_generation_by_path[path] = q_object._filter_call_generation
        non_null_lookups: set[str] = set()
        for generation, conditions in conditions_by_generation.items():
            for key, value in conditions:
                lookup = self._get_null_rejected_lookup(key, value)
                if lookup is None:
                    continue
                paths = AggregatedMultiValuedPaths.get_multi_valued_paths(self.model, lookup)
                if paths and first_generation_by_path.get(paths[-1]) == generation:
                    non_null_lookups.add(lookup)
        return non_null_lookups

    @staticmethod
    def _get_null_rejected_lookup(key: str, value: Any) -> str | None:
        """The field a filter kwarg keeps from being NULL.

        Args:
            key: The kwarg key.
            value: The kwarg value.

        Returns:
            The field lookup without its lookup suffix, or None when the kwarg can match NULL.
        """
        field_lookup, separator, suffix = key.rpartition("__")
        if not separator:
            field_lookup, suffix = key, ""
        if suffix == Lookup.ISNULL:
            return field_lookup if value is False else None
        if suffix == Lookup.NOT_ISNULL:
            return field_lookup if value is True else None
        if value is None:
            return None
        if (
            suffix == Lookup.IN
            and isinstance(value, (list, tuple, set, frozenset))
            and any(item is None for item in value)
        ):
            # ``__in=[..., None]`` matches NULL too (``OR ... IS NULL``).
            return None
        if suffix in NULL_REJECTING_LOOKUP_SUFFIXES:
            return field_lookup if suffix else key
        # The suffix is a field name itself (``tags__code="x"``).
        return key

    def _aggregates_per_model_row(self) -> bool:
        """Whether a query selecting chosen columns computes its aggregates per model row - they
        were annotated before ``.values()``/``.values_list()``, like Django.

        Returns:
            False - only a ``.values()``/``.values_list()`` query decides otherwise.
        """
        return False

    def _apply_auto_group_by(self) -> None:
        """Applies the implicit GROUP BY an aggregate annotation needs without an explicit
        ``.group_by()``: every selected non-aggregate term. Runs after every step adding joins or
        changing the SELECT list.

        With nothing non-aggregate selected, a HAVING filter groups by every base-table column - one
        group per row; without one the query is a single aggregate over the whole table and gets no
        GROUP BY. A ``.values()`` query whose aggregates were annotated before it groups by the
        primary key, like Django.
        """
        if self._has_aggregate:
            namespaced_ctx = self.query._sql_context_with_namespace(self.query.QUERY_CLS.SQL_CONTEXT)
            grouped_sql: set[str] = set()
            group_by_terms: list[Term] = []
            for select_term in self.query._selects:
                self._add_group_by_terms(
                    group_by_terms, grouped_sql, self._get_select_group_by_terms(select_term), namespaced_ctx
                )
            self._add_group_by_terms(group_by_terms, grouped_sql, self._get_orderby_group_by_terms(), namespaced_ctx)
            aggregates_per_model_row = self._aggregates_per_model_row()
            if not group_by_terms and not aggregates_per_model_row:
                if not self.query._havings:
                    return
                effective_table = self._effective_basetable()
                group_by_terms = [effective_table[field] for field in self.model._meta.db_fields]
            elif self.group_by_must_include_primary_key or aggregates_per_model_row:
                # One group per row: the selected columns alone needn't be unique, so the primary
                # key is grouped by too - it needn't be selected.
                effective_table = self._effective_basetable()
                self._add_group_by_terms(
                    group_by_terms,
                    grouped_sql,
                    [
                        effective_table[self.model._meta.fields_db_projection[attr]]
                        for attr in self.model._meta.pk_attr_names
                    ],
                    namespaced_ctx,
                )
            if self.query._havings:
                # A column HAVING reads outside its aggregates (Q(n__gte=2) | Q(dept__name="ops"),
                # an aggregate compared with a column) must be grouped too - Postgres rejects it
                # otherwise, SQLite picks an arbitrary row's value.
                grouped_sql = {self._get_group_by_sql(term, namespaced_ctx) for term in group_by_terms}
                self._add_group_by_terms(
                    group_by_terms, grouped_sql, self.query._havings.get_group_by_column_terms(), namespaced_ctx
                )
            # Grouped by without their SELECT alias: a bare alias like "id" would be ambiguous next
            # to a joined table's column of that name.
            aliasless_terms = []
            for term in group_by_terms:
                # An alias-free term stays the same object, so an ORDER BY of it renders with the
                # same bind parameters as its GROUP BY.
                aliasless_term = term
                if term.alias is not None:
                    aliasless_term = copy(term)
                    aliasless_term.alias = None
                aliasless_terms.append(aliasless_term)
            self.query = self.query.groupby(*aliasless_terms)

    @staticmethod
    def _get_select_group_by_terms(select_term: Term) -> list[Term]:
        """The GROUP BY terms one selected term needs. An expression grouped as a whole is referenced
        by its SELECT position - rendered again it would bind its literals under new placeholders.

        Args:
            select_term: The selected term.

        Returns:
            The terms to group by.
        """
        select_group_by_terms = select_term.get_group_by_terms()
        if (
            len(select_group_by_terms) == 1
            and select_group_by_terms[0] is select_term
            and not isinstance(select_term, Field)
        ):
            return [SelectReference(select_term)]
        return select_group_by_terms

    def _get_orderby_group_by_terms(self) -> list[Term]:
        """The ORDER BY terms the implicit GROUP BY also groups by - an ORDER BY column neither
        aggregated nor grouped is rejected by Postgres and collapses the result on SQLite.

        Returns:
            The group-by terms of every ORDER BY term.
        """
        orderby_group_by_terms: list[Term] = []
        for term, _direction in self.query._orderbys:
            # A bare table-less Field is a reference to a SELECT alias, which the selected terms
            # already cover.
            if isinstance(term, Field) and term.table is None:
                continue
            term_nodes: Iterator[Any] = term.nodes_()
            if any(node.is_subquery for node in term_nodes):
                # A subquery rendered again binds its literals again - Postgres wouldn't match it to
                # a GROUP BY copy, so the columns it reads are grouped instead.
                orderby_group_by_terms.extend(term.get_group_by_column_terms())
                continue
            orderby_group_by_terms.extend(term.get_group_by_terms())
        return orderby_group_by_terms

    @staticmethod
    def _get_group_by_sql(term: Term, namespaced_ctx: SqlContext) -> str:
        """A group-by term's aliasless SQL - a term already grouped by is skipped by it.

        Args:
            term: The group-by term.
            namespaced_ctx: The query's namespaced SQL context.

        Returns:
            The SQL.
        """
        if isinstance(term, SelectReference):
            return term.get_sql(namespaced_ctx)
        aliasless_term = term
        if term.alias is not None:
            aliasless_term = copy(term)
            aliasless_term.alias = None
        return aliasless_term.get_sql(namespaced_ctx.copy(with_alias=False))

    @classmethod
    def _add_group_by_terms(
        cls, group_by_terms: list[Term], grouped_sql: set[str], new_terms: list[Term], namespaced_ctx: SqlContext
    ) -> None:
        """Appends the terms whose SQL isn't grouped by yet.

        Args:
            group_by_terms: The GROUP BY terms collected so far, extended in place.
            grouped_sql: Their SQL, extended in place.
            new_terms: The candidate terms.
            namespaced_ctx: The query's namespaced SQL context.
        """
        for term in new_terms:
            term_sql = cls._get_group_by_sql(term, namespaced_ctx)
            if term_sql not in grouped_sql:
                grouped_sql.add(term_sql)
                group_by_terms.append(term)

    def _join_table_by_field(
        self,
        table: Table,
        related_field_name: str,
        related_field: RelationalField[Model],
        extra_condition: Q | None = None,
        value_wrapper_refs: RecordedValueRefs | None = None,
    ) -> Table:
        joins = LookupPaths.get_scoped_joins(
            table,
            related_field,
            related_field_name,
            visibility=self._visibility,
            dialect=self.dialect,
            connection=self._db,
        )
        if extra_condition is not None:
            related_table, join_criterion = joins[-1]
            # Recorded into the query's own references by _join_select_related(), whose values the
            # description holds; folded anywhere else, its values can't be bound.
            if value_wrapper_refs is None:
                JoinConditionRecording.record_unbindable()
            modifier = extra_condition.get_result(
                ExpressionContext(
                    model=related_field.related_model,
                    dialect=self.dialect,
                    connection=self._db,
                    table=related_table,
                    annotations={},
                    value_wrapper_refs=value_wrapper_refs,
                )
            )
            if modifier.joins:
                raise QueryError(
                    "Select(relation, extra_condition=...) only supports direct fields of the related model, "
                    "not a further relation"
                )
            if modifier.where_criterion:
                joins[-1] = (related_table, join_criterion & modifier.where_criterion)
        for join in joins:
            self._join_table(join)
        return joins[-1][0]

    def _reset_joined_tables(self) -> None:
        """Clears the record of joined tables before the query is built again - a second build would
        take every join as already made.
        """
        self._joined_tables = []
        self._joined_tables_set = set()
        self._annotation_expression_terms = {}
        self._expression_term_refs = {}

    def _join_table(self, table_criterio_tuple: TableCriterionTuple) -> None:
        if table_criterio_tuple[0] not in self._joined_tables_set:
            self.query = self.query.join(table_criterio_tuple[0], how=JoinType.LEFT_OUTER).on(table_criterio_tuple[1])
            self._joined_tables.append(table_criterio_tuple[0])
            self._joined_tables_set.add(table_criterio_tuple[0])

    def _apply_select_for_update(self) -> None:
        """Adds ``FOR UPDATE`` to the query, once every JOIN is built.

        Each ``of`` name - ``"self"``, the model's table name or a forward relation path - is
        rendered as the alias the query joins that table under, and its JOIN chain is made INNER:
        Postgres doesn't lock the nullable side of an outer join. With no ``of`` and a JOIN, only
        the base table is locked.

        Raises:
            QueryError: An ``of`` name is not a forward relation path of the model, is not joined,
                or crosses a relation that can't be INNER JOINed.
            QueryError: The query aggregates, groups, removes duplicates or computes a window
                function.
            UnSupportedError: The database has no SELECT ... FOR UPDATE.
        """
        if not self.features.supports_select_for_update:
            raise UnSupportedError(
                f"select_for_update() is not supported by the {self.dialect.name} backend - "
                "there is no SELECT ... FOR UPDATE equivalent there, so no lock can ever be taken. "
                "Silently ignoring the call would let a caller believe rows are locked when they "
                "aren't - remove this call instead of relying on it."
            )
        if self._has_aggregate or self.query._groupbys:
            raise QueryError(
                "select_for_update() can't be combined with an aggregate annotation or .group_by() - "
                "a grouped row stands for several table rows, so SQL can't lock it (FOR UPDATE is not "
                "allowed with GROUP BY). Lock the rows in a separate query, e.g. "
                "Model.objects.filter(pk__in=...).select_for_update(), then aggregate."
            )
        if self._distinct or self._distinct_on:
            raise QueryError(
                "select_for_update() can't be combined with .distinct() - a deduplicated row can stand for "
                "several table rows, so SQL can't lock it (FOR UPDATE is not allowed with DISTINCT). Lock "
                "the rows in a separate query, e.g. Model.objects.filter(pk__in=...).select_for_update()."
            )
        if self._reads_window_function():
            raise QueryError(
                "select_for_update() can't be combined with a window function (Window(...)) - SQL computes it "
                "over every matched row, so it can't lock them (FOR UPDATE is not allowed with window "
                "functions). Lock the rows in a separate query, e.g. "
                "Model.objects.filter(pk__in=...).select_for_update()."
            )
        base_table_name = self._effective_basetable().get_table_name()
        table_names: set[str] = set()
        inner_join_aliases: set[str] = set()
        joined_aliases = {table.get_table_name() for table in self._joined_tables}
        for name in sorted(self._select_for_update_of):
            if name in ("self", self.model._meta.db_table):
                table_names.add(base_table_name)
                continue
            model = self.model
            alias = base_table_name
            path = ""
            path_hops: list[tuple[str, str, RelationalField[Model]]] = []
            for part in name.split("__"):
                field = model._meta.fields_map.get(part)
                if not isinstance(field, ForeignKeyFieldInstance):
                    raise QueryError(
                        f"select_for_update(of=...) got {name!r}, which is not a forward relation path of "
                        f'{self.model.__name__} - pass "self" or a select_related()-style path such as "author".'
                    )
                path = f"{path}__{part}" if path else part
                alias = Identifiers.get_within_limit(f"{alias}__{part}")
                path_hops.append((alias, path, field))
                model = field.related_model
            if alias not in joined_aliases:
                raise QueryError(
                    f"select_for_update(of=...) got {name!r}, but this query doesn't join that relation - "
                    f"add .select_related({name!r})."
                )
            for hop_alias, hop_path, hop_field in path_hops:
                if hop_field.null:
                    raise QueryError(
                        f"select_for_update(of=...) can't lock {name!r}: {hop_path!r} is a nullable relation, "
                        "so it's joined with LEFT OUTER JOIN, whose nullable side can't be locked."
                    )
                ambient_condition = RowScopes.of(hop_field.related_model).get_condition(
                    visibility=self._visibility,
                )
                if (
                    not hop_field.has_database_constraint
                    or ambient_condition is not None
                    or hop_path in self._select_related_extra_conditions
                ):
                    raise QueryError(
                        f"select_for_update(of=...) can't lock {name!r}: {hop_path!r} has no database "
                        "constraint or carries an extra JOIN condition (soft delete, tenant, "
                        "Select(extra_condition=...)), so it can't be switched from LEFT OUTER JOIN to "
                        "INNER JOIN without changing the result."
                    )
                inner_join_aliases.add(hop_alias)
            table_names.add(alias)
        if not self._select_for_update_of and self._joined_tables:
            table_names.add(base_table_name)
        if inner_join_aliases:
            inner_joins: list[Join] = []
            for join in self.query._joins:
                if isinstance(join.item, Table) and join.item.get_table_name() in inner_join_aliases:
                    join = copy(join)
                    join.how = JoinType.INNER
                inner_joins.append(join)
            self.query._joins = inner_joins
        self.query = self.query.for_update(
            self._select_for_update_nowait,
            self._select_for_update_skip_locked,
            table_names,
            self._select_for_update_no_key and self.features.supports_select_for_no_key_update,
        )

    def _join_table_with_forwarded_fields(
        self, model: type[Model], table: Table, field: str, forwarded_fields: str, path: str | None = None
    ) -> tuple[Table, str]:
        # The dotted relation path up to this hop - the key a Select(relation, extra_condition=...)
        # is kept under.
        if path is None:
            path = field
        if field in model._meta.fields_db_projection and not forwarded_fields:
            return table, model._meta.fields_db_projection[field]

        if field in model._meta.fields_db_projection and forwarded_fields:
            raise FieldError(f'Field "{field}" for model "{model.__name__}" is not relation')

        if field in self.model._meta.fetch_fields and not forwarded_fields:
            raise QueryError(f'Selecting relation "{field}" is not possible, select concrete field on related model')

        field_object = cast("RelationalField[Model]", model._meta.fields_map.get(field))
        if not field_object:
            raise FieldError(f'Unknown field "{path}": {model.__name__} has no field "{field}"')

        extra_condition = self._select_related_extra_conditions.get(path)
        table = self._join_table_by_field(table, field, field_object, extra_condition)
        field, __, forwarded_fields_ = forwarded_fields.partition("__")

        return self._join_table_with_forwarded_fields(
            model=field_object.related_model,
            table=table,
            field=field,
            forwarded_fields=forwarded_fields_,
            path=f"{path}__{field}",
        )

    def _get_annotation_expression_term(self, field_name: str, annotations: dict[str, Any], table: Table) -> Term:
        """Resolves an annotation to its full SQL expression, for a clause that cannot reference its SELECT alias.

        Args:
            field_name: The annotation name.
            annotations: The annotations the expression may reference.
            table: The table the expression is resolved against.

        Returns:
            The annotation's expression term.
        """
        annotation = annotations[field_name]
        if isinstance(annotation, Term) and not isinstance(annotation, Expression):
            return annotation
        cache_key = (field_name, table)
        if (term := self._annotation_expression_terms.get(cache_key)) is not None:
            return term
        # The expression's values are recorded apart from the query's own - the plan binds them
        # last (_get_expression_term_value_refs()). Resolved for a second table, the name records
        # more references than its values, and the query keeps no plan.
        value_wrapper_refs = self._expression_term_refs.setdefault(field_name, [])
        term = annotation.get_result(
            self._get_annotation_expression_context(field_name, annotations, table, value_wrapper_refs)
        ).term
        self._annotation_expression_terms[cache_key] = term
        return term

    def _get_annotation_expression_context(
        self,
        field_name: str,
        annotations: dict[str, Any],
        table: Table,
        value_wrapper_refs: RecordedValueRefs | None = None,
    ) -> ExpressionContext:
        """The context an annotation is resolved in outside SELECT.

        Args:
            field_name: The annotation name.
            annotations: The annotations the expression may reference.
            table: The table the expression is resolved against.
            value_wrapper_refs: Where the expression's value references are recorded.

        Returns:
            The resolve context.
        """
        return ExpressionContext(
            model=self.model,
            dialect=self.dialect,
            connection=self._db,
            table=table,
            annotations=annotations,
            annotation_names_in_progress={field_name},
            visibility=self._visibility,
            value_wrapper_refs=value_wrapper_refs,
        )

    def _get_expression_term_names(self) -> list[str]:
        """The annotations a GROUP BY, ORDER BY or DISTINCT ON names - each may be resolved again
        into its full expression there (``_get_annotation_expression_term()``).

        Returns:
            The names, sorted.
        """
        annotations = self._annotations
        if not annotations:
            return []
        named = {*self._group_bys, *(ordering[0] for ordering in self._orderings), *self._distinct_on}
        return sorted(name for name in named if name in annotations)

    def _get_expression_terms_plan_description(self) -> PlanDescription | None:
        """Describes the annotations a GROUP BY, ORDER BY or DISTINCT ON names, for a type that
        binds their expressions' values (``binds_expression_term_values``): their values follow
        the query's own, in the order of their names.

        Returns:
            The description, None when one keeps no plan.
        """
        names = self._get_expression_term_names()
        if not names:
            return PlanDescription.EMPTY
        context = PlanContext(self._annotations)
        values: list[Any] = []
        for name in names:
            annotation = self._annotations[name]
            if not isinstance(annotation, Plannable):
                return None
            description = annotation.get_plan_description(context)
            if description is None:
                return None
            values.extend(description.values)
        return PlanDescription(tuple(names), values)

    def _get_expression_term_value_refs(self) -> RecordedValueRefs:
        """The value references of the annotations a GROUP BY, ORDER BY or DISTINCT ON names, in
        the order of their names - one the build didn't resolve again (selected under its alias)
        is resolved now, its references then binding nothing: the text doesn't hold its terms.

        Returns:
            The references.
        """
        value_wrapper_refs: RecordedValueRefs = []
        names = self._get_expression_term_names()
        if not names:
            return value_wrapper_refs
        table = self._effective_basetable()
        for name in names:
            annotation = self._annotations[name]
            if isinstance(annotation, RawSQL):
                value_wrapper_refs.extend(
                    (ValueRefOrigin.ANNOTATION, LiteralValueRef(param)) for param in annotation.params
                )
                continue
            if name not in self._expression_term_refs:
                self._get_annotation_expression_term(name, self._annotations, table)
            value_wrapper_refs.extend(self._expression_term_refs.get(name, ()))
        return value_wrapper_refs

    def _annotation_holds_json(self, field_name: str, annotations: dict[str, Any], table: Table) -> bool:
        """Whether an annotation's value is JSON (a JSON field, a path into one, a ``JSONObject``).

        Args:
            field_name: The annotation name.
            annotations: The annotations the expression may reference.
            table: The table the expression is resolved against.

        Returns:
            True for a JSON value.
        """
        annotation = annotations[field_name]
        if not isinstance(annotation, Expression):
            return False
        result = annotation.get_result(self._get_annotation_expression_context(field_name, annotations, table))
        return isinstance(annotation.get_value_field(result), JSONField)

    def _get_annotation_select_alias(self, field_name: str) -> str | None:
        """Finds the alias an annotation is actually SELECTed under.

        Args:
            field_name: The annotation name.

        Returns:
            The SELECT alias (the name itself, or a positional/renamed alias the same annotation
            was re-registered under by values()/values_list()), or None when it is not selected.
        """
        selected_aliases = {select.alias for select in self.query._selects if select.alias}
        if field_name in selected_aliases:
            return field_name
        annotation = self._annotations[field_name]
        for alias in selected_aliases:
            if self._annotations.get(alias) is annotation:
                return alias
        return None

    def _get_field_object_by_path(self, field_name: str) -> ModelField[Any] | None:
        """Returns the model field a ``field``/``relation__field`` name (or an annotation that's a
        bare ``F()`` of one) points at.

        Args:
            field_name: The field path or annotation name.

        Returns:
            The field, or None when the name isn't a plain model field path.
        """
        seen_annotation_names: set[str] = set()
        while (annotation := self._annotations.get(field_name)) is not None:
            if type(annotation) is not F or field_name in seen_annotation_names:
                return None
            seen_annotation_names.add(field_name)
            field_name = annotation.name
        concrete_field_paths = self.get_concrete_field_paths(self.model, field_name)
        if len(concrete_field_paths) == 1:
            field_name = concrete_field_paths[0]
        return LookupPath.parse(self.model, field_name).get_target_field()

    def _get_group_bys(self, *field_names: str) -> list[Term]:
        group_bys: list[Term] = []
        effective_table = self._effective_basetable()
        for field_name in field_names:
            EncryptedFieldMixin.raise_if_encrypted(self._get_field_object_by_path(field_name), "GROUP BY")
            if field_name in self._annotations:
                annotation_term = self._get_annotation_expression_term(field_name, self._annotations, effective_table)
                if annotation_term.contains_aggregate:
                    raise FieldError(
                        f"Cannot group by {field_name!r} - it is an aggregate annotation, computed from the "
                        "groups themselves. Group by a field or a non-aggregate annotation instead."
                    )
                # A bare Field renders as the quoted SELECT alias. An annotation that is not
                # selected at all (.alias(), or left out of values()/values_list()) has no alias
                # to reference and is grouped by its full expression instead.
                select_alias = self._get_annotation_select_alias(field_name)
                if select_alias is not None:
                    group_bys.append(Field(select_alias))
                else:
                    group_bys.append(
                        self._get_annotation_expression_term(field_name, self._annotations, effective_table)
                    )
                continue
            # A composite key (pk, or a relation to one) groups by every one of its columns.
            for concrete_field_name in self.get_concrete_field_paths(self.model, field_name):
                field, __, forwarded_fields = concrete_field_name.partition("__")
                related_table, related_db_field = self._join_table_with_forwarded_fields(
                    model=self.model,
                    table=effective_table,
                    field=field,
                    forwarded_fields=forwarded_fields,
                )
                group_bys.append(
                    related_table[related_db_field].as_(f"{related_table.get_table_name()}__{concrete_field_name}")
                )
        return group_bys

    def _apply_with_ctes(self, value_wrapper_refs: RecordedValueRefs | None = None) -> None:
        """Attaches every ``with_cte()`` CTE to ``self.query``. Runs after every other reassignment of
        ``self.query``. A CTE body is built with the enclosing query's correlation context cleared -
        a ``WITH`` body can't reference the enclosing FROM items.

        Args:
            value_wrapper_refs: The list this query records its value references into - each CTE
                body's go there too, in order; a body recording none adds an empty reference, so no
                plan is kept.
        """
        for name, cte_body in self._with_ctes:
            if isinstance(cte_body, AwaitableQuery):
                query = cte_body.get_bound_to(self._db, self.model, f"the body of with_cte({name!r}, ...)")
                # A relation's field-level lazy="joined"/"select" default counts like an explicit
                # select_related()/prefetch_related().
                if query._loads_relations():
                    # A CTE body is never run by itself - relations it would load afterwards are
                    # never loaded.
                    raise QueryError(
                        "CTE bodies do not support select_related()/prefetch_related() - the "
                        "loaded relation cannot survive being folded into a WITH clause"
                    )
                outer_token = outer_expression_context.set(None)
                joins_token = outer_extra_joins.set(None)
                try:
                    if value_wrapper_refs is not None and query.plannable:
                        query._make_subquery(value_wrapper_refs=value_wrapper_refs)
                    else:
                        query._make_subquery()
                        if value_wrapper_refs is not None:
                            value_wrapper_refs.append((ValueRefOrigin.SUBQUERY, None))
                finally:
                    outer_expression_context.reset(outer_token)
                    outer_extra_joins.reset(joins_token)
                cte_query: Selectable = query.query
            else:
                cte_query = cte_body
                if value_wrapper_refs is not None:
                    value_wrapper_refs.append((ValueRefOrigin.SUBQUERY, None))
            self.query = self.query.with_(cte_query, name)

    def _first_column(self, row: Any) -> Any:
        """Returns the first column of a result row - by position where the connection's rows
        allow it (``Features.supports_positional_rows``), which skips building the row's mapping,
        else the first value of a row that is a plain mapping.

        Args:
            row: The row.

        Returns:
            The value.
        """
        if self.features.supports_positional_rows:
            return row[0]
        return next(iter(row.values()))

    @staticmethod
    def get_relation_ordering_key_names(model: type[Model], field_name: str) -> tuple[str, ...] | None:
        """The key field names an ordering by a forward FK/O2O relation stands for, at the end of
        a plain or ``related__relation`` path.

        Args:
            model: The model the path starts from.
            field_name: An ordering field name.

        Returns:
            The relation's own key field names with the path prefix kept (``("tournament_id",)``,
            ``("event__tournament_id",)``), or None when the path doesn't end in a forward FK/O2O
            relation.
        """
        lookup_path = LookupPath.parse(model, field_name)
        if len(lookup_path.rest) != 1:
            return None
        meta = lookup_path.model._meta
        last_name = lookup_path.rest[0]
        if last_name not in meta.fk_fields and last_name not in meta.o2o_fields:
            return None
        key_field_names = cast("RelationalField[Model]", meta.fields_map[last_name]).source_fields
        return tuple(f"{lookup_path.prefix}{key_field_name}" for key_field_name in key_field_names)

    def get_ordering(
        self,
        model: type[Model],
        table: Table,
        orderings: Iterable[tuple[str, Order]],
        annotations: dict[str, Term | Expression],
        fields_for_select: Collection[str] | None = None,
        annotation_output_aliases: dict[str, str] | None = None,
        *,
        select_related_path_prefix: str = "",
    ) -> None:
        """
        Applies standard ordering to QuerySet.

        Args:
            model: The Model this queryset is based on.
            table: ``hare.sql.Table`` to keep track of the virtual SQL table
                (to allow self referential joins)
            orderings: What columns/order to order by
            annotations: Annotations that may be ordered on
            fields_for_select: Contains fields that are selected in the SELECT clause if
                .only(), .values() or .values_list() are used.
            annotation_output_aliases: Maps an annotation's own name to the alias it is
                actually SELECTed under, for callers (``.values_list()``) that rename annotations to
                positional aliases (``"0"``, ``"1"``, ...) instead of keeping their own name in
                SELECT - an ORDER BY reference must use that same alias, since Postgres (unlike
                SQLite) rejects an ORDER BY alias that doesn't match a SELECTed column.
            select_related_path_prefix: The dotted relation path already walked to reach this
                call, empty at the top level - extended by one hop per recursive call below, so a
                multi-hop ordering (``left__left__name``) matches a ``Select(relation, extra_
                condition=...)`` registered under the FULL path (``"left__left"``), not just this
                hop's own name. Lets a JOIN built here for ordering fold in the SAME extra_condition
                ``QuerySet._join_select_related()`` would otherwise apply later - without this,
                ``_join_table()``'s table-identity dedup keeps whichever JOIN got added FIRST, which
                would silently drop extra_condition if this method's own (otherwise unconditioned)
                JOIN ran first.

        Raises:
            FieldError: If a field provided does not exist in model.
        """
        orderings = self._apply_default_ordering(orderings, annotations)

        for ordering in orderings:
            field_name = ordering[0]
            key_field_names = self.get_relation_ordering_key_names(model, field_name)
            if key_field_names is not None:
                self.get_ordering(
                    model,
                    table,
                    [(key_field_name, ordering[1]) for key_field_name in key_field_names],
                    annotations,
                    fields_for_select,
                    annotation_output_aliases,
                    select_related_path_prefix=select_related_path_prefix,
                )
                continue
            if field_name not in annotations and (
                field_name in model._meta.fetch_fields or (field_name == "pk" and "pk" not in model._meta.fields_map)
            ):
                # A to-many or reverse one-to-one relation orders by the related primary key,
                # "pk" by the primary key field(s), like Django.
                self.get_ordering(
                    model,
                    table,
                    [(field_path, ordering[1]) for field_path in self.get_concrete_field_paths(model, field_name)],
                    annotations,
                    fields_for_select,
                    annotation_output_aliases,
                    select_related_path_prefix=select_related_path_prefix,
                )
                continue

            related_field_name, __, forwarded = field_name.partition("__")
            if related_field_name in model._meta.fetch_fields and field_name not in annotations:
                related_field = cast("RelationalField[Model]", model._meta.fields_map[related_field_name])
                full_path = (
                    f"{select_related_path_prefix}__{related_field_name}"
                    if select_related_path_prefix
                    else related_field_name
                )
                extra_condition = self._select_related_extra_conditions.get(full_path)
                related_table = self._join_table_by_field(table, related_field_name, related_field, extra_condition)
                self.get_ordering(
                    related_field.related_model,
                    related_table,
                    [(forwarded, ordering[1])],
                    {},
                    select_related_path_prefix=full_path,
                )
            elif field_name in annotations:
                if model is self.model:
                    EncryptedFieldMixin.raise_if_encrypted(self._get_field_object_by_path(field_name), "ORDER BY")
                term: Term
                # An ORDER BY uses the bare alias only when the annotation is selected: an .alias()
                # never is, unless .values()/.values_list() names it.
                is_selected = self.ordering_can_reference_annotation_alias and (
                    field_name not in self._alias_keys if not fields_for_select else field_name in fields_for_select
                )
                if is_selected:
                    alias = field_name
                    if annotation_output_aliases:
                        alias = annotation_output_aliases.get(field_name, field_name)
                    term = Field(alias)
                else:
                    term = self._get_annotation_expression_term(field_name, annotations, table)
                if self._annotation_holds_json(field_name, annotations, table):
                    # JSON values order as jsonb - by the expression itself, as a SELECT DISTINCT
                    # copies the ordering into SELECT, where no alias resolves.
                    term = JsonSortKey(self._get_annotation_expression_term(field_name, annotations, table))
                self.query = self.query.orderby(term, order=ordering[1])
            else:
                field_object = model._meta.fields_map.get(field_name)

                if not field_object:
                    full_name = (
                        f"{select_related_path_prefix}__{field_name}" if select_related_path_prefix else field_name
                    )
                    raise FieldError(
                        f"Unknown field {full_name} for ordering: {model.__name__} has no field {field_name}"
                    )
                EncryptedFieldMixin.raise_if_encrypted(field_object, "ORDER BY")
                field_name = field_object.source_field or field_name
                field: Term = table[field_name]

                func = field_object.get_function_cast(self.dialect)
                if func:
                    field = func(field_object, field)
                if isinstance(field_object, JSONField):
                    # SQLite orders JSON values as Postgres orders jsonb.
                    field = JsonSortKey(field)

                self.query = self.query.orderby(field, order=ordering[1])

    def _include_orderbys_in_select(self) -> None:
        """Adds every ORDER BY term missing from the SELECT list to it - a plain ``SELECT DISTINCT``
        needs them selected (Postgres). Applied on every dialect, so the rows are the same
        everywhere. A term added here is never read into a model attribute.
        """
        namespaced_ctx = self.query._sql_context_with_namespace(self.query.QUERY_CLS.SQL_CONTEXT)
        selected_sql = set()
        for select_term in self.query._selects:
            selected_sql.add(select_term.get_sql(namespaced_ctx))
            # An annotation selected under an alias is ordered by a bare reference to the alias -
            # recognized as selected already, not appended again.
            if select_term.alias:
                selected_sql.add(Field(select_term.alias).get_sql(namespaced_ctx))
        orderbys: list[tuple[Term, Order | None]] = []
        for index, (term, direction) in enumerate(self.query._orderbys):
            term_sql = term.get_sql(namespaced_ctx)
            if term_sql not in selected_sql:
                if not isinstance(term, Field) and term.alias is None:
                    term = term.as_(f"{DISTINCT_ORDERING_COLUMN_ALIAS_PREFIX}{index}")
                self.query = self.query.select(term)
                selected_sql.add(term_sql)
            orderbys.append((term, direction))
        self.query._orderbys = orderbys

    def get_distinct(
        self,
        distinct: bool,
        distinct_on: Sequence[str],
        orderings: Iterable[tuple[str, Order]],
        annotations: dict[str, Term | Expression],
    ) -> None:
        self.query._distinct = distinct
        self.query._distinct_on = []
        if not distinct:
            return
        orderings = self._apply_default_ordering(orderings, annotations)
        if not distinct_on:
            # A plain DISTINCT (no DISTINCT ON field list) - distinct_on's own
            # "leading ORDER BY fields" validation below doesn't apply here.
            self._include_orderbys_in_select()
        else:
            if not self.dialect.supports_distinct_on:
                raise UnSupportedError(f"distinct(*fields) is not supported by the {self.dialect} dialect")
            ordering_fields = [ordering[0] for ordering in orderings]
            len_ordering_fields = len(ordering_fields)
            # Compared the way the ordering was expanded - a relation by its key columns, "pk" by
            # every primary key field.
            distinct_on_ordering_fields = [
                ordering_field
                for distinct_on_name in distinct_on
                for ordering_field in (
                    self.get_relation_ordering_key_names(self.model, distinct_on_name)
                    or (self.model._meta.pk_attr_names if distinct_on_name == "pk" else (distinct_on_name,))
                )
            ]
            distinct_on_by_source_field = []
            for field_name in (
                concrete_field_path
                for distinct_on_name in distinct_on
                for concrete_field_path in (
                    (distinct_on_name,)
                    if distinct_on_name in annotations
                    else self.get_concrete_field_paths(self.model, distinct_on_name)
                )
            ):
                if field_name in annotations:
                    distinct_on_by_source_field.append(
                        self._get_annotation_expression_term(field_name, annotations, self._effective_basetable())
                    )
                    continue
                field_object = self.model._meta.fields_map.get(field_name)
                part_after = field_name
                related_table = self._effective_basetable()
                related_model: type[Model] = self.model
                path_prefix = ""
                while part_after:
                    related_field_name, __, part_after = part_after.partition("__")
                    if related_field_name in related_model._meta.fetch_fields:
                        # Each hop is looked up on the model reached so far, not on the query's own
                        # model.
                        related_field = cast(
                            "RelationalField[Model]", related_model._meta.fields_map[related_field_name]
                        )
                        path_prefix = f"{path_prefix}__{related_field_name}" if path_prefix else related_field_name
                        extra_condition = self._select_related_extra_conditions.get(path_prefix)
                        related_table = self._join_table_by_field(
                            related_table, related_field_name, related_field, extra_condition
                        )
                        related_model = related_field.related_model
                    else:
                        field_object = related_model._meta.fields_map.get(related_field_name)

                        if not field_object:
                            raise FieldError(f"Unknown field {related_field_name} for model {related_model.__name__}")
                        EncryptedFieldMixin.raise_if_encrypted(field_object, "distinct(*fields)")
                        related_table_field: Term = related_table[field_object.source_field or related_field_name]
                        if func := field_object.get_function_cast(self.dialect):
                            related_table_field = func(field_object, related_table_field)
                        distinct_on_by_source_field.append(related_table_field)
            # Checked once every name resolved - an unknown one is a FieldError, not a mismatch.
            for i, field in enumerate(distinct_on_ordering_fields):
                if ordering_fields and (i >= len_ordering_fields or ordering_fields[i] != field):
                    raise QueryError(
                        f"distinct(*fields) must match the leading order_by() fields. "
                        f"Expected order_by() to start with {distinct_on!r}."
                    )
            self.query = self.query.distinct_on(*distinct_on_by_source_field)

    def _get_annotate(
        self,
        fields_for_select: Collection[str] | None = None,
        *,
        value_wrapper_refs: (RecordedValueRefs | None) = None,
        aggregated_multi_valued_paths: AggregatedMultiValuedPaths | None = None,
    ) -> bool:
        # Fresh on every build - kept on the query, not on the annotation expression, which queries
        # share.
        self._annotation_output_fields = {}
        if not self._annotations:
            return False

        has_aggregate = False
        unused_alias_keys = self._get_unused_alias_keys(fields_for_select)
        # In dict order - the order of the .annotate() calls, which is the SELECT column order and
        # the order the plan lists the values in. An expression registered under several keys is
        # resolved once.
        results_by_annotation_id: dict[int, ExpressionResult] = {}
        for key, annotation in self._annotations.items():
            if key in unused_alias_keys:
                continue
            resolved_info = results_by_annotation_id.get(id(annotation))
            # A Subquery is a Term and an Expression: its inner query is built while this query's
            # context is active, or its OuterRef finds nothing.
            if resolved_info is not None:
                info = resolved_info
            elif isinstance(annotation, Term) and not isinstance(annotation, Expression):
                info = ExpressionResult(term=annotation)
                if isinstance(annotation, RawSQL) and value_wrapper_refs is not None:
                    # RawSQL wrapped its params when it was created - recorded as they are.
                    for param in annotation.params:
                        value_wrapper_refs.append((ValueRefOrigin.ANNOTATION, LiteralValueRef(param)))
            else:
                info = annotation.get_result(
                    ExpressionContext(
                        model=self.model,
                        dialect=self.dialect,
                        connection=self._db,
                        # The table the FROM clause uses - an alias for a self-referential model
                        # inside a subquery.
                        table=self._effective_basetable(),
                        annotations=self._annotations,
                        value_wrapper_refs=value_wrapper_refs,
                        annotation_names_in_progress={key},
                        # An annotation crossing a select_related(extra_condition=...) relation
                        # builds its JOIN with that condition - the JOIN built first is the one
                        # kept.
                        select_related_extra_conditions=self._select_related_extra_conditions,
                        aggregated_multi_valued_paths=aggregated_multi_valued_paths,
                        visibility=self._visibility,
                    )
                )
            results_by_annotation_id[id(annotation)] = info

            for join in info.joins:
                self._join_table(join)
            # An .alias() key isn't selected - unless .values()/.values_list() names it. Without
            # them every ordinary annotation is.
            should_select = key in fields_for_select if fields_for_select is not None else key not in self._alias_keys
            if should_select:
                select_term = info.term
                if isinstance(annotation, Value):
                    select_term = Value.get_typed_term(
                        select_term, annotation.value, ParameterPosition.SELECTED_VALUE, self.dialect
                    )
                self.query._select_other(select_term.as_(key))  # type:ignore[arg-type]
            has_aggregate = has_aggregate or info.term.contains_aggregate
            # The result's field is kept on this build, not on the annotation expression queries
            # share - only for functions whose result has their argument's type
            # (populate_field_object).
            if getattr(annotation, "populate_field_object", False):
                self._annotation_output_fields[key] = (
                    annotation.get_value_field(info) if isinstance(annotation, Expression) else info.output_field  # type:ignore[call-overload]
                )
            elif isinstance(annotation, Value):
                self._annotation_output_fields[key] = Value.get_literal_output_field(annotation.value)

        return has_aggregate

    def _query_is_plannable(self) -> bool:
        """Whether this query keeps a plan: its state allows one and each annotation and filter keeps
        one.

        Returns:
            True when the query keeps a plan.
        """
        return self._query_state_is_plannable() and self._get_filters_plan_description() is not None

    def _query_state_is_plannable(self) -> bool:
        """Whether the state the description doesn't cover allows a plan: no grouping or ordering by an
        annotation rendering values of its own, every ``.alias()`` used, and no correlation to an
        enclosing query on the same table. Checked before the build.

        Returns:
            True when the state allows a plan.
        """
        outer_context = outer_expression_context.get()
        outer_table_name = outer_context.table.get_table_name() if outer_context is not None else None
        if outer_table_name is not None and self.model._meta.basetable.get_table_name() == outer_table_name:
            return False
        annotations = self._annotations
        if not annotations:
            return True
        # A GROUP BY/ORDER BY of an annotation that isn't SELECTed, and a DISTINCT ON of one,
        # renders the annotation's full expression - a type that doesn't bind its values
        # (binds_expression_term_values) would keep the first query's values there.
        if not self.binds_expression_term_values:
            for name in self._get_expression_term_names():
                annotation = annotations[name]
                if not isinstance(annotation, Plannable) or (
                    name not in self._distinct_on and not self._annotation_renders_as_expression(name)
                ):
                    continue
                annotation_description = annotation.get_plan_description(PlanContext.EMPTY)
                if annotation_description is not None and annotation_description.values:
                    return False
        # An unused .alias() is left out of the built query, so the values it holds would no
        # longer line up with every annotation's - one holding none (a renamed field) is harmless.
        for name in self._get_unused_alias_keys():
            annotation = annotations[name]
            if not isinstance(annotation, Plannable):
                return False
            annotation_description = annotation.get_plan_description(PlanContext.EMPTY)
            if annotation_description is None or annotation_description.values:
                return False
        return True

    def _annotation_renders_as_expression(self, annotation_name: str) -> bool:
        """Whether a GROUP BY/ORDER BY of an annotation renders its full expression rather than
        its SELECT alias.

        Args:
            annotation_name: The annotation name.

        Returns:
            True when the annotation isn't selected, or ORDER BY can't reference an alias.
        """
        return annotation_name in self._alias_keys or not self.ordering_can_reference_annotation_alias

    def _get_annotations_plan_description(self) -> PlanDescription | None:
        """Describes the annotations in the order ``_get_annotate()`` resolves them: per key its name,
        its expression's structure and whether it is an ``.alias()``. An expression under several
        keys is listed once.

        Returns:
            The description, None when an annotation keeps no plan.
        """
        annotations = self._annotations
        if not annotations:
            return PlanDescription.EMPTY
        context = PlanContext(annotations)
        structures: list[tuple[Any, ...]] = []
        values: list[Any] = []
        descriptions_by_annotation_id: dict[int, tuple[str, PlanDescription]] = {}
        for key, annotation in annotations.items():
            first_key_and_description = descriptions_by_annotation_id.get(id(annotation))
            if first_key_and_description is None:
                if not isinstance(annotation, Plannable):
                    return None
                description = annotation.get_plan_description(context)
                if description is None:
                    return None
                first_key_and_description = descriptions_by_annotation_id[id(annotation)] = (key, description)
                values.extend(description.values)
            first_key, description = first_key_and_description
            structures.append((key, description.structure, key in self._alias_keys, first_key))
        return PlanDescription(tuple(structures), values)

    @staticmethod
    def _get_conditions_plan_description(conditions: Iterable[Q], context: PlanContext) -> PlanDescription | None:
        """Describes conditions applied one after another - a query's filters, a relation's
        ``Select(extra_condition=...)``.

        Args:
            conditions: The conditions.
            context: The context they are resolved in.

        Returns:
            The structure of each and the values of all of them in order, None when one keeps no
            plan.
        """
        structures = []
        values: list[Any] = []
        for condition in conditions:
            description = condition.get_plan_description(context)
            if description is None:
                return None
            structures.append(description.structure)
            values.extend(description.values)
        return PlanDescription(tuple(structures), values)

    def _get_filters_plan_description(self) -> PlanDescription | None:
        """Describes the annotations, then the filters - ``get_filters()`` resolves them in this
        order; a filter on an annotation resolves the annotation again.

        Returns:
            The description, None when an annotation or a filter keeps no plan.
        """
        annotations_description = self._get_annotations_plan_description()
        if annotations_description is None:
            return None
        # A query built into another one has no connection of its own yet - its lists are described
        # by their length.
        db = self._db
        filters_description = self._get_conditions_plan_description(
            self._q_objects,
            PlanContext(
                self._annotations or None,
                db.dialect.single_parameter_in_list_min_length if db is not None else None,
            ),
        )
        if filters_description is None:
            return None
        return PlanDescription(
            (annotations_description.structure, filters_description.structure),
            annotations_description.values + filters_description.values,
        )

    def _get_cursor_plan_description(self) -> PlanDescription:
        """Describes the keyset boundaries: per bounded field whether its value is None - an ``IS
        NULL`` test instead of a comparison - and the values other than None, lower boundary first.

        Returns:
            The description.
        """
        cursor_values = self._cursor_values
        before_cursor_values = self._before_cursor_values
        if not cursor_values and not before_cursor_values:
            return self.NO_CURSOR_PLAN_DESCRIPTION
        return PlanDescription(
            (
                tuple(value is None for value in cursor_values),
                tuple(value is None for value in before_cursor_values),
            ),
            [value for value in (*cursor_values, *before_cursor_values) if value is not None],
        )

    def _get_ctes_plan_description(self) -> PlanDescription | None:
        """Describes the CTEs: each one's name and its body's description, the query built into
        this one (``_apply_with_ctes(value_wrapper_refs=...)`` records the bodies' values).

        Returns:
            The description, None when a body keeps no plan - a body given as SQL among them.
        """
        options = self._options
        if options is QueryOptions.DEFAULT or not options.with_ctes:
            return PlanDescription.EMPTY
        structures: list[tuple[Any, ...]] = []
        values: list[Any] = []
        for name, cte_body in options.with_ctes:
            if not isinstance(cte_body, AwaitableQuery):
                return None
            cte_description = cte_body.get_plan_description(PlanContext.EMPTY)
            if cte_description is None:
                return None
            structures.append((name, cte_body.get_pinned_connection_name(), cte_description.structure))
            values.extend(cte_description.values)
        return PlanDescription(tuple(structures), values)

    def _get_extension_calls_structure(self) -> tuple[Any, ...] | None:
        """The calls of the dialect's QuerySet methods this query makes (.sample(), .final(), ...),
        for its plan key: each method's name and arguments - the dialect writes them into the
        query after it is built, so they are part of its SQL text.

        Returns:
            One ``(name, args, kwargs)`` per call, or None when an argument can't be part of a key
            (it isn't hashable) - such a query keeps no plan.
        """
        options = self._options
        if options is QueryOptions.DEFAULT or not options.extension_calls:
            return ()
        structure = tuple(
            (extension_call.name, extension_call.args, tuple(sorted(extension_call.kwargs.items())))
            for extension_call in options.extension_calls
        )
        try:
            hash(structure)
        except TypeError:
            return None
        return structure

    def _get_connection_structure(self, connection_bound: bool) -> tuple[Any, ...]:
        """The part of a plan key the connection decides: its dialect and its alias - two
        differently configured connections sharing one dialect never share a plan.

        Args:
            connection_bound: False for a query built into another one: the dialect and the
                connection are that query's, and the part is the connection the query is pinned
                to with ``.using()`` (None when it isn't) - a query pinned to another connection
                than the enclosing one is refused when it is built, so it never shares a plan
                built without that check.

        Returns:
            The parts.
        """
        db = self._db
        connection_name = db.connection_name if db is not None else None
        if not connection_bound:
            return (connection_name,)
        return self.dialect, connection_name

    def _get_visibility_structure(self) -> tuple[Any, ...]:
        """The part of a plan key the query's visibility decides - its escape hatches and a tenant
        it pins. The active tenant isn't part of it: a plan binds the default scopes of the
        running query's context.

        Returns:
            The parts.
        """
        visibility = self._visibility
        return (
            visibility.all_tenants,
            visibility.include_deleted,
            visibility.only_deleted,
            Q.get_hashable_value(visibility.tenant) if visibility.tenant_is_pinned else None,
        )

    def _get_filter_value_query(self) -> AwaitableQuery[Any]:
        """The query a filter compares with when this query is its value.

        Returns:
            This query itself; a bare queryset selects its primary key instead.
        """
        return self

    def _get_scoped_copy(self) -> Self:
        """A copy of this query with its default scope applied - what a query built into another one (a
        subquery, a CTE body, a set operation's branch) describes in its ``get_plan_description()``.

        Returns:
            The copy.
        """
        scoped_query = copy(self)
        scoped_query._apply_ambient_scope()
        return scoped_query

    def _get_query_plan_description(
        self, query_class: type, *parts: Any, describes_cursor: bool = False, connection_bound: bool = True
    ) -> PlanDescription | None:
        """Describes this query built as ``query_class``. The structure - the plan key - is the class,
        model, dialect and connection alias, the class's own ``parts``, the annotations, filters,
        distinct settings, keyset boundaries, CTEs, the dialect's QuerySet method calls and the zone
        of aware datetimes. The values are the filters', the boundaries' and the CTEs'.

        Args:
            query_class: The query class the plan belongs to.
            parts: The structure only this class has.
            describes_cursor: Whether the keyset boundaries are part of the query.
            connection_bound: False for a query built into another one: the dialect and connection
                are that query's.

        Returns:
            The description, None when a part of the query keeps no plan.
        """
        filters_description = self._get_filters_plan_description()
        if filters_description is None:
            return None
        ctes_description = self._get_ctes_plan_description()
        if ctes_description is None:
            return None
        extension_calls_structure = self._get_extension_calls_structure()
        if extension_calls_structure is None:
            return None
        values = filters_description.values
        cursor_structure = None
        if describes_cursor:
            cursor_description = self._get_cursor_plan_description()
            cursor_structure = cursor_description.structure
            values = values + cursor_description.values
        expression_terms_structure = None
        expression_terms_values: list[Any] = []
        if self.binds_expression_term_values:
            expression_terms_description = self._get_expression_terms_plan_description()
            if expression_terms_description is None:
                return None
            expression_terms_structure = expression_terms_description.structure
            expression_terms_values = expression_terms_description.values
        return PlanDescription(
            (
                query_class,
                self.model,
                *self._get_connection_structure(connection_bound),
                self._get_visibility_structure(),
                *parts,
                filters_description.structure,
                self._distinct,
                tuple(self._distinct_on),
                cursor_structure,
                ctes_description.structure,
                extension_calls_structure,
                expression_terms_structure,
                Timezone.get_rendered_zone_name(),
            ),
            values + ctes_description.values + expression_terms_values,
        )

    def _find_plan(
        self,
        plan_key: tuple[Any, ...],
        current_values: list[Any],
        *,
        paginate: bool = False,
        plan: StatementPlan | None = None,
    ) -> bool:
        """Runs this query on the plan kept under its key, when its values fit it. The output fields of
        ``populate_field_object`` annotations are restored too.

        Args:
            plan_key: The plan key.
            current_values: This query's values, in the order the plan's references were recorded.
            paginate: Whether this query's LIMIT/OFFSET replace the plan's.
            plan: The plan when the caller already found it.

        Returns:
            True when this query runs on the plan - the caller then builds nothing.
        """
        if plan is None:
            plan = StatementPlans.find(plan_key)
        if plan is None or not self._runs_compiled_statement:
            return False
        parameters = self._get_plan_parameters(plan, current_values, paginate)
        return parameters is not None and self._run_on_plan(plan, parameters)

    def _get_plan_parameters(self, plan: StatementPlan, current_values: list[Any], paginate: bool) -> list[Any] | None:
        """This query's parameters on a plan - its values, the values of the default scopes folded
        into the plan's JOINs, and its LIMIT/OFFSET.

        Args:
            plan: The plan.
            current_values: This query's values, in the order the plan's references were recorded.
            paginate: Whether this query's LIMIT/OFFSET replace the plan's.

        Returns:
            The parameters, None when the query's SQL text would differ from the plan's.
        """
        if plan.join_conditions:
            join_condition_values = plan.get_join_condition_values()
            if join_condition_values is None:
                return None
            current_values = [*current_values, *join_condition_values]
        pagination = (self._limit, self._offset) if paginate else ()
        return plan.bind(current_values, self.model, self._db.dialect, *pagination)

    def _run_on_plan(self, plan: StatementPlan, parameters: list[Any]) -> bool:
        """Runs this query on a plan with its parameters bound - nothing is cloned or rendered.

        Args:
            plan: The plan.
            parameters: The parameters (``_get_plan_parameters()``).

        Returns:
            True - the query runs on the plan.
        """
        self.query = plan.query_builder
        self._compiled_statement = (cast("str", plan.sql), parameters)
        self._annotation_output_fields = dict(plan.annotation_output_fields)
        self._statement_plan = plan
        StatementPlans.count_hit()
        return True

    def _record_plan(
        self,
        plan_key: tuple[Any, ...] | None,
        value_wrapper_refs: RecordedValueRefs | None,
        expected_value_count: int,
        decode_plan: Any = None,
        decode_plan_is_partial: bool = False,
        select_related_idx: tuple[Any, ...] = (),
        decode_plan_key: tuple[str | None, ...] | None = None,
        binds_ctes: bool = False,
        result_reading: Any = None,
        extension_calls_applied: bool = False,
        join_condition_entries: tuple[tuple[RecordedJoinCondition, RecordedValueRefs] | None, ...] = (),
    ) -> None:
        """Keeps the query just built as the plan of its key, when every value it holds was
        recorded with a reference a later query's value can replace.

        Args:
            plan_key: The plan key, None for a query that keeps no plan.
            value_wrapper_refs: The references recorded while building, None when not recorded.
            expected_value_count: How many references the build records for a query keeping a
                plan - the number of its values.
            decode_plan: The row decode plan kept with the plan.
            decode_plan_is_partial: Whether the decode plan covers only some fields.
            select_related_idx: The select_related() row layout kept with the plan.
            decode_plan_key: The names of the base model's selected columns, kept with the plan.
            binds_ctes: Whether the references of the query's CTEs are among the recorded ones.
            result_reading: What else reading the result of the plan needs (StatementPlan).
            extension_calls_applied: Whether the calls of the dialect's QuerySet methods are applied
                to the query already - a query with calls is recorded only then (_make_query()),
                and never when an argument of a call can't be part of a key.
            join_condition_entries: The default scopes the build folded into its JOINs
                (``JoinConditionRecording``) - None for a condition no later query can bind.
        """
        if plan_key is None or value_wrapper_refs is None or None in join_condition_entries:
            return
        if self._extension_calls and not extension_calls_applied:
            if self._get_extension_calls_structure() is not None:
                self._deferred_plan_record = {
                    "plan_key": plan_key,
                    "value_wrapper_refs": value_wrapper_refs,
                    "expected_value_count": expected_value_count,
                    "decode_plan": decode_plan,
                    "decode_plan_is_partial": decode_plan_is_partial,
                    "select_related_idx": select_related_idx,
                    "decode_plan_key": decode_plan_key,
                    "binds_ctes": binds_ctes,
                    "result_reading": result_reading,
                    "join_condition_entries": join_condition_entries,
                }
            return
        if self.binds_expression_term_values:
            value_wrapper_refs = [*value_wrapper_refs, *self._get_expression_term_value_refs()]
        join_conditions: list[RecordedJoinCondition] = []
        for join_condition_entry in join_condition_entries:
            join_condition, join_condition_refs = cast(
                "tuple[RecordedJoinCondition, RecordedValueRefs]", join_condition_entry
            )
            join_conditions.append(join_condition)
            value_wrapper_refs = [*value_wrapper_refs, *join_condition_refs]
            expected_value_count += join_condition.value_count
        if len(value_wrapper_refs) != expected_value_count or any(ref is None for _key, ref in value_wrapper_refs):
            return
        # A query with a plan that didn't run on it - built to be shown or built into another
        # query, or with a value the plan can't bind - keeps that plan.
        existing_plan = StatementPlans.find(plan_key)
        if existing_plan is not None:
            self._statement_plan = existing_plan
            self._record_call_signature_plan(existing_plan, plan_key)
            return
        plan = self._statement_plan = StatementPlan(
            self.query,
            tuple((key, ref) for key, ref in value_wrapper_refs if ref is not None),
            decode_plan,
            decode_plan_is_partial,
            select_related_idx,
            tuple(self._annotation_output_fields.items()),
            decode_plan_key,
            binds_ctes,
            result_reading,
            tuple(join_conditions),
        )
        StatementPlans.record(plan_key, plan)
        self._record_call_signature_plan(plan, plan_key)

    def _record_call_signature_plan(self, plan: StatementPlan, plan_key: tuple[Any, ...]) -> None:
        """Keeps the plan just found or built under the key of the calls the queryset was made
        with too, when its values are the calls' own, in their order.

        Args:
            plan: The plan.
            plan_key: The key it is kept under.
        """
        call_signature_record = self._call_signature_record
        if call_signature_record is None:
            return
        self._call_signature_record = None
        CallSignaturePlans.record(self.model, *call_signature_record, plan, plan_key)

    def _get_statements(self, params_inline: bool) -> list[tuple[str, list[Any]]]:
        self._make_query()
        if params_inline:
            return [(self.query.get_sql(), [])]
        sql, values = self.query.get_parameterized_sql()
        return [(sql, values)]

    def _make_query(self, **kwargs: Any) -> None:
        """Builds ``self.query`` for the context the query runs in: its default scope first.

        Args:
            kwargs: Passed on to ``_build_query()``.
        """
        CallsBeforeSetup.raise_if_invalid(self)
        self._compiled_statement = None
        self._statement_plan = None
        self._deferred_plan_record = None
        self._apply_ambient_scope()
        self._build_query(**kwargs)
        if self._extension_calls:
            self._apply_extension_calls()

    def _apply_extension_calls(self) -> None:
        """Applies the calls of the dialect's QuerySet methods to the query just built, then
        records its plan - the plan's SQL text holds the calls. A query run on its plan has them
        in the text already.

        Raises:
            UnSupportedError: The connection's dialect has no implementation of a called method.
        """
        if self._compiled_statement is not None:
            QuerySetExtensions.check(self)
            return
        QuerySetExtensions.apply(self)
        deferred_plan_record = self._deferred_plan_record
        if deferred_plan_record is not None:
            self._deferred_plan_record = None
            # A copy of the plan's query with other values in its terms would miss the calls.
            self._record_plan(**deferred_plan_record, extension_calls_applied=True)

    def _make_query_to_run(self) -> None:
        """Builds the query to be run by itself - the only build a plan's compiled statement may stand
        in for. A query shown with ``.sql()``/``explain()`` or built into another one is built with
        ``_make_query()``. A query of a queryset made by simple calls alone runs on the plan kept
        under the key of the calls, built not even in part.
        """
        self._runs_compiled_statement = True
        if self._call_signature is not None and self._run_on_call_signature_plan():
            return
        self._make_query()

    def _run_on_call_signature_plan(self) -> bool:
        """Runs this query on the plan kept under the key of the calls its queryset was made with
        (``CallSignaturePlans``).

        Returns:
            True when the query runs on the plan; False when it is built - the plan it builds is then
            kept under the key too.
        """
        CallsBeforeSetup.raise_if_invalid(self)
        self._call_signature_record = None
        kind_structure, kind_values = self._get_call_signature_kind_part()
        if kind_structure is None:
            return False
        signature_key = CallSignaturePlans.get_key(type(self), self, self._db, kind_structure)
        if signature_key is None:
            return False
        key, values, value_keys, visibility = signature_key
        plan = CallSignaturePlans.find(self.model, key)
        if plan is None:
            self._call_signature_record = (key, value_keys, len(kind_values))
            return False
        parameters = self._get_plan_parameters(plan, [*values, *kind_values], self.plan_binds_slice)
        self._deferred_plan_record = None
        if parameters is None or not self._run_on_plan(plan, parameters):
            return False
        self._visibility = visibility
        self._prepare_call_signature_run()
        self._restore_from_plan(plan)
        return True

    def _get_call_signature_kind_part(self) -> tuple[Any, list[Any]]:
        """What the key of the calls and the values bound hold of this kind of query beyond its
        calls - the values a write assigns, say.

        Returns:
            The structure (None when the query isn't run by the key of its calls) and the values,
            bound after the calls' own.
        """
        return (), []

    def _prepare_call_signature_run(self) -> None:
        """Works out what running on a plan of the calls needs - what a build would before looking
        its plan up (``_prepare_build()``)."""
        self._prepare_build()

    def _get_parameterized_sql(self) -> tuple[str, list[Any]]:
        """The statement to run: the compiled one running on a plan left, otherwise
        ``self.query`` rendered.

        Returns:
            The SQL text and its parameters.
        """
        compiled_statement = self._compiled_statement
        if compiled_statement is not None:
            return compiled_statement
        return self.query.get_parameterized_sql()

    #: Whether a plan's statement takes the LIMIT and OFFSET of the query running on it.
    plan_binds_slice: ClassVar[bool] = False

    def _build_query(self, *, value_wrapper_refs: RecordedValueRefs | None = None) -> None:
        """Builds ``self.query`` - the one way a query of any type is built: prepared, then run on
        the plan kept for it (``StatementPlans``), or built in full and kept as the plan.

        Args:
            value_wrapper_refs: The list of an enclosing query this query, built into it (a
                subquery, a CTE body, a branch of a set operation), records its value references
                into - in full, with no plan of its own looked up or kept. A query that keeps no
                plan records an empty reference there, so the enclosing query keeps none either.
        """
        records_for_caller = value_wrapper_refs is not None
        # The default scopes the build folds into JOINs are recorded from the start - preparing
        # builds JOINs too (.only() across a relation). Built into another query, they record into
        # that query's recording.
        join_condition_entries: list[tuple[RecordedJoinCondition, RecordedValueRefs] | None] = []
        recording_token = None if records_for_caller else JoinConditionRecording.entries.set(join_condition_entries)
        try:
            self._reset_joined_tables()
            self._annotation_output_fields = {}
            self._prepare_build()
            if records_for_caller:
                if not self._keeps_plan_built_into_another():
                    cast("RecordedValueRefs", value_wrapper_refs).append((ValueRefOrigin.SUBQUERY, None))
                self._build_statement(value_wrapper_refs, records_for_caller=True)
                return
            description, plan = self._get_plan()
            if (
                description is not None
                and plan is not None
                and self._find_plan(
                    description.structure,
                    self._get_plan_values(description, plan),
                    paginate=self.plan_binds_slice,
                    plan=plan,
                )
            ):
                self._restore_from_plan(plan)
                return
            value_wrapper_refs = [] if description is not None else None
            keeps_plan = self._build_statement(value_wrapper_refs, records_for_caller=False)
        finally:
            if recording_token is not None:
                JoinConditionRecording.entries.reset(recording_token)
        if description is not None and keeps_plan:
            self._record_plan(
                self._get_plan_key(description),
                value_wrapper_refs,
                self._get_plan_value_count(description),
                binds_ctes=True,
                join_condition_entries=tuple(join_condition_entries),
                **self._get_plan_record(),
            )

    def _prepare_build(self) -> None:
        """Works out what both a run on a plan and a full build need - called before the plan is
        looked up, so a check made here is made on a plan hit too."""

    def _keeps_plan_built_into_another(self) -> bool:
        """Whether this query, built into another one, lets that query keep a plan - it records
        every value it holds.

        Returns:
            True when it does.
        """
        # The enclosing query's key holds this query's structure; whether a query correlated to it
        # aliases its own table is decided by the two tables alone, in that key as well.
        outer_context_token = outer_expression_context.set(None)
        try:
            return self._query_is_plannable()
        finally:
            outer_expression_context.reset(outer_context_token)

    def _get_build_plan_description(self) -> PlanDescription | None:
        """Describes the statement this query builds - the structure is the plan key, the values
        are bound into the plan's statement.

        Returns:
            The description, None for a query that keeps no plan.
        """
        return None

    def _get_plan(self) -> tuple[PlanDescription | None, StatementPlan | None]:
        """The description of this query's statement and the plan kept under its key.

        Returns:
            ``(None, None)`` for a query that keeps no plan; the description alone for the first
            query of its key.
        """
        description = self._get_build_plan_description()
        if description is None:
            return None, None
        # A plan kept under the key is the answer to whether the query keeps one - except inside a
        # correlated subquery, where that also depends on the enclosing query.
        plan = StatementPlans.find(description.structure) if outer_expression_context.get() is None else None
        if plan is None and not self._query_state_is_plannable():
            return None, None
        return description, plan

    def _get_plan_key(self, description: PlanDescription) -> tuple[Any, ...]:
        """The key the plan of ``description`` is kept under."""
        return description.structure

    def _get_plan_values(self, description: PlanDescription, plan: StatementPlan) -> list[Any]:
        """This query's values, in the order the references of ``plan`` were recorded."""
        return description.values

    def _get_plan_value_count(self, description: PlanDescription) -> int:
        """How many value references the build just made records for a query keeping a plan."""
        return len(description.values)

    def _restore_from_plan(self, plan: StatementPlan) -> None:
        """Takes from the plan this query runs on what reading the result needs - a full build
        works it out while building."""

    def _get_plan_record(self) -> dict[str, Any]:
        """What else the plan of the query just built keeps (``_record_plan()``'s arguments)."""
        return {}

    def _build_statement(self, value_wrapper_refs: RecordedValueRefs | None, *, records_for_caller: bool) -> bool:
        """Builds ``self.query`` in full.

        Args:
            value_wrapper_refs: The list the value references are recorded into, None when no plan
                is recorded.
            records_for_caller: Whether the list is an enclosing query's - the query is built into
                it.

        Returns:
            False when the statement built can't be kept as a plan after all.
        """
        raise NotImplementedError()  # pragma: nocoverage

    def _apply_ambient_scope(self) -> None:
        """Resolves the tenant the query runs for and, for a query under its model's default scope,
        puts that scope's filters in front of its own - replacing the ones a previous compilation
        put there.

        Raises:
            QueryError: ``Hare.init()`` hasn't run, or the model is tenant-scoped and no
                tenant is active.
        """
        if not self.model._meta.basetable.get_table_name():
            raise ConfigurationError(
                f"Can't run a query on {self.model.__name__} - Hare.init() hasn't set up its connections yet"
            )
        # Resolved once per build - every JOIN and every query made from this one is scoped to
        # the same tenant as its own filters.
        visibility = self._visibility = self._visibility.get_for_active_tenant()
        ambient_filters = RowScopes.of(self.model).get_filters(visibility, uses_default_scope=self._uses_default_scope)
        ambient_q_objects = [Q(**{field_name: value}) for field_name, value in ambient_filters]
        if ambient_q_objects or self._ambient_q_count:
            self._q_objects = [*ambient_q_objects, *self._q_objects[self._ambient_q_count :]]
            self._ambient_q_count = len(ambient_q_objects)

    def _get_execution_query(self, for_write: bool = False) -> Self:
        """A copy of this query bound to the connection it runs on for one execution - a queryset
        run again later, or by several tasks at once, compiles into its own copy each time.

        Args:
            for_write: Whether the connection is chosen for a write.

        Returns:
            The copy, or this query itself when it already is one.
        """
        execution_query = self if self._is_execution_query else cast("Self", self.__copy__())
        execution_query._is_execution_query = True
        if cast("DatabaseClient | None", execution_query._db) is None:
            execution_query._apply_db(execution_query.get_connection(for_write))
        return execution_query

    def _make_subquery(self, **kwargs: Any) -> None:
        """Builds ``self.query`` to be embedded in an enclosing query; a ``.none()`` queryset
        gets an always-false condition there, since it never reaches the ``_is_none`` check done
        at execution time.

        Args:
            kwargs: Passed on to ``_build_query()`` - ``value_wrapper_refs``, the enclosing query's
                list its value references are recorded into, for a type that describes its plan
                (``plannable``).
        """
        self._make_query(**kwargs)
        self._apply_none_as_subquery()

    def _apply_none_as_subquery(self) -> None:
        if self._is_none:
            self.query = self.query.where(
                BasicCriterion(
                    Equality.EQ,
                    ValueWrapper(1, allow_parametrize=False),
                    ValueWrapper(0, allow_parametrize=False),
                )
            )

    async def _execute(self) -> Any:
        raise NotImplementedError()  # pragma: nocoverage

    async def _execute_with_retry_context(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """Awaits the query's execution - for a read-only query with the connection-loss retry context
        active, so only reads are retried. Rejects a ``select_for_update()`` query run outside a
        transaction.
        """
        if self._select_for_update and not isinstance(self._db, TransactionClient):
            coro.close()
            raise QueryError(
                "select_for_update() requires an active Transactions.atomic() block - a "
                "SELECT ... FOR UPDATE lock is only meaningful for the lifetime of the enclosing "
                "transaction; used outside one, the lock is acquired and immediately released "
                "(autocommit), giving no real protection while looking like it does"
            )
        if not type(self).is_read_only or not self._db.read_retry_max_retries:
            return await coro
        token = retryable_read_query_active.set(True)
        try:
            return await coro
        finally:
            retryable_read_query_active.reset(token)
