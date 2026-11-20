from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.constants import PREFETCH_MAX_SLICED_COMPOSITE_PARENTS
from hare.query.constants import PRIMARY_KEY_ALIAS
from hare.query.expressions import Subquery
from hare.query.relation_loading.prefetching.enums import PrefetchRowsStrategy, PrefetchStrategy
from hare.query.relation_loading.prefetching.prefetch_checks import PrefetchChecks
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
        objs: tuple[Model, ...],
        node: PrefetchNode,
        result: PrefetchResult,
    ) -> None:
        """Collect detached reverse foreign-key and one-to-one attachments.

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
                key_names = related_query._binding.primary_key_attribute_names  # type: ignore[union-attr]
                numbered_keys = SlicedPrefetch.get_numbered_rows(
                    related_query, relation_fields, parent_condition
                ).values(*key_names)
                key_path = PRIMARY_KEY_ALIAS if len(key_names) > 1 else key_names[0]
                unsliced = unsliced.filter(**{f"{key_path}__in": Subquery(numbered_keys)})
            related_query = unsliced

        groups = await RelatedRowsFetch.get_groups_by_parent(objs, related_query, related_field_names, relation_fields)
        if node.strategy is PrefetchStrategy.REVERSE_SINGLE:
            for instance, rows in zip(objs, groups, strict=True):
                result.add_reverse(instance, field, to_attribute, rows[-1] if rows else None)
            return

        for instance, rows in zip(objs, groups, strict=True):
            result.add_many(instance, field, to_attribute, rows)
