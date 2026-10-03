"""What a filter compares, so filters of the same values are recognised whatever their key."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.lookup_info.lookup_info import LookupInfo


@dataclasses.dataclass(frozen=True, slots=True)
class ValueTarget:
    """The values a filter compares: a field, reached through relations.

    A forward relation compared by its key is its own key column: ``author``, ``author__in``,
    ``author__pk__in`` and ``author_id__in`` all compare ``Book.author_id``. A date part, a JSON
    path or a lookup doesn't change the target: ``published_at__year`` and
    ``published_at__gte`` compare ``published_at``.

    Attributes:
        relations: The relations crossed.
        field: The field compared - a tuple of fields for a composite key.
    """

    relations: tuple[Any, ...]
    field: Any

    @classmethod
    def of(cls, lookup_info: LookupInfo) -> ValueTarget:
        """The target of a filter, as the ORM describes it.

        Args:
            lookup_info: The filter's description.

        Returns:
            The target.
        """
        relations = lookup_info.relations
        field = lookup_info.field
        if relations:
            last_relation = relations[-1]
            owner_model = getattr(relations[-2], "related_model") if len(relations) > 1 else lookup_info.model
            source_field_name = getattr(last_relation, "source_field", None)
            source_field = owner_model._meta.fields_map.get(source_field_name) if source_field_name else None
            if source_field is not None and source_field is not last_relation:
                relation_key = owner_model._meta.get_lookup_info(last_relation.model_field_name).field
                if field is relation_key:
                    return cls(relations=relations[:-1], field=source_field)
        return cls(relations=relations, field=field)
