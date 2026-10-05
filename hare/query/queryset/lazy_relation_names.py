from __future__ import annotations

from typing import TYPE_CHECKING

from hare.core.caching.model_cache import ModelCache
from hare.fields.enums import RelationLoadStrategy

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


class LazyRelationNames:
    """The relations a model loads by default - declared with ``lazy=``."""

    @staticmethod
    @ModelCache.fact()
    def get(model: type[Model]) -> tuple[frozenset[str], frozenset[str]]:
        """The forward FK/O2O and M2M relations of ``model`` a query loads by default - declared
        ``lazy=RelationLoadStrategy.JOINED`` and ``lazy=RelationLoadStrategy.SELECT``.

        Returns:
            ``(joined relation names, prefetched relation names)``.
        """
        meta = model._meta
        joined: set[str] = set()
        prefetched: set[str] = set()
        for field_name in meta.foreign_key_fields | meta.one_to_one_fields | meta.many_to_many_fields:
            lazy = getattr(meta.fields_map[field_name], "lazy", None)
            if lazy == RelationLoadStrategy.JOINED:
                joined.add(field_name)
            elif lazy == RelationLoadStrategy.SELECT:
                prefetched.add(field_name)
        return frozenset(joined), frozenset(prefetched)
