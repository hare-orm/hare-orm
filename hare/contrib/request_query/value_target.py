"""What a filter compares, so filters of the same values are recognised whatever their key."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any

from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance

if TYPE_CHECKING:
    from hare.query.lookup_info.lookup_info import LookupInfo


@dataclasses.dataclass(frozen=True, slots=True)
class ValueTarget:
    """The values a filter compares: a field, reached through relations.

    A forward relation compared by its key is its own key column: ``author``, ``author__in``,
    ``author__pk__in`` and ``author_id__in`` all compare ``Book.author_id``. A date part, a JSON
    path or a lookup doesn't change the target: ``published_at__year`` and
    ``published_at__gte`` compare ``published_at``. A composite relation compares its complete
    source key in column order.

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
        if relations and isinstance(relation := relations[-1], ForeignKeyFieldInstance):
            fields = field if isinstance(field, tuple) else (field,)
            if fields == relation.to_field_instances:
                owner = relations[-2].get_related_binding() if len(relations) > 1 else lookup_info.binding  # type: ignore[attr-defined]
                source_fields = tuple(owner.fields_map[name] for name in relation.source_fields)
                return cls(
                    relations=relations[:-1], field=source_fields[0] if len(source_fields) == 1 else source_fields
                )
        return cls(relations=relations, field=field)
