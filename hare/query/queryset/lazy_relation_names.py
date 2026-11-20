from __future__ import annotations

from typing import TYPE_CHECKING

from hare.core.caching.model_cache import ModelCache
from hare.fields.enums import RelationLoadStrategy

if TYPE_CHECKING:
    from hare.models.model_binding import ModelBinding


class LazyRelationNames:
    """The relations a model loads by default - declared with ``lazy=``."""

    @staticmethod
    @ModelCache.fact()
    def get(binding: ModelBinding) -> tuple[frozenset[str], frozenset[str]]:
        """The forward FK/O2O and M2M relations of ``model`` a query loads by default - declared
        ``lazy=RelationLoadStrategy.JOINED`` and ``lazy=RelationLoadStrategy.SELECT``.

        Returns:
            ``(joined relation names, prefetched relation names)``.
        """
        joined: set[str] = set()
        prefetched: set[str] = set()
        for field_name in binding.forward_key_relation_names | binding.many_to_many_fields:
            lazy = getattr(binding.fields_map[field_name], "lazy", None)
            if lazy == RelationLoadStrategy.JOINED:
                joined.add(field_name)
            elif lazy == RelationLoadStrategy.SELECT:
                prefetched.add(field_name)
        return frozenset(joined), frozenset(prefetched)
