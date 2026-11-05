from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.query.relation_loading.prefetching.enums import (
    PrefetchAttachmentType,
    PrefetchRowsStrategy,
    PrefetchStrategy,
)

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
    from hare.fields.relations.fields.relational_field import RelationalField
    from hare.models import Model
    from hare.query.queryset import QuerySet
    from hare.query.relation_loading.prefetching.prefetch_request import PrefetchRequest
    from hare.query.relation_loading.prefetching.prefetch_result import PrefetchResult
    from hare.query.relation_loading.prefetching.prefetcher import Prefetcher


@dataclass(frozen=True, slots=True)
class PrefetchAttachment:
    obj: Model
    field: str
    to_attribute: str | None
    value: Model | list[Model] | None
    attachment_type: PrefetchAttachmentType


@dataclass(frozen=True, slots=True)
class PrefetchKeyBinding:
    model: type[Model]
    source_names: tuple[str, ...]
    target_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PrefetchNode:
    field: str
    relation: RelationalField[Any]
    strategy: PrefetchStrategy
    to_attribute: str | None
    queryset: QuerySet[Any] | None
    target_connection: DatabaseClient | None
    row_slice: slice | None = None
    through_connection: DatabaseClient | None = None
    rows_strategy: PrefetchRowsStrategy = PrefetchRowsStrategy.ALL
    dependent_request: PrefetchRequest | None = None
    key_bindings: tuple[PrefetchKeyBinding, ...] = ()


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
        result: Detached attachments for the enclosing plan.
        through_connection: The already selected client of the through table.
        rows_strategy: The query strategy chosen by the plan.
    """

    prefetcher: Prefetcher
    objs: Iterable[Model]
    field: str
    field_object: ManyToManyFieldInstance[Any]
    to_attribute: str | None
    related_queryset: QuerySet[Any]
    result: PrefetchResult
    through_connection: DatabaseClient
    rows_strategy: PrefetchRowsStrategy
