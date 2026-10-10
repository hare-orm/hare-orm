from __future__ import annotations

from collections.abc import AsyncGenerator, Callable, Iterable, Sequence
from operator import attrgetter
from typing import TYPE_CHECKING, Any, ClassVar, Self, TypeVar, cast

from hare.exceptions import QueryError
from hare.fields.field import Field as ModelField
from hare.query.constants import PLAN_CACHE_MISS
from hare.query.expressions import F
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.enums import PlanKeyForm
from hare.query.plans.statement.declared_plan_slots import DeclaredPlanSlots
from hare.query.plans.statement.statement_plan import StatementPlan
from hare.query.plans.statement.statement_plan_descriptions import StatementPlanDescriptions
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.queryset.query_specification import QuerySpecification
from hare.query.queryset.row_multiplication import RowMultiplication
from hare.query.queryset.single_rows.get_exceptions import GetExceptions
from hare.query.queryset.specification_copying import SpecificationCopying
from hare.query.statements.building.query_joins import QueryJoins
from hare.query.statements.building.query_ordering import QueryOrdering
from hare.query.statements.constants import ITERATOR_CURSOR_ANNOTATION_PREFIX
from hare.query.statements.select.model_rows.instance_hydration import InstanceHydration
from hare.query.statements.select.model_rows.model_rows_plan_descriptions import ModelRowsPlanDescriptions
from hare.query.statements.select.model_rows.only_defer_fields import OnlyDeferFields
from hare.query.statements.select.model_rows.select_related_joins import SelectRelatedJoins
from hare.query.statements.select.select_query import SelectQuery
from hare.sql import Order
from hare.sql.terms.field import Field

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.query.queryset.queryset import QuerySet
    from hare.query.rows.native.hydration_layout import HydrationEntry
    from hare.sql import Table
    from hare.sql.terms.term import Term

TModel = TypeVar("TModel", bound="Model")


