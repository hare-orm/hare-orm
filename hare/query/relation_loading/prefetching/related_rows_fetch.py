from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.dialects.base.constants import PREFETCH_MAX_COMPOSITE_ROWS
from hare.query.enums import Connector
from hare.query.expressions import Q
from hare.query.relation_loading.prefetching.prefetch_checks import PrefetchChecks

if TYPE_CHECKING:  # pragma: nocoverage
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
        keys = list(
            dict.fromkeys(
                key
                for key in (tuple(getattr(instance, name) for name in key_field_names) for instance in objs)
                if None not in key
            )
        )
        if not keys:
            return None
        if len(relation_fields) == 1:
            return Q(**{f"{relation_fields[0]}__in": [key[0] for key in keys]})
        return Q.with_connector(Connector.OR, *(Q(**dict(zip(relation_fields, key, strict=True))) for key in keys))

    @staticmethod
    def collect_related_fetch_values(
        objs: Iterable[Model], related_field_name: str, relation_field: str
    ) -> dict[str, list[Any]]:
        """The ``<relation_field>__in`` values fetching the related rows of ``objs`` - their
        Python values, which the filter encodes for the database itself.

        Args:
            objs: The objs whose related rows are fetched.
            related_field_name: The field of ``objs`` the relation targets.
            relation_field: The related model's key field pointing at it.

        Returns:
            ``relation_field`` to the values.
        """
        # A NULL target value (a nullable to_field=) is referenced by no row.
        values: list[Any] = [value for obj in objs if (value := getattr(obj, related_field_name)) is not None]
        return {relation_field: values}

    @staticmethod
    async def fetch_matching_any_group(related_queryset: QuerySet[Any], groups: list[Q]) -> list[Model]:
        """Fetches every row of ``related_queryset`` matching any of ``groups``, OR-ing at most
        ``PREFETCH_MAX_COMPOSITE_ROWS`` of them per query.

        Args:
            related_queryset: The prefetch queryset to filter.
            groups: One AND-group per composite key row.

        Returns:
            The fetched rows of every batch, in batch order.
        """
        fetched: list[Model] = []
        for start in range(0, len(groups), PREFETCH_MAX_COMPOSITE_ROWS):
            batch = groups[start : start + PREFETCH_MAX_COMPOSITE_ROWS]
            fetched.extend(await related_queryset.filter(Q.with_connector(Connector.OR, *batch)))
        return fetched

    @staticmethod
    async def fetch_by_composite_keys(
        objs: Iterable[Model],
        related_queryset: QuerySet[Any],
        key_field_names: tuple[str, ...],
        relation_fields: tuple[str, ...],
    ) -> list[Model]:
        """The rows of ``related_queryset`` whose ``relation_fields`` equal the composite key an
        instance holds in ``key_field_names`` - one AND-group per distinct key, OR-ed in batches. A
        key with a NULL part references no row.

        Args:
            objs: The parent objs.
            related_queryset: The related rows' queryset.
            key_field_names: The fields of the parents holding the key.
            relation_fields: The fields of the related rows referencing it.

        Returns:
            The related rows.
        """
        keys: list[tuple[Any, ...]] = []
        seen_keys: set[tuple[Any, ...]] = set()
        for instance in objs:
            key = tuple(getattr(instance, name) for name in key_field_names)
            if None not in key and key not in seen_keys:
                seen_keys.add(key)
                keys.append(key)
        if not keys:
            return []
        related_queryset = PrefetchChecks.ensure_only_includes_fields(related_queryset, *relation_fields)
        groups = [Q(**dict(zip(relation_fields, key, strict=True))) for key in keys]
        return await RelatedRowsFetch.fetch_matching_any_group(related_queryset, groups)
