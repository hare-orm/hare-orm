from __future__ import annotations

from collections import defaultdict
from collections.abc import AsyncGenerator, Callable, Iterable, Sequence
from operator import attrgetter
from typing import TYPE_CHECKING, Any, ClassVar, Self, TypeVar, cast

from hare.core.lookup_path import LookupPath
from hare.core.model_cache import ModelCache
from hare.exceptions import FieldError, MultipleObjectsReturned, QueryError
from hare.fields.base.field import Field as ModelField
from hare.fields.relations.fields.backward_fk_relation import BackwardFKRelation
from hare.fields.relations.fields.backward_one_to_one_relation import BackwardOneToOneRelation
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.fields.relations.fields.relational_field import RelationalField
from hare.query.constants import ITERATOR_CURSOR_ANNOTATION_PREFIX, PLAN_CACHE_MISS
from hare.query.expressions import F, Q
from hare.query.expressions.aggregate_paths.aggregated_multi_valued_paths import AggregatedMultiValuedPaths
from hare.query.expressions.outer_query_state import outer_expression_context
from hare.query.expressions.value_refs.value_ref_types import RecordedValueRefs
from hare.query.lookup_paths import LookupPaths
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.statement_plan import StatementPlan
from hare.query.plans.statement_plans import StatementPlans
from hare.query.queryset.query_options import QueryOptions
from hare.query.queryset.query_spec import QuerySpec
from hare.query.relation_loading.prefetch_request import PrefetchRequest
from hare.query.rows.model_rows import ModelRows
from hare.query.statements.select.select_query import SelectQuery
from hare.sql import Order
from hare.sql.terms.field import Field
from hare.utils import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.transaction_client import TransactionClient
    from hare.fields.relations.fields.relational_field import RelationalField
    from hare.models import Model
    from hare.query.queryset.queryset import QuerySet
    from hare.query.rows.hydration_layout import HydrationEntry
    from hare.sql import Table
    from hare.sql.queries.builder.query_builder import QueryBuilder
    from hare.sql.terms.base.term import Term

TModel = TypeVar("TModel", bound="Model")


