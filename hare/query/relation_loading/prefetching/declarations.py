from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
    from hare.models import Model
    from hare.query.queryset import QuerySet
    from hare.query.relation_loading.prefetching.prefetcher import Prefetcher


@dataclass(frozen=True, slots=True)
class ManyToManyPrefetchJob:
    """One many-to-many relation prefetched for a set of objs.

    Attributes:
        prefetcher: The prefetcher.
        objs: The objs whose relation is prefetched.
        field: The many-to-many field's name.
        field_object: The many-to-many field.
        to_attribute: The attribute the related rows are set as, None for the relation itself.
        related_queryset: The related rows' queryset.
    """

    prefetcher: Prefetcher
    objs: Iterable[Model]
    field: str
    field_object: ManyToManyFieldInstance[Any]
    to_attribute: str | None
    related_queryset: QuerySet[Any]
