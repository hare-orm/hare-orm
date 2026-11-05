from __future__ import annotations

from copy import copy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.core.caching.cache_bucket import CacheBucket
    from hare.fields.field import Field
    from hare.fields.relations.bound_relation import BoundRelation
    from hare.models import Model


@dataclass(slots=True)
class BoundField:
    """Runtime identity and conversion state, separate from a field's declaration values.

    The publication snapshot has no binding to a live model. Cache buckets belong to this
    binding and are populated exclusively through Cache; copying a runtime field starts
    fresh buckets and normalization state.
    """

    model: type[Model] | None = None
    name: str = ""
    column: str | None = None
    reference: Field[Any] | None = None
    declaration: Field[Any] | None = None
    relation: BoundRelation | None = None
    static_default_is_normalized: bool | None = None
    assign_normalized_types: frozenset[type] | None = None
    cache_buckets: dict[int, CacheBucket[Any]] = field(default_factory=dict)

    def copy_runtime(self) -> BoundField:
        """Keep identity for a field copy that will receive a new declaration on publication."""
        return BoundField(self.model, self.name, self.column, self.reference, relation=copy(self.relation))