class ModelRowsQuery(SelectQuery[TModel]):
    """Builds and runs the SQL of a queryset returning model instances - made from the queryset
    for one build."""

    __slots__ = (
        "_source_queryset",
        "_base_selects",
        "_effective_fields_for_select",
        "_select_related_idx",
        "_decode_plan",
        "_decode_plan_is_partial",
        "_decode_plan_key",
    )

    # Slots a copy made to run once (_get_execution_query()) gets its own shallow copy of.
    mutable_clone_slots: ClassVar[dict[str, str]] = QuerySpec.MUTABLE_SPEC_SLOTS | {
        "_joined_tables": "[]",
        "_joined_tables_set": "set()",
    }

    def __init__(self, source_queryset: QuerySet[TModel, Any]) -> None:
        """
        Args:
            source_queryset: The queryset whose rows the query builds.
        """
        QuerySpec.copy_spec(source_queryset, self)
        self._source_queryset = source_queryset
        self._init_build_state()
        # The .only() whitelist, or .defer()'s blacklist expanded into one - rebuilt by every
        # _make_query() call.
        self._effective_fields_for_select: tuple[str, ...] = ()
        self._select_related_idx: list[
            tuple[type[Model], int, Table | str, type[Model], Iterable[str | None]]
        ] = []  # format with: model,idx,model_name,parent_model
        self._decode_plan: tuple[HydrationEntry, ...] | None = None
        self._decode_plan_is_partial: bool = False
        self._decode_plan_key: tuple[str | None, ...] | None = None
        self._base_selects: tuple[Term, ...] | None = None

    def _get_row_join_lookups(self) -> list[str]:
        return self._get_ordering_lookups()

    def _get_group_key_lookups(self) -> list[str]:
        # Model instances aren't grouped by .group_by() fields - only values()/values_list(),
        # count() and aggregate() are.
        return []

    def _check_no_annotation_field_collision(self) -> None:
        if not self._annotations:
            return
        # An annotation named like a field, or like a property of the model (`pk`), would overwrite
        # it on the hydrated instance. An .alias() is never selected and can't collide.
        reserved_names = self.model._meta.fields_map.keys() | self._get_model_property_names(self.model)
        colliding_keys = reserved_names & (self._annotations.keys() - self._alias_keys)
        if colliding_keys:
            raise FieldError(
                f"annotate() key(s) {sorted(colliding_keys)} collide with existing field(s) or "
                f"reserved attribute(s) on model {self.model.__name__} - fetching full model instances "
                "would silently corrupt hydration. Use a different annotation name, or "
                ".values()/.values_list()."
            )

    @staticmethod
    def _get_model_property_names(model: type[Model]) -> set[str]:
        """Collects the names of all properties defined on a model class and its bases.

        Args:
            model: The model class to inspect.

        Returns:
            The property names, e.g. ``{"pk"}``.
        """
        return {
            name
            for klass in model.__mro__
            for name, attribute in vars(klass).items()
            if isinstance(attribute, property)
        }

    def _get_model_rows(self) -> ModelRows:
        """How this query's rows are read into instances - shared by ``_execute()`` and
        ``stream()``.

        Returns:
            The reader.
        """
        return ModelRows(
            self.model,
            self._db,
            select_related_buckets=self._select_related_idx,
            decode_plan=self._decode_plan,
            decode_plan_is_partial=self._decode_plan_is_partial,
            # An .alias() key is never selected - there's no column to read it from.
            annotations=list(self._annotations.keys() - self._alias_keys),
            annotation_fields=self._get_annotation_fields(),
        )

    def _get_prefetch_request(self) -> PrefetchRequest | None:
        """What the instances this queryset loads get prefetched, with the settings of this
        queryset the prefetch queries take over - None when nothing is prefetched.

        Returns:
            The request, or None.
        """
        if not (self._prefetch_map or self._prefetch_queries):
            return None
        return PrefetchRequest(
            self._prefetch_map,
            self._prefetch_queries,
            visibility=self._visibility,
            select_for_update=self._select_for_update,
            select_for_update_nowait=self._select_for_update_nowait,
            select_for_update_skip_locked=self._select_for_update_skip_locked,
            select_for_update_no_key=self._select_for_update_no_key,
            db_explicitly_chosen=self._db_explicitly_chosen,
        )

    async def _execute(self) -> list[TModel]:
        self._check_no_annotation_field_collision()
        instance_list = cast(
            "list[TModel]",
            await self._get_model_rows().fetch(*self._get_parameterized_sql(), self._get_prefetch_request()),
        )
        if self._single:
            if len(instance_list) == 1:
                return instance_list[0]  # type: ignore[return-value]
            if not instance_list:
                if self._raise_does_not_exist:
                    self._raise_object_does_not_exist()
                return None  # type: ignore[return-value]
            raise MultipleObjectsReturned(self.model)
        if self._reverse_result_order:
            return instance_list[::-1]
        return instance_list

    # --- paging and streaming -------------------------------------------------------------------

    def _get_default_iteration_orderings(self) -> list[tuple[str, Order]]:
        """``Meta.ordering``, else the primary key - the order an unordered slice takes its rows
        in."""
        return list(self._apply_default_ordering(self._orderings, self._annotations)) or [
            (pk_attr_name, Order.ASC) for pk_attr_name in self.model._meta.pk_attr_names
        ]

    def _get_iterated_query(self) -> Self:
        query = super()._get_iterated_query()
        if not self._distinct_on:
            return query
        # A page boundary has to apply to the rows DISTINCT ON picks, not to the rows it picks from.
        queryset = self._source_queryset._clone()
        queryset._orderings = list(query._orderings)
        return type(self)(queryset._with_distinct_on_rows_as_subquery())

    def _prepare_paging(self) -> list[Callable[[Any], Any]] | None:
        """Orders the pages with the primary key appended when the ordering fields alone can tie -
        then, when a primary key can repeat, the selected annotations reading a to-many relation.

        Returns:
            Readers of each ordering value off an instance, or None to page by ``OFFSET`` - a
            primary key can repeat, or see ``_get_keyset_readers()``.
        """
        rows_repeat = self._rows_repeat_per_primary_key()
        orderings = list(self._orderings)
        ordering_field_names = {field_name for field_name, _order in orderings}
        if not self._ordering_field_names_are_unique(ordering_field_names):
            orderings += [
                (pk_attr_name, Order.ASC)
                for pk_attr_name in self.model._meta.pk_attr_names
                if pk_attr_name not in ordering_field_names
            ]
        if rows_repeat:
            ordering_field_names = {field_name for field_name, _order in orderings}
            orderings += [
                (annotation_name, Order.ASC)
                for annotation_name in self._get_row_multiplying_annotation_names(selected_only=True)
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
            self._reads_window_function()
        ):
            return None
        loaded_field_names = set(self._get_effective_fields_for_select())
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

    def _rows_repeat_per_primary_key(self) -> bool:
        """Whether the query can return one primary key several times - a filter or annotation joins a
        to-many relation (with ``.distinct()``, only a selected annotation keeps the rows apart).
        Keyset paging would drop repeats across a page boundary.

        Returns:
            True if a primary key can repeat.
        """
        if self._distinct:
            return bool(self._get_row_multiplying_annotation_names(selected_only=True))
        if self._q_objects_join_multi_valued_relation():
            return True
        return bool(self._get_row_multiplying_annotation_names(selected_only=False))

    def _q_objects_join_multi_valued_relation(self) -> bool:
        """Whether resolving the filters joins a to-many relation into this query - also through
        an ``OuterRef("relation__field")`` of an ``Exists(...)``/``Subquery(...)`` condition -
        other than one a filter narrows to one related row (``tags=1``).

        Returns:
            True if a filter joins a to-many relation that can repeat a row.
        """
        multi_valued_paths = AggregatedMultiValuedPaths()
        multi_valued_paths.is_recording_row_joins = True
        claiming_generation_by_path: dict[str, int] = {}
        for q_object in self._q_objects:
            q_object.get_result(
                self._get_probe_expression_context(
                    self._annotations, multi_valued_paths, multi_valued_join_generations=claiming_generation_by_path
                )
            )
        multi_valued_paths.record_row_pinning_conditions(
            self.model, self._get_top_level_conditions_by_generation(), claiming_generation_by_path
        )
        return bool(set(multi_valued_paths.row_paths) - multi_valued_paths.pinned_paths)

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
        self._check_no_annotation_field_collision()

    def _stream_batches(self, db: TransactionClient, chunk_size: int) -> AsyncGenerator[list[Any]]:
        return cast(
            "AsyncGenerator[list[Any]]",
            self._get_model_rows().stream_batches(*self._get_parameterized_sql(), chunk_size),
        )

    def _prefetch_map_required_local_fields(self) -> set[str]:
        """The base model's fields ``.prefetch_related()`` needs on each instance - the key column of a
        forward relation, the primary key for a reverse or many-to-many one - selected whatever
        ``.only()``/``.defer()`` say. Covers relation names and ``Prefetch(...)`` objects.
        """
        required: set[str] = set()
        for field_name in self._prefetch_map.keys() | self._prefetch_queries.keys():
            field = self.model._meta.fields_map[field_name]
            if isinstance(field, (ForeignKeyFieldInstance, OneToOneFieldInstance)):
                required.update(field.source_fields)
            elif isinstance(field, (BackwardFKRelation, BackwardOneToOneRelation)):
                required.update(f.model_field_name for f in field.to_field_instances)
            elif isinstance(field, ManyToManyFieldInstance):
                required.add(cast("str", self.model._meta.pk_attr))
        return required

    def _select_related_required_local_fields(self) -> set[str]:
        """The base model's key columns an explicit ``.select_related()`` relation needs on each
        instance, selected whatever ``.only()`` says. A relation joined only by its
        ``lazy="joined"`` default isn't covered - ``.only()`` not naming it opts out. Only the first
        hop of a path matters.
        """
        required: set[str] = set()
        for relation_path in self._explicitly_select_related:
            field = self.model._meta.fields_map.get(relation_path.partition("__")[0])
            if isinstance(field, (ForeignKeyFieldInstance, OneToOneFieldInstance)):
                required.update(field.source_fields)
        return required

    def _get_effective_fields_for_select(self) -> tuple[str, ...]:
        """The ``.only()`` whitelist, or ``.defer()``'s blacklist expanded into one.

        Returns:
            The field expressions to select, empty when neither restriction is set.
        """
        if self._deferred_fields:
            return self._get_deferred_fields()
        return self._fields_for_select

    def _get_deferred_fields(self) -> tuple[str, ...]:
        """Expands the ``.defer()`` fields into the expression list ``.only()`` takes, with every field
        of each ``.select_related()`` path added - ``.defer()`` prunes only the base model's
        columns.

        Returns:
            The expanded field expressions, in model field order.
        """
        fetch_fields = self.model._meta.fetch_fields
        deferred_fields = set(self._deferred_fields)
        expressions = [
            field_name
            for field_name in self.model._meta.fields_map
            if field_name not in fetch_fields and field_name not in deferred_fields
        ]
        expressions.extend(sorted(self._prefetch_map_required_local_fields() - set(expressions)))

        for relation_path in sorted(self._select_related):
            lookup_path = LookupPath.parse(self.model, relation_path, crosses_last=True)
            for position, related_field in enumerate(lookup_path.relations):
                prefix = "__".join(lookup_path.relation_names[: position + 1])
                related_model = related_field.related_model
                expressions.extend(
                    f"{prefix}__{field_name}"
                    for field_name in related_model._meta.fields_map
                    if field_name not in related_model._meta.fetch_fields
                )

        return tuple(expressions)

    def _join_select_related(
        self,
        lookup_expression: str,
        value_wrapper_refs: RecordedValueRefs | None = None,
    ) -> tuple[type[Model], Table]:
        fields = LookupPaths.expand_expression(self.model, lookup_expression)
        extra_condition = self._select_related_extra_conditions.get(lookup_expression)
        model: type[Model] = self.model
        table = self.model._meta.basetable
        path: tuple[str | None, ...] = (None,)
        for index, field in enumerate(fields):
            field = cast("RelationalField[Model]", field)
            path = path + (field.model_field_name,)
            is_last_field = index == len(fields) - 1
            table = self._join_table_by_field(
                table,
                field.model_field_name,
                field,
                extra_condition if is_last_field else None,
                value_wrapper_refs=value_wrapper_refs,
            )

            # With a subset of fields selected, a relation's own fields are added only when
            # .select_related() asked for it and .only()/.defer() name none of its fields.
            step_lookup_expression = "__".join(cast("tuple[str, ...]", path[1:]))
            if self._effective_fields_for_select and (
                lookup_expression not in self._explicitly_select_related
                or any(
                    field_name.startswith(f"{step_lookup_expression}__")
                    for field_name in self._effective_fields_for_select
                )
            ):
                model = field.related_model
                continue

            related_fields = field.related_model._meta.db_fields
            append_item = (
                field.related_model,
                len(related_fields),
                field.model_field_name,
                model,
                path,
            )
            model = field.related_model
            if append_item in self._select_related_idx:
                # An earlier path already selects this hop - select_related("a", "a__b") walks
                # "a" twice; its columns are in the row once.
                continue
            self._select_related_idx.append(append_item)
            related_projection_reverse = field.related_model._meta.fields_db_projection_reverse
            self.query = self.query.select(
                *[
                    table[related_field].as_(
                        LookupPaths.safe_select_label(
                            table.get_table_name(), related_projection_reverse[related_field]
                        )
                    )
                    for related_field in related_fields
                ]
            )
        return model, table

    @staticmethod
    def _get_pk_select_field_names(model: type[Model]) -> tuple[str, ...]:
        """The selectable field name(s) backing ``model``'s primary key, in pk order.

        Args:
            model: The model whose primary key is selected.

        Returns:
            Keys of ``model._meta.fields_db_projection``.
        """
        meta = model._meta
        return tuple(
            name if name in meta.fields_db_projection else cast("str", meta.fields_map[name].source_field)
            for name in meta.pk_attr_names
        )

    def _get_only(self, only_lookup_expressions: tuple[str, ...]) -> None:
        # Group fields by fetch fields, e.g. ["a__b", "a__c"] -> {"a": ["b", "c"]}.
        # The direct fields of the model are the ones that would have the key "".
        fetch_to_fields = defaultdict(list)
        # Shallowest paths first: _select_related_idx gets the entries really selected before the
        # fillers that only tell an empty instance is made.
        for expression in sorted(only_lookup_expressions, key=lambda x: x.count("__")):
            fetch_fields_lookup, __, field_name = expression.rpartition("__")
            fetch_to_fields[fetch_fields_lookup].append(field_name)

        # select direct model fields which would have the key "": {"": ["a", "b"]}
        data_fields = fetch_to_fields.pop("", None)
        if data_fields:
            table = self.model._meta.basetable

            # Annotation names in .only() select no model column - the bucket size counts real
            # columns only. An .alias() named like a field means the field.
            def is_real_field(field: str) -> bool:
                return field not in self._annotations or (
                    field in self._alias_keys and field in self.model._meta.fields_db_projection
                )

            own_field_count = sum(1 for field in data_fields if is_real_field(field))
            self._select_related_idx.append(
                (
                    self.model,
                    own_field_count,
                    table,
                    self.model,
                    (None,),
                )
            )
            try:
                self.query = self.query.select(
                    *[
                        table[self.model._meta.fields_db_projection[field]].as_(field)
                        for field in data_fields
                        if is_real_field(field)
                    ]
                )
            except KeyError as e:
                raise FieldError(f'Unknown field "{e.args[0]}" for model "{self.model.__name__}"') from e

        else:
            # even though no data fields are selected, we need to let the executor know
            # that an empty instance of the model has to be created
            self._select_related_idx.append(
                (
                    self.model,
                    0,
                    self.model._meta.basetable,
                    self.model,
                    (None,),
                )
            )

        # Select fields of related models, e.g. {"a": ["b", "c"]}
        added_paths = set()
        for fetch_fields_lookup, data_fields in fetch_to_fields.items():
            fetch_fields = LookupPaths.expand_expression(self.model, fetch_fields_lookup)
            # The relation's extra_condition goes into this JOIN too - it is built before
            # select_related's, and the first JOIN is the one kept.
            extra_condition = self._select_related_extra_conditions.get(fetch_fields_lookup)
            model: type[Model] = self.model
            referring_model = model
            table = self.model._meta.basetable
            path: tuple[str | None, ...] = (None,)
            for i, fetch_field in enumerate(fetch_fields):
                field = cast("RelationalField[Model]", fetch_field)
                path = path + (field.model_field_name,)
                is_last_field = i == len(fetch_fields) - 1
                table = self._join_table_by_field(
                    table, field.model_field_name, field, extra_condition if is_last_field else None
                )
                referring_model = model
                model = field.related_model

                if path in added_paths:
                    continue

                # Every hop selects its related model's primary key: only the key tells a joined row
                # from a LEFT JOIN miss.
                hop_field_names = list(
                    dict.fromkeys((*self._get_pk_select_field_names(model), *(data_fields if is_last_field else ())))
                )
                self._select_related_idx.append((model, len(hop_field_names), table, referring_model, path))
                added_paths.add(path)
                try:
                    self.query = self.query.select(
                        *[
                            table[model._meta.fields_db_projection[hop_field_name]].as_(
                                LookupPaths.safe_select_label(table.get_table_name(), hop_field_name)
                            )
                            for hop_field_name in hop_field_names
                        ]
                    )
                except KeyError as e:
                    raise FieldError(f'Unknown field "{e.args[0]}" for model "{model.__name__}"') from e

    # model -> (its basequery_all_fields, the column-name key of that query's SELECT list) - see
    # _get_all_fields_selects_key().
    ALL_FIELDS_SELECTS_KEY_CACHE: ClassVar[ModelCache[tuple[QueryBuilder, tuple[str | None, ...]]]] = ModelCache()

    # Declared for the type checker - the local assignments would infer narrower types.
    _select_related_idx: "list[tuple[type[Model], int, Table | str, type[Model], Iterable[str | None]]]"

    _decode_plan_key: "tuple[str | None, ...] | None"

    _annotation_output_fields: "dict[str, ModelField[Any] | None]"

    _statement_plan: "StatementPlan | None"

    _base_selects: "tuple[Term, ...] | None"

    def _build_decode_plan(self, base_selects: tuple[Term, ...]) -> tuple["HydrationEntry", ...] | None:
        """The positional decode plan of the base model's selected columns (``base_selects``, taken
        before annotations and joins are added). None when rows aren't read by position.
        """
        if len(self._select_related_idx) != 1 or not self.features.supports_positional_rows:
            return None
        entry_by_column = self.model._meta.get_hydration_layout(self._db).entry_by_column
        plan: list[HydrationEntry] = []
        for term in base_selects:
            if not isinstance(term, Field):
                return None
            entry = entry_by_column.get(term.name)
            if entry is None:
                return None
            plan.append(entry)
        return tuple(plan)

    @staticmethod
    def _get_all_fields_selects_key(model: type[Model]) -> tuple[str | None, ...]:
        """The column names of ``model``'s every-column SELECT list - part of the decode plans' key and
        of the plan key. Kept per model beside the query it was read from.

        Args:
            model: The model.

        Returns:
            One name per selected term, None for a term that isn't a plain column.
        """
        basequery_all_fields = cast("QueryBuilder", model._meta.basequery_all_fields)
        cached = ModelRowsQuery.ALL_FIELDS_SELECTS_KEY_CACHE.get(model)
        if cached is not None and cached[0] is basequery_all_fields:
            return cached[1]
        selects_key = tuple(term.name if isinstance(term, Field) else None for term in basequery_all_fields._selects)
        ModelRowsQuery.ALL_FIELDS_SELECTS_KEY_CACHE[model] = (basequery_all_fields, selects_key)
        return selects_key

    def _has_plain_query_state(self) -> bool:
        """Whether the query selects every column of its model and has only filters, ordering, a plain
        ``.distinct()`` and a slice - it then takes the short plan key.

        Returns:
            True for a plain query.
        """
        if self._select_related or self._effective_fields_for_select or self._annotations:
            return False
        options = self._options
        return options is QueryOptions.DEFAULT or not (
            options.select_related_extra_conditions
            or options.cursor_values
            or options.before_cursor_values
            or options.distinct_on
            or options.select_for_update
        )

    def _select_related_extra_conditions_in_join_order(self) -> list[tuple[str, Q]]:
        """`(path, extra_condition)` pairs in the order `_build_query()`'s own
        `sorted(self._select_related)` loop resolves them in - the order their values are
        recorded in.

        Returns:
            The pairs.
        """
        return [
            (path, self._select_related_extra_conditions[path])
            for path in sorted(self._select_related)
            if path in self._select_related_extra_conditions
        ]

    def _get_extra_conditions_plan_description(self) -> PlanDescription | None:
        """Describes the ``Select(relation, extra_condition=Q(...))`` conditions in join order -
        two queries selecting the same relations with other conditions never share a plan.

        Returns:
            The description, None when a condition keeps no plan.
        """
        paths_and_conditions = self._select_related_extra_conditions_in_join_order()
        if not paths_and_conditions:
            return PlanDescription((), [])
        conditions_description = self._get_conditions_plan_description(
            [extra_condition for _path, extra_condition in paths_and_conditions], PlanContext.EMPTY
        )
        if conditions_description is None:
            return None
        return PlanDescription(
            (tuple(path for path, _extra_condition in paths_and_conditions), conditions_description.structure),
            conditions_description.values,
        )

    def _get_queryset_plan_description(
        self, connection_bound: bool = True
    ) -> tuple[PlanDescription | None, StatementPlan | None]:
        """Describes this query - the structure is the plan key - and finds the plan under it. A plain
        query takes a short key and its plan is looked up first. The values are, in recording order:
        the annotations', the filters', the keyset boundaries', the ``Select(extra_condition=...)``
        conditions', then the CTEs'.

        Args:
            connection_bound: False for this query built into another one: the key leaves out the
                connection, and no plan is looked up.

        Returns:
            The description, None for a query that keeps no plan, and the plan found.
        """
        extension_calls_structure = self._get_extension_calls_structure()
        if extension_calls_structure is None:
            return None, None
        ctes_description = self._get_ctes_plan_description()
        if ctes_description is None:
            return None, None
        plan: StatementPlan | None = None
        if self._has_plain_query_state():
            # _get_conditions_plan_description(), inline - the most frequent query.
            filter_structures = []
            filter_values: list[Any] = []
            for q in self._q_objects:
                q_description = q.get_plan_description(PlanContext.EMPTY)
                if q_description is None:
                    return None, None
                filter_structures.append(q_description.structure)
                filter_values += q_description.values
            filters_structure = tuple(filter_structures)
            # The full key below with every part a plain query leaves at its default dropped - a
            # key of another length never equals a full one.
            plan_key = (
                *self._get_connection_structure(connection_bound),
                self._get_visibility_structure(),
                self._decode_plan_key,
                tuple(self._orderings),
                self._default_ordering_disabled,
                self._distinct,
                # Whether the rows are sliced - the LIMIT and OFFSET values are bound per query,
                # their presence is part of the SQL text.
                self._limit is not None,
                self._offset is not None,
                filters_structure,
                ctes_description.structure,
                extension_calls_structure,
                Timezone.get_rendered_zone_name(),
            )
            # A plan kept under the key is the answer to whether the query keeps one - except
            # inside a correlated subquery, where that also depends on the enclosing query.
            if connection_bound and outer_expression_context.get() is None:
                plan = StatementPlans.find_for_model(self.model, plan_key)
            if plan is None and not self._query_state_is_plannable():
                return None, None
            if ctes_description.values:
                filter_values += ctes_description.values
            return PlanDescription(plan_key, filter_values), plan
        if not self._query_state_is_plannable():
            return None, None
        filters_description = self._get_filters_plan_description()
        if filters_description is None:
            return None, None
        extra_conditions_description = self._get_extra_conditions_plan_description()
        if extra_conditions_description is None:
            return None, None
        cursor_description = self._get_cursor_plan_description()
        expression_terms_description = self._get_expression_terms_plan_description()
        if expression_terms_description is None:
            return None, None
        plan_key = (
            # The connection alias too, not only its dialect: two connections of one dialect can
            # point a model at different tables.
            *self._get_connection_structure(connection_bound),
            self._get_visibility_structure(),
            # Only the base table's own selected columns - the joined relations and the
            # .only()/.defer() fields their columns follow are parts of their own.
            self._decode_plan_key,
            tuple(sorted(self._select_related)),
            self._effective_fields_for_select,
            extra_conditions_description.structure,
            filters_description.structure,
            tuple(self._orderings),
            # Whether Meta.ordering's own fallback is suppressed (by order_by() with no
            # arguments, or on a union branch) - self._orderings is empty either way.
            self._default_ordering_disabled,
            cursor_description.structure,
            self._distinct,
            tuple(self._distinct_on),
            # Whether the rows are sliced - the LIMIT and OFFSET values are bound per query,
            # their presence is part of the SQL text.
            self._limit is not None,
            self._offset is not None,
            self._select_for_update,
            self._select_for_update_nowait,
            self._select_for_update_skip_locked,
            tuple(sorted(self._select_for_update_of)),
            self._select_for_update_no_key,
            # A `field__year`/`__month`/... lookup on a DatetimeField renders the configured zone
            # into its criterion (`Extract(..., zone_name=...)`), not a bound value.
            Timezone.get_rendered_zone_name(),
            ctes_description.structure,
            # The dialect's QuerySet method calls, written into the query after the build.
            extension_calls_structure,
            # The annotations a GROUP BY/ORDER BY/DISTINCT ON resolves again - their values last.
            expression_terms_description.structure,
        )
        if connection_bound:
            plan = StatementPlans.find_for_model(self.model, plan_key)
        values = (
            filters_description.values
            + cursor_description.values
            + extra_conditions_description.values
            + ctes_description.values
            + expression_terms_description.values
        )
        return PlanDescription(plan_key, values), plan

    def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
        """Describes this query built into another one (``_get_scoped_copy()``) - its slice is
        among its values.

        Args:
            context: The enclosing query's - this query resolves names against its own
                annotations.

        Returns:
            The description, None for a query that keeps no plan.
        """
        query = self._get_scoped_copy()
        query._prepare_build()
        description, _plan = query._get_queryset_plan_description(connection_bound=False)
        if description is None:
            return None
        slice_bounds = [bound for bound in (query._limit, query._offset) if bound is not None]
        return PlanDescription(
            (type(query), query.model, *description.structure, query._is_none), description.values + slice_bounds
        )

    def _prepare_build(self) -> None:
        """Works out what the plan key needs before the build: the relations loaded by default, the
        effective ``.only()``/``.defer()`` fields and the selected columns (``_base_selects``, None
        for every column).
        """
        self._select_related_idx = []
        self._apply_lazy_relation_defaults()
        # .defer() is resolved here, not in defer() itself, so that every .select_related(...) path
        # is already known regardless of call order (.defer() before or after .select_related()).
        # Kept apart from _fields_for_select so the user's own .only()/.defer() state never changes.
        self._effective_fields_for_select = self._get_effective_fields_for_select()
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
                        *sorted(self._prefetch_map_required_local_fields()),
                        *sorted(self._select_related_required_local_fields()),
                    )
                )
            )
            self._get_only(fields_for_select)
            self._apply_effective_basetable()
            # A snapshot of the COLUMNS (not the final decode_plan - the len(_select_related_idx)
            # gate is decided below, after select_related joins) - self.query._selects here holds
            # EXACTLY the base model's own columns, annotations/joins are only added later.
            base_selects = tuple(self.query._selects)
            # The same columns-key component of the decode plans' own key.
            self._decode_plan_key = tuple(term.name if isinstance(term, Field) else None for term in base_selects)
        else:
            # select all fields - the same column names basequery_all_fields selects.
            self._decode_plan_key = self._get_all_fields_selects_key(self.model)
        self._base_selects = base_selects

    def _get_plan(self) -> tuple[PlanDescription | None, StatementPlan | None]:
        return self._get_queryset_plan_description()

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
        self._select_related_idx = list(plan.select_related_idx)

    def _get_plan_record(self) -> dict[str, Any]:
        return {
            "decode_plan": self._decode_plan,
            "decode_plan_is_partial": self._decode_plan_is_partial,
            "select_related_idx": tuple(self._select_related_idx),
            "decode_plan_key": self._decode_plan_key,
        }

    def _select_columns(self) -> None:
        if self._base_selects is None:
            # Annotation columns don't count: ModelRows leaves them out before slicing the row
            # into each model's columns and reads them by name, wherever they landed.
            self._select_related_idx.append(
                (
                    self.model,
                    len(self.model._meta.db_fields),
                    self.model._meta.basetable,
                    self.model,
                    (None,),
                )
            )
            self.query = self._get_base_query_all_fields()
            self._apply_effective_basetable()
            self._base_selects = tuple(self.query._selects)

    def _apply_ordering(self) -> None:
        self.get_ordering(
            self.model,
            self._effective_basetable(),
            self._orderings,
            self._annotations,
            self._effective_fields_for_select,
        )

    def _join_loaded_relations(self, value_wrapper_refs: RecordedValueRefs | None) -> None:
        # Sorted, not raw set-iteration order - the order of a set[str] follows string hashes and
        # insertion history, while the plan's values list the extra conditions in this loop's
        # order (_select_related_extra_conditions_in_join_order()).
        for select_related in sorted(self._select_related):
            self._join_select_related(select_related, value_wrapper_refs=value_wrapper_refs)

    def _finish_statement(self) -> None:
        decode_plan_key = (
            self.model,
            self.dialect,
            # See plan_key's own comment above on why the connection alias itself (not
            # just its dialect) must be part of this key too.
            self._db.connection_name if self._db is not None else None,
            self._decode_plan_key,
            len(self._select_related_idx),
        )
        decode_plan = StatementPlans.decode_plans.get(decode_plan_key, PLAN_CACHE_MISS)
        if decode_plan is PLAN_CACHE_MISS:
            decode_plan = StatementPlans.decode_plans[decode_plan_key] = self._build_decode_plan(
                cast("tuple[Term, ...]", self._base_selects)
            )
        self._decode_plan = decode_plan
        self._decode_plan_is_partial = bool(self._effective_fields_for_select)
