from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.dialects.base.constants import PREFETCH_MAX_SLICED_COMPOSITE_PARENTS
from hare.query.expressions import Subquery
from hare.query.relation_loading.prefetching.enums import PrefetchRowsStrategy
from hare.query.relation_loading.prefetching.prefetch_checks import PrefetchChecks
from hare.query.relation_loading.prefetching.prefetch_keys import PrefetchKeys
from hare.query.relation_loading.prefetching.related_rows_fetch import RelatedRowsFetch
from hare.query.relation_loading.prefetching.sliced_prefetch import SlicedPrefetch

if TYPE_CHECKING:
    from hare.models import Model
    from hare.query.relation_loading.prefetching.declarations import PrefetchNode
    from hare.query.relation_loading.prefetching.prefetch_result import PrefetchResult


class ReverseRelationPrefetch:
    """The prefetch of a reverse foreign key or one-to-one relation: the related rows fetched by the
    parents' keys and laid out on each parent."""

    @staticmethod
    async def prefetch_reverse_relation(
        objs: Iterable[Model],
        node: PrefetchNode,
        result: PrefetchResult,
    ) -> None:
        """Collects detached reverse foreign-key attachments from the related rows.

        Args:
            objs: The objects.
            node: The bound relation, keys, queryset and selected slice strategy.
            result: Detached assignments collected for publication after all fetches.

        Raises:
            QueryError: An object is unsaved, or didn't load the key the relation reads.
        """
        field, to_attribute, related_query = node.field, node.to_attribute, node.queryset
        # Reverse fetch nodes always have a query supplied by PrefetchPlan.
        assert related_query is not None  # nosec B101
        PrefetchChecks.reject_unsaved_instances(objs)
        binding = node.key_bindings[0]
        related_field_names = binding.source_names
        PrefetchChecks.reject_unloaded_relation_keys(objs, related_field_names, field)
        relation_fields = binding.target_names
        sliced = node.rows_strategy is PrefetchRowsStrategy.NUMBERED
        if (
            sliced
            and len(relation_fields) > 1
            and related_query.get_connection().features.max_expression_depth is not None
        ):
            objs = list(objs)
            if len(objs) > PREFETCH_MAX_SLICED_COMPOSITE_PARENTS:
                # The parents' composite keys are OR-ed - this database bounds the depth of an expression.
                for start in range(0, len(objs), PREFETCH_MAX_SLICED_COMPOSITE_PARENTS):
                    await ReverseRelationPrefetch.prefetch_reverse_relation(
                        objs[start : start + PREFETCH_MAX_SLICED_COMPOSITE_PARENTS],
                        node,
                        result,
                    )
                return
        if sliced:
            # The rows of each parent's slice, picked by their keys in one subquery.
            parent_condition = RelatedRowsFetch.get_parent_keys_condition(objs, related_field_names, relation_fields)
            unsliced = SlicedPrefetch.get_unsliced(related_query)
            if parent_condition is not None:
                key_names = related_query.model._meta.primary_key_attribute_names
                numbered_keys = SlicedPrefetch.get_numbered_rows(
                    related_query, relation_fields, parent_condition
                ).values(*key_names)
                key_path = "pk" if len(key_names) > 1 else key_names[0]
                unsliced = unsliced.filter(**{f"{key_path}__in": Subquery(numbered_keys)})
            related_query = unsliced

        if len(relation_fields) == 1:
            related_field_name = related_field_names[0]
            relation_field = relation_fields[0]
            related_objects_for_fetch = RelatedRowsFetch.collect_related_fetch_values(
                objs, related_field_name, relation_field
            )
            related_object_list = await related_query.filter(
                **{f"{field_name}__in": key_values for field_name, key_values in related_objects_for_fetch.items()}
            )

            related_object_map: dict[Any, list[Model]] = {}
            for entry in related_object_list:
                object_id = getattr(entry, relation_field)
                related_object_map.setdefault(object_id, []).append(entry)
            for instance in objs:
                instance_rows = related_object_map.get(getattr(instance, related_field_name), [])
                result.add_many(
                    instance,
                    field,
                    to_attribute,
                    instance_rows,
                )
            return

        related_object_map = {}
        for entry in await RelatedRowsFetch.fetch_by_composite_keys(
            objs, related_query, related_field_names, relation_fields
        ):
            object_id = PrefetchKeys.get_value(entry, relation_fields)
            related_object_map.setdefault(object_id, []).append(entry)

        for instance in objs:
            row = PrefetchKeys.get_value(instance, related_field_names)
            instance_rows = related_object_map.get(row, [])
            result.add_many(instance, field, to_attribute, instance_rows)

    @staticmethod
    async def prefetch_reverse_one_to_one_relation(
        objs: Iterable[Model],
        node: PrefetchNode,
        result: PrefetchResult,
    ) -> None:
        """Collects detached reverse one-to-one attachments from the related rows.

        Args:
            objs: The objects.
            node: The bound relation, keys and queryset.
            result: Detached assignments collected for publication after all fetches.
        """
        field, to_attribute, related_queryset = node.field, node.to_attribute, node.queryset
        # Reverse fetch nodes always have a query supplied by PrefetchPlan.
        assert related_queryset is not None  # nosec B101
        PrefetchChecks.reject_unsaved_instances(objs)
        binding = node.key_bindings[0]
        related_field_names = binding.source_names
        PrefetchChecks.reject_unloaded_relation_keys(objs, related_field_names, field)
        relation_fields = binding.target_names

        related_object_map: dict[Any, Model] = {}
        if len(relation_fields) == 1:
            related_field_name = related_field_names[0]
            relation_field = relation_fields[0]
            related_objects_for_fetch = RelatedRowsFetch.collect_related_fetch_values(
                objs, related_field_name, relation_field
            )
            related_object_list = await related_queryset.filter(
                **{f"{field_name}__in": key_values for field_name, key_values in related_objects_for_fetch.items()}
            )
            for entry in related_object_list:
                related_object_map[getattr(entry, relation_field)] = entry
            for instance in objs:
                obj = related_object_map.get(getattr(instance, related_field_name))
                # A to_attribute prefetch leaves the bare relation unfetched - several Prefetch(field,
                # to_attribute=...) of one relation don't overwrite each other.
                result.add_reverse(instance, field, to_attribute, obj)
            return

        for entry in await RelatedRowsFetch.fetch_by_composite_keys(
            objs, related_queryset, related_field_names, relation_fields
        ):
            related_object_map[PrefetchKeys.get_value(entry, relation_fields)] = entry

        for instance in objs:
            row = PrefetchKeys.get_value(instance, related_field_names)
            obj = related_object_map.get(row)
            # See the single-column branch above for why to_attribute and the bare cache are mutually
            # exclusive here.
            result.add_reverse(instance, field, to_attribute, obj)
