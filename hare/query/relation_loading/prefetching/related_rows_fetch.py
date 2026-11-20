from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Iterable, Sequence
from itertools import chain
from typing import TYPE_CHECKING, Any

from hare.native.native_modules import NativeModules
from hare.query.enums import Connector
from hare.query.expressions import Q
from hare.query.key_comparison import KeyComparison
from hare.query.relation_loading.prefetching.prefetch_checks import PrefetchChecks
from hare.query.relation_loading.prefetching.prefetch_keys import PrefetchKeys

if TYPE_CHECKING:
    from hare.models import Model
    from hare.query.queryset import QuerySet


class RelatedRowsFetch:
    """The related rows of a prefetch fetched by the parents' keys - one IN list, one condition per
    group of keys, or the rows of a composite key compared column by column."""

    @staticmethod
    def get_parent_keys_condition(
        objs: Iterable[Model], key_field_names: tuple[str, ...], relation_fields: tuple[str, ...]
    ) -> Q | None:
        """The condition taking the related rows of ``objs``: ``<relation field>__in`` for a
        single-column key, an AND-group per key OR-ed for a composite one.

        Args:
            objs: The parent objs.
            key_field_names: The fields of the parents holding the key.
            relation_fields: The fields of the related rows referencing it.

        Returns:
            The condition; None when no parent has a key.
        """
        keys = PrefetchKeys.get_distinct_values(objs, key_field_names)
        if not keys:
            return None
        if len(relation_fields) == 1:
            return Q(**{f"{relation_fields[0]}__in": [key[0] for key in keys]})
        return Q.with_connector(Connector.OR, *(Q(**dict(zip(relation_fields, key, strict=True))) for key in keys))

    @staticmethod
    async def fetch_matching_any_group(
        related_queryset: QuerySet[Any], groups: list[Q], *, key_width: int
    ) -> list[Model]:
        """Fetches every row of ``related_queryset`` matching any of ``groups``, OR-ing at most
        ``PREFETCH_MAX_COMPOSITE_ROWS`` of them per query.

        Args:
            related_queryset: The prefetch queryset to filter.
            groups: One AND-group per composite key row.
            key_width: The number of parameters contributed by each complete key.

        Returns:
            The fetched rows of every batch, in batch order.
        """
        fetched: list[Model] = []
        batch_size = PrefetchKeys.get_batch_size(related_queryset.get_connection(), key_width)
        for start in range(0, len(groups), batch_size):
            batch = groups[start : start + batch_size]
            fetched.extend(await related_queryset.filter(Q.with_connector(Connector.OR, *batch)))
        return fetched

    @staticmethod
    async def fetch_by_keys(
        queryset: QuerySet[Any], names: tuple[str, ...], keys: Collection[tuple[Any, ...]]
    ) -> list[Model]:
        """Read rows by homogeneous keys, batching composite comparisons."""
        if len(names) == 1:
            return await queryset.filter(**{f"{names[0]}__in": [key[0] for key in keys]})
        if not keys:
            return []
        groups = [Q(**dict(zip(names, key, strict=True))) for key in keys]
        return await RelatedRowsFetch.fetch_matching_any_group(queryset, groups, key_width=len(names))

    @staticmethod
    async def get_groups_by_parent(
        objs: Sequence[Model],
        related_queryset: QuerySet[Any],
        key_field_names: tuple[str, ...],
        relation_fields: tuple[str, ...],
    ) -> list[list[Model]]:
        """Fetch and match related rows, retaining the queryset's order for each parent."""
        requested = [PrefetchKeys.get_value(obj, key_field_names) for obj in objs]
        keys = dict.fromkeys(key for key in requested if None not in key)
        related_queryset = PrefetchChecks.ensure_only_includes_fields(related_queryset, *relation_fields)
        rows = await RelatedRowsFetch.fetch_by_keys(related_queryset, relation_fields, keys)
        if not rows:
            return [[] for _ in objs]

        connection = related_queryset.get_connection()
        fields = [related_queryset._binding.fields_map[name] for name in relation_fields]  # type: ignore[union-attr]
        if len(relation_fields) == 1 and not KeyComparison.needs_conversion(connection, fields):
            by_key = NativeModules.rows.group_by_attribute(rows, relation_fields[0], True)
            return [by_key.get(key[0], []) for key in requested]

        stored_keys = [PrefetchKeys.get_value(row, relation_fields) for row in rows]
        read = await KeyComparison.get_key_reader(connection, fields, chain(keys, stored_keys))
        grouped: defaultdict[tuple[Any, ...], list[Model]] = defaultdict(list)
        for key, row in zip(stored_keys, rows, strict=True):
            grouped[read(key)].append(row)
        return [grouped.get(read(key), []) for key in requested]
