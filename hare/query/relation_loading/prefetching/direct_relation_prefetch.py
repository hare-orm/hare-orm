from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from itertools import chain
from typing import TYPE_CHECKING, Any

from hare.query.enums import Connector
from hare.query.expressions import Q
from hare.query.key_comparison import KeyComparison
from hare.query.relation_loading.prefetching.prefetch_checks import PrefetchChecks
from hare.query.relation_loading.prefetching.prefetch_keys import PrefetchKeys
from hare.query.relation_loading.prefetching.related_rows_fetch import RelatedRowsFetch

if TYPE_CHECKING:
    from hare.models import Model
    from hare.query.relation_loading.prefetching.declarations import PrefetchNode
    from hare.query.relation_loading.prefetching.prefetch_result import PrefetchResult


class DirectRelationPrefetch:
    """Fetch forward relations using prepared tuple keys, then retain detached attachments."""

    @staticmethod
    async def prefetch_direct_relation(
        objs: Iterable[Model],
        node: PrefetchNode,
        result: PrefetchResult,
    ) -> None:
        """Match each source class against its declared target fields without changing objects.

        Null-containing keys match no target. Distinct source classes can reference different
        unique fields of the same model, so a target key includes its field-name tuple.
        The plan has already applied required projection, connection and per-parent slice.
        """
        bindings = {binding.model: binding for binding in node.key_bindings}
        keys_by_shape: defaultdict[tuple[str, ...], dict[tuple[Any, ...], None]] = defaultdict(dict)
        for obj in objs:
            binding = bindings[type(obj)]
            PrefetchChecks.reject_unloaded_relation_keys((obj,), binding.source_names, node.field)
            key = PrefetchKeys.get_value(obj, binding.source_names)
            if None not in key:
                keys_by_shape[binding.target_names][key] = None
        queryset = node.queryset
        # A prepared direct fetch always owns a queryset; only loaded nodes omit it.
        assert queryset is not None  # nosec B101
        related_objects: list[Model] = []
        if keys_by_shape:
            if all(len(names) == 1 for names in keys_by_shape):
                conditions = [
                    Q(**{f"{names[0]}__in": [key[0] for key in keys]}) for names, keys in keys_by_shape.items()
                ]
                related_objects = await queryset.filter(Q.with_connector(Connector.OR, *conditions))
            else:
                groups = [
                    Q(**dict(zip(names, key, strict=True))) for names, keys in keys_by_shape.items() for key in keys
                ]
                related_objects = await RelatedRowsFetch.fetch_matching_any_group(
                    queryset,
                    groups,
                    key_width=max(map(len, keys_by_shape)),
                )
        readers = {}
        targets: dict[tuple[tuple[str, ...], tuple[Any, ...]], Model] = {}
        if related_objects:
            connection = queryset.get_connection()
            target_binding = queryset._binding
            for names, requested in keys_by_shape.items():
                stored = [(PrefetchKeys.get_value(obj, names), obj) for obj in related_objects]
                fields = [target_binding.fields_map[name] for name in names]  # type: ignore[union-attr]
                reader = await KeyComparison.get_key_reader(
                    connection, fields, chain(requested, (key for key, _ in stored))
                )
                readers[names] = reader
                targets.update(((names, reader(key)), obj) for key, obj in stored)
        for obj in objs:
            binding = bindings[type(obj)]
            key = PrefetchKeys.get_value(obj, binding.source_names)
            read = readers.get(binding.target_names)
            related = None if read is None or None in key else targets.get((binding.target_names, read(key)))
            result.add_forward(obj, node.field, node.to_attribute, related)
