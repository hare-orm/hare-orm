from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast

from hare.dialects.base.constants import PREFETCH_BIND_PARAMETERS_HEADROOM
from hare.query.queryset.lazy_relation_names import LazyRelationNames
from hare.query.queryset.row_multiplication import RowMultiplication
from hare.query.relation_loading.prefetching.declarations import PrefetchKeyBinding, PrefetchNode
from hare.query.relation_loading.prefetching.enums import PrefetchRowsStrategy, PrefetchStrategy
from hare.query.relation_loading.prefetching.many_to_many_prefetch import ManyToManyPrefetch
from hare.query.relation_loading.prefetching.prefetch import Prefetch
from hare.query.relation_loading.prefetching.prefetch_checks import PrefetchChecks
from hare.query.relation_loading.prefetching.prefetch_request import PrefetchRequest
from hare.query.relation_loading.prefetching.prefetch_result import PrefetchResult
from hare.query.relation_loading.prefetching.sliced_prefetch import SlicedPrefetch

if TYPE_CHECKING:
    from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
    from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
    from hare.fields.relations.fields.relational_field import RelationalField
    from hare.models import Model
    from hare.query.queryset.queryset import QuerySet
    from hare.query.relation_loading.prefetching.prefetcher import Prefetcher


@dataclass(frozen=True, slots=True)
class PrefetchPlan:
    """Binds relation loading before tasks start and publishes only a complete fetch.

    Nodes retain cloned queries, their selected clients and per-parent window slices.
    Existing related objects are traversed through the same result collector, so failure
    cannot leave a completed sibling's attachments on objects already visible to callers.
    """

    prefetcher: Prefetcher
    objs: tuple[Model, ...]
    request: PrefetchRequest
    nodes: tuple[PrefetchNode, ...]

    @classmethod
    def prepare(cls, prefetcher: Prefetcher, objs: Iterable[Model], request: PrefetchRequest) -> PrefetchPlan:
        objs = tuple(objs)
        if not objs:
            return cls(prefetcher, objs, request, ())
        loaded = prefetcher.get_loaded_relation_names(objs, request)
        fetched = replace(
            request,
            prefetch_map={
                key: value
                for key, value in request.prefetch_map.items()
                if key not in loaded and key in prefetcher.model._meta.fetch_fields
            },
        )
        meta = prefetcher.model._meta
        nodes = []
        for field, entries in prefetcher._make_prefetch_queries(fetched).items():
            relation = cast("RelationalField[Any]", meta.fields_map[field])
            if field in meta.backward_foreign_key_fields:
                strategy = PrefetchStrategy.REVERSE
            elif field in meta.backward_one_to_one_fields:
                strategy = PrefetchStrategy.REVERSE_SINGLE
            elif field in meta.many_to_many_fields:
                strategy = PrefetchStrategy.MANY_TO_MANY
            else:
                strategy = PrefetchStrategy.DIRECT
            key_bindings = cls.bind_keys(objs, field, strategy)
            if strategy is PrefetchStrategy.MANY_TO_MANY:
                PrefetchChecks.reject_unsaved_instances(objs)
                for binding in key_bindings:
                    PrefetchChecks.reject_unloaded_relation_keys(
                        (obj for obj in objs if type(obj) is binding.model), binding.source_names, field
                    )
            for to_attribute, queryset in entries:
                queryset = queryset._clone()
                # A prefetch loads one level; lazy SELECT defaults must not traverse a self-relation chain.
                _, lazy_select_fields = LazyRelationNames.get(queryset.model)
                queryset._deferred_related_fields = queryset._deferred_related_fields | lazy_select_fields
                required_projection = tuple(
                    dict.fromkeys(name for binding in key_bindings for name in binding.target_names)
                )
                queryset = PrefetchChecks.ensure_only_includes_fields(queryset, *required_projection)
                dependent_request = None
                if to_attribute and (dependencies := request.prefetch_map.get(to_attribute)):
                    from hare.query.statements.select.model_rows.only_defer_fields import OnlyDeferFields

                    dependent_map: dict[str, set[str | Prefetch]] = {}
                    dependent_queries: dict[str, list[tuple[str | None, QuerySet[Any]]]] = {}
                    Prefetch.add_lookups(queryset.model, dependent_map, dependent_queries, dependencies)
                    required_fields = OnlyDeferFields.get_prefetch_required_fields(
                        queryset.model, queryset._fields_for_select, dependent_map, dependent_queries
                    )
                    queryset = PrefetchChecks.ensure_only_includes_fields(queryset, *required_fields)
                    dependent_request = PrefetchRequest.from_query(queryset, dependent_map, dependent_queries)
                target_connection = queryset.get_connection()
                queryset._apply_connection(target_connection)
                row_slice = None
                through_connection = None
                rows_strategy = PrefetchRowsStrategy.ALL
                sliced = SlicedPrefetch.is_sliced(queryset)
                if strategy in {PrefetchStrategy.DIRECT, PrefetchStrategy.REVERSE_SINGLE}:
                    if sliced:
                        if not SlicedPrefetch.keeps_the_only_row(queryset):
                            rows_strategy = PrefetchRowsStrategy.EMPTY
                        queryset = SlicedPrefetch.get_unsliced(queryset)
                elif strategy is PrefetchStrategy.REVERSE:
                    if sliced:
                        if (
                            SlicedPrefetch.reads_window(queryset)
                            or not queryset.model._meta.has_primary_key
                            or SlicedPrefetch.repeats_rows_of_a_key(queryset)
                        ):
                            row_slice = SlicedPrefetch.get_row_slice(queryset)
                            queryset = SlicedPrefetch.get_ordered_unsliced(queryset)
                        else:
                            rows_strategy = PrefetchRowsStrategy.NUMBERED
                else:
                    many_relation = cast("ManyToManyFieldInstance[Any]", relation)
                    through_connection = ManyToManyPrefetch.get_through_connection(prefetcher, many_relation)
                    rows_strategy = PrefetchRowsStrategy.LINKS
                    if sliced:
                        if (
                            SlicedPrefetch.reads_window(queryset)
                            or target_connection is not through_connection
                            or prefetcher.connection is not through_connection
                            or RowMultiplication.filters_join_multi_valued_relation(queryset)
                        ):
                            row_slice = SlicedPrefetch.get_row_slice(queryset)
                            queryset = SlicedPrefetch.get_ordered_unsliced(queryset)
                        else:
                            rows_strategy = PrefetchRowsStrategy.NUMBERED
                    elif (
                        through_connection is prefetcher.connection
                        and not meta.has_composite_primary_key
                        and not queryset.model._meta.has_composite_primary_key
                        and ManyToManyPrefetch.can_join_through_table(queryset, many_relation, prefetcher.connection)
                        and len({obj.pk for obj in objs})
                        <= through_connection.features.max_bind_parameters - PREFETCH_BIND_PARAMETERS_HEADROOM
                    ):
                        rows_strategy = PrefetchRowsStrategy.JOIN
                nodes.append(
                    PrefetchNode(
                        field,
                        relation,
                        strategy,
                        to_attribute,
                        queryset,
                        target_connection,
                        row_slice,
                        through_connection,
                        rows_strategy,
                        dependent_request,
                        key_bindings,
                    )
                )
        for field in loaded:
            nodes.append(
                PrefetchNode(
                    field,
                    cast("RelationalField[Any]", meta.fields_map[field]),
                    PrefetchStrategy.LOADED,
                    None,
                    None,
                    None,
                )
            )
        return cls(prefetcher, objs, request, tuple(nodes))

    @staticmethod
    def bind_keys(objs: tuple[Model, ...], field: str, strategy: PrefetchStrategy) -> tuple[PrefetchKeyBinding, ...]:
        """Bind key names before fetching, retaining distinct direct-relation source shapes."""
        bindings = []
        for model in dict.fromkeys(type(obj) for obj in objs):
            relation = cast("RelationalField[Any]", model._meta.fields_map[field])
            if strategy is PrefetchStrategy.DIRECT:
                source_names = relation.source_fields
                target_names = relation.to_field_names
            elif strategy is PrefetchStrategy.MANY_TO_MANY:
                source_names = model._meta.primary_key_attribute_names
                target_names = relation.related_model._meta.primary_key_attribute_names
            else:
                source_names = tuple(target.model_field_name for target in relation.to_field_instances)
                target_names = cast("BackwardForeignKeyRelation[Any]", relation).relation_fields
            bindings.append(PrefetchKeyBinding(model, tuple(source_names), tuple(target_names)))
        return tuple(bindings)

    async def fetch(self) -> PrefetchResult:
        """Fetches with structured cancellation; no attachment is published by these tasks."""
        result = PrefetchResult()
        if len(self.nodes) == 1:
            return await self.prefetcher._fetch_node(self.objs, self.nodes[0], self.request)
        tasks = []
        try:
            async with asyncio.TaskGroup() as group:
                for node in self.nodes:
                    tasks.append(group.create_task(self.prefetcher._fetch_node(self.objs, node, self.request)))
        except BaseExceptionGroup as errors:
            # Preserve a single query failure's public exception type after siblings are drained.
            if len(errors.exceptions) == 1:
                raise errors.exceptions[0] from None
            raise
        for task in tasks:
            result.extend(task.result())
        return result