class ModelRowsQuery(SelectQuery[TModel]):
    """Builds and runs the SQL of a queryset returning model instances - made from the queryset
    for one build."""

    __slots__ = (
        "_source_queryset",
        "_base_selects",
        "_effective_fields_for_select",
        "_select_related_positions",
        "_decode_plan",
        "_decode_plan_is_partial",
        "_decode_plan_key",
    )

    # Slots a copy made to run once (_get_execution_query()) gets its own shallow copy of.
    #: The short key of a plain query - one selecting every column of its model with only filters,
    #: ordering, a plain .distinct() and a slice (``ModelRowsPlanDescriptions.has_plain_query_state()``).
    plain_plan_slots: ClassVar[DeclaredPlanSlots] = (
        *StatementPlanDescriptions.HEAD_SLOTS,
        ("_decode_plan_key", PlanKeyForm.VALUE),
        ("_orderings", PlanKeyForm.TUPLE),
        (StatementPlanDescriptions.get_options_structure, PlanKeyForm.METHOD),
        ("_distinct", PlanKeyForm.VALUE),
        ("_limit", PlanKeyForm.BOUND_INTO_ANOTHER),
        ("_offset", PlanKeyForm.BOUND_INTO_ANOTHER),
        ("_is_none", PlanKeyForm.VALUE),
        ("_q_objects", PlanKeyForm.CONDITIONS),
        (StatementPlanDescriptions.get_ctes_plan_description, PlanKeyForm.DESCRIBED),
        (StatementPlanDescriptions.get_extension_calls_plan_description, PlanKeyForm.DESCRIBED),
        (StatementPlanDescriptions.get_zone_structure, PlanKeyForm.METHOD),
    )
    #: The key of any other query. Only the base table's own selected columns - the joined relations
    #: and the .only()/.defer() fields their columns follow are parts of their own; the
    #: Select(relation, extra_condition=...) conditions in join order.
    plan_slots: ClassVar[DeclaredPlanSlots] = (
        *StatementPlanDescriptions.HEAD_SLOTS,
        ("_decode_plan_key", PlanKeyForm.VALUE),
        ("_select_related", PlanKeyForm.SORTED_TUPLE),
        ("_effective_fields_for_select", PlanKeyForm.VALUE),
        (ModelRowsPlanDescriptions.get_extra_conditions_plan_description, PlanKeyForm.DESCRIBED),
        (StatementPlanDescriptions.get_filters_plan_description, PlanKeyForm.DESCRIBED),
        ("_orderings", PlanKeyForm.TUPLE),
        (StatementPlanDescriptions.get_options_structure, PlanKeyForm.METHOD),
        *StatementPlanDescriptions.CURSOR_SLOTS,
        ("_distinct", PlanKeyForm.VALUE),
        ("_limit", PlanKeyForm.BOUND_INTO_ANOTHER),
        ("_offset", PlanKeyForm.BOUND_INTO_ANOTHER),
        ("_is_none", PlanKeyForm.VALUE),
        (StatementPlanDescriptions.get_zone_structure, PlanKeyForm.METHOD),
        (StatementPlanDescriptions.get_ctes_plan_description, PlanKeyForm.DESCRIBED),
        (StatementPlanDescriptions.get_extension_calls_plan_description, PlanKeyForm.DESCRIBED),
        *StatementPlanDescriptions.EXPRESSION_TERMS_SLOTS,
    )

    mutable_clone_slots: ClassVar[dict[str, str]] = QuerySpecification.MUTABLE_SPECIFICATION_SLOTS | {
        "_joined_tables": "[]",
        "_joined_tables_set": "set()",
    }

    def __init__(self, source_queryset: QuerySet[TModel, Any]) -> None:
        """
        Args:
            source_queryset: The queryset whose rows the query builds.
        """
        SpecificationCopying.copy_specification(source_queryset, self)
        self._source_queryset = source_queryset
        self._init_build_state()
        # The .only() whitelist, or .defer()'s blacklist expanded into one - rebuilt by every
        # _make_query() call.
        self._effective_fields_for_select: tuple[str, ...] = ()
        self._select_related_positions: list[
            tuple[type[Model], int, Table | str, type[Model], Iterable[str | None]]
        ] = []  # format with: model,idx,model_name,parent_model
        self._decode_plan: tuple[HydrationEntry, ...] | None = None
        self._decode_plan_is_partial: bool = False
        self._decode_plan_key: tuple[str | None, ...] | None = None
        self._base_selects: tuple[Term, ...] | None = None

    def _get_row_join_lookups(self) -> list[str]:
        return QueryJoins.get_ordering_lookups(self)

    def _get_group_key_lookups(self) -> list[str]:
        # Model instances aren't grouped by .group_by() fields - only values()/values_list(),
        # count() and aggregate() are.
        return []

    async def _execute(self) -> list[TModel]:
        InstanceHydration.check_no_annotation_field_collision(self)
        instance_list: list[TModel] = await InstanceHydration.get_model_rows_reader(self).fetch(  # type: ignore[assignment]
            *self._get_parameterized_sql(), InstanceHydration.get_prefetch_request(self)
        )
        if self._single:
            if len(instance_list) == 1:
                return instance_list[0]  # type: ignore[return-value]
            if not instance_list:
                if self._raise_does_not_exist:
                    GetExceptions.raise_object_does_not_exist(self)
                return None  # type: ignore[return-value]
            GetExceptions.raise_multiple_objects_returned(self)
        if self._reverse_result_order:
            return instance_list[::-1]
        return instance_list

    # --- paging and streaming -------------------------------------------------------------------

    def _get_default_iteration_orderings(self) -> list[tuple[str, Order]]:
        """``Meta.ordering``, else the primary key - the order an unordered slice takes its rows
        in."""
        return list(self._apply_default_ordering(self._orderings, self._annotations)) or [
            (primary_key_attribute_name, Order.ASC)
            for primary_key_attribute_name in self.model._meta.primary_key_attribute_names
        ]

    def _get_iterated_query(self) -> Self:
        # Local import: the queryset package imports the query statements.
        from hare.query.queryset.single_rows.single_row import SingleRow

        query = super()._get_iterated_query()
        if not self._distinct_on:
            return query
        # A page boundary has to apply to the rows DISTINCT ON picks, not to the rows it picks from.
        queryset = self._source_queryset._clone()
        queryset._orderings = list(query._orderings)
        return type(self)(SingleRow.with_distinct_on_rows_as_subquery(queryset))

    def _prepare_paging(self) -> list[Callable[[Any], Any]] | None:
        """Orders the pages with the primary key appended when the ordering fields alone can tie -
        then, when a primary key can repeat, the selected annotations reading a to-many relation.

        Returns:
            Readers of each ordering value off an instance, or None to page by ``OFFSET`` - a
            primary key can repeat, or see ``_get_keyset_readers()``.
        """
        rows_repeat = RowMultiplication.rows_repeat_per_primary_key(self)
        orderings = list(self._orderings)
        ordering_field_names = {field_name for field_name, _order in orderings}
        if not self._ordering_field_names_are_unique(ordering_field_names):
            orderings += [
                (primary_key_attribute_name, Order.ASC)
                for primary_key_attribute_name in self.model._meta.primary_key_attribute_names
                if primary_key_attribute_name not in ordering_field_names
            ]
        if rows_repeat:
            ordering_field_names = {field_name for field_name, _order in orderings}
            orderings += [
                (annotation_name, Order.ASC)
                for annotation_name in RowMultiplication.get_row_multiplying_annotation_names(self, selected_only=True)
                if annotation_name not in ordering_field_names
            ]
        self._orderings = orderings
        return None if rows_repeat else self._get_keyset_readers()

    def _get_keyset_readers(self) -> list[Callable[[Any], Any]] | None:
        """Readers of each ordering value off an instance, when every ordering field is a column
        of the model itself - each one ``.only()``/``.defer()`` leaves unloaded is selected as an
        annotation.

        Returns:
            The readers, or None for ``OFFSET`` paging - an ordering across a relation or by an
            annotation, or a window function (a keyset condition would change the rows it is
            computed over).
        """
        direct_fields = self.model._meta.direct_fields
        if not all(field_name in direct_fields for field_name, _order in self._orderings) or (
            RowMultiplication.reads_window_function(self)
        ):
            return None
        loaded_field_names = set(OnlyDeferFields.get_effective_fields_for_select(self))
        readers: list[Callable[[Any], Any]] = []
        for index, (field_name, _order) in enumerate(self._orderings):
            if not loaded_field_names or field_name in loaded_field_names:
                readers.append(attrgetter(field_name))
                continue
            annotation_name = f"{ITERATOR_CURSOR_ANNOTATION_PREFIX}{index}"
            self._annotations = {**self._annotations, annotation_name: F(field_name)}
            readers.append(attrgetter(annotation_name))
        return readers

    def _drop_cursor_values(self, rows: Sequence[Any]) -> None:
        annotation_names = [name for name in self._annotations if name.startswith(ITERATOR_CURSOR_ANNOTATION_PREFIX)]
        if annotation_names:
            for row in rows:
                for annotation_name in annotation_names:
                    row.__dict__.pop(annotation_name, None)

    def _raise_if_not_streamable(self) -> None:
        """Rejects streaming instances with relations to prefetch, and annotations colliding with
        a field.

        Raises:
            QueryError: ``prefetch_related()`` - resolving a prefetch needs every parent instance
                up front.
            FieldError: An annotation collides with a field.
        """
        if self._prefetch_map or self._prefetch_queries:
            raise QueryError(
                "stream() does not support prefetch_related() - resolving a prefetch needs every "
                "parent instance up front to batch its own IN-list queries, which defeats "
                "incremental row-at-a-time streaming; use iterator() or plain iteration instead"
            )
        InstanceHydration.check_no_annotation_field_collision(self)

    def _stream_batches(self, connection: DatabaseClient, chunk_size: int) -> AsyncGenerator[list[Any]]:
        return cast(
            "AsyncGenerator[list[Any]]",
            InstanceHydration.get_model_rows_reader(self).stream_batches(*self._get_parameterized_sql(), chunk_size),
        )

    # Declared for the type checker - the local assignments would infer narrower types.
    _select_related_positions: list[tuple[type[Model], int, Table | str, type[Model], Iterable[str | None]]]

    _decode_plan_key: tuple[str | None, ...] | None

    _annotation_output_fields: dict[str, ModelField[Any] | None]

    _statement_plan: StatementPlan | None

    _base_selects: tuple[Term, ...] | None

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        """Describes this query built into another one (``_get_scoped_copy()``) - its slice is
        among its values.

        Args:
            context: The enclosing query's - this query resolves names against its own
                annotations.

        Returns:
            The description, None for a query that keeps no plan.
        """
        if self._options.keeps_no_plan():
            # Its percent and seed are written into FROM, not bound.
            return None
        query = self._get_scoped_copy()
        query._prepare_build()
        description, _plan = ModelRowsPlanDescriptions.get_queryset_plan_description(query, built_into_another=True)
        return description

    def _prepare_build(self) -> None:
        """Works out what the plan key needs before the build: the relations loaded by default, the
        effective ``.only()``/``.defer()`` fields and the selected columns (``_base_selects``, None
        for every column).
        """
        self._select_related_positions = []
        self._apply_lazy_relation_defaults()
        # .defer() is resolved here, not in defer() itself, so that every .select_related(...) path
        # is already known regardless of call order (.defer() before or after .select_related()).
        # Kept apart from _fields_for_select so the user's own .only()/.defer() state never changes.
        self._effective_fields_for_select = OnlyDeferFields.get_effective_fields_for_select(self)
        # The SELECT the query starts from - built right away for .only()/.defer(), which
        # changes it; for every column, only when the query doesn't run on a plan.
        base_selects: tuple[Term, ...] | None = None
        if self._effective_fields_for_select:
            # select .only() fields
            self.query = self._get_base_query().select()
            # The fields prefetch_related() needs on each instance are selected even when .only()
            # doesn't list them.
            fields_for_select = tuple(
                dict.fromkeys(
                    (
                        *self._effective_fields_for_select,
                        *sorted(OnlyDeferFields.prefetch_map_required_local_fields(self)),
                        *sorted(OnlyDeferFields.select_related_required_local_fields(self)),
                    )
                )
            )
            OnlyDeferFields.get_only(self, fields_for_select)
            QueryJoins.apply_effective_basetable(self)
            # A snapshot of the COLUMNS (not the final decode_plan - the len(_select_related_positions)
            # gate is decided below, after select_related joins) - self.query._selects here holds
            # EXACTLY the base model's own columns, annotations/joins are only added later.
            base_selects = tuple(self.query._selects)
            # The same columns-key component of the decode plans' own key.
            self._decode_plan_key = tuple(term.name if isinstance(term, Field) else None for term in base_selects)
        else:
            # select all fields - the same column names basequery_all_fields selects.
            self._decode_plan_key = InstanceHydration.get_all_fields_selects_key(self.model)
        self._base_selects = base_selects

    def _get_plan(self) -> tuple[PlanDescription | None, StatementPlan | None]:
        return ModelRowsPlanDescriptions.get_queryset_plan_description(self)

    def _describe_plain_statement(self, built_into_another: bool) -> PlanDescription | None:
        """Describes the statement of a plain query from ``plain_plan_slots`` (``QueryKeyCompiler``).

        Args:
            built_into_another: Whether the query is built into another one.

        Returns:
            The description, None for a query that keeps no plan.
        """
        raise NotImplementedError  # pragma: nocoverage - generated from plain_plan_slots

    def _get_plan_key(self, description: PlanDescription) -> tuple[Any, ...]:
        # Kept among the model's own plans.
        return (self.model, *description.structure)

    def _prepare_call_signature_run(self) -> None:
        # The relations loaded by default (a lazy="select" relation is prefetched); the row layout
        # the build works out is the plan's, restored from it.
        self._apply_lazy_relation_defaults()

    def _restore_from_plan(self, plan: StatementPlan) -> None:
        self._decode_plan = plan.decode_plan
        self._decode_plan_is_partial = plan.decode_plan_is_partial
        # _build_select_executor() needs the row layout of the joined relations on every hit.
        self._select_related_positions = list(plan.select_related_positions)

    def _get_plan_record(self) -> dict[str, Any]:
        return {
            "decode_plan": self._decode_plan,
            "decode_plan_is_partial": self._decode_plan_is_partial,
            "select_related_positions": tuple(self._select_related_positions),
            "decode_plan_key": self._decode_plan_key,
        }

    def _select_columns(self) -> None:
        if self._base_selects is None:
            # Annotation columns don't count: ModelRows leaves them out before slicing the row
            # into each model's columns and reads them by name, wherever they landed.
            self._select_related_positions.append(
                (
                    self.model,
                    len(self.model._meta.db_fields),
                    self.model._meta.basetable,
                    self.model,
                    (None,),
                )
            )
            self.query = self._get_base_query_all_fields()
            QueryJoins.apply_effective_basetable(self)
            self._base_selects = tuple(self.query._selects)

    def _apply_ordering(self) -> None:
        QueryOrdering.get_ordering(
            self,
            self.model,
            self._effective_basetable(),
            self._orderings,
            self._annotations,
            self._effective_fields_for_select,
        )

    def _join_loaded_relations(self, value_wrapper_references: RecordedValueReferences | None) -> None:
        # Sorted, not raw set-iteration order - the order of a set[str] follows string hashes and
        # insertion history, while the plan's values list the extra conditions in this loop's
        # order (SelectRelatedJoins.select_related_extra_conditions_in_join_order()).
        for select_related in sorted(self._select_related):
            SelectRelatedJoins.join_select_related(
                self, select_related, value_wrapper_references=value_wrapper_references
            )

    def _finish_statement(self) -> None:
        decode_plan_key = (
            self.model,
            self.dialect,
            # See plan_key's own comment above on why the connection alias itself (not
            # just its dialect) must be part of this key too.
            self._connection.connection_alias if self._connection is not None else None,
            self._decode_plan_key,
            len(self._select_related_positions),
        )
        decode_plan = StatementPlans.decode_plans.get(decode_plan_key, PLAN_CACHE_MISS)
        if decode_plan is PLAN_CACHE_MISS:
            decode_plan = StatementPlans.decode_plans[decode_plan_key] = InstanceHydration.build_decode_plan(
                self, cast("tuple[Term, ...]", self._base_selects)
            )
        self._decode_plan = decode_plan
        self._decode_plan_is_partial = bool(self._effective_fields_for_select)
