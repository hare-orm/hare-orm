from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.core.caching.model_cache import ModelCache
from hare.models.class_building.constants import LOOKUP_SEPARATOR
from hare.query.constants import PRIMARY_KEY_ALIAS

if TYPE_CHECKING:
    from hare.fields.field import Field
    from hare.models.model_binding import ModelBinding


class SensitiveFilterKeys:
    """Which filter keys compare a ``sensitive=True`` field's values - a filter on such a key binds
    them hidden (``QueryParameters``), wherever the key reaches the field: by its name, through a
    relation, as a part of ``pk``, as a key inside a JSON value, or as a relation compared by a
    sensitive key."""

    @staticmethod
    def reads_sensitive_values(binding: ModelBinding, key: str) -> bool:
        """Whether a filter key compares values of a sensitive field.

        Args:
            binding: The filtered model binding.
            key: The filter key.

        Returns:
            True when the values compared are a sensitive field's - or the related keys a relation
            is compared by are.
        """
        if not SensitiveFilterKeys.reaches_sensitive_fields(binding):
            return False
        relation: Field[Any] | None = None
        for segment in key.split(LOOKUP_SEPARATOR):
            meta = binding
            names = meta.primary_key_attribute_names if segment == PRIMARY_KEY_ALIAS else (segment,)
            fields = [meta.fields_map.get(name) for name in names]
            if None in fields:
                # A lookup or a key inside a value - after a relation, it compares the related keys.
                return relation is not None and SensitiveFilterKeys.compares_sensitive_keys(binding, relation)
            if any(field is not None and field.sensitive for field in fields):
                return True
            related_model = getattr(fields[0], "related_model", None) if len(fields) == 1 else None
            if related_model is None:
                return False
            relation, binding = fields[0], fields[0].get_related_binding()  # type: ignore[union-attr]
        return relation is not None and SensitiveFilterKeys.compares_sensitive_keys(binding, relation)

    @staticmethod
    def compares_sensitive_keys(binding: ModelBinding, relation: Field[Any]) -> bool:
        """Whether the values a relation is compared by - its key columns' or the related primary
        key's - are a sensitive field's.

        Args:
            binding: The binding of the model of the relation.
            relation: The relation.

        Returns:
            True for sensitive keys.
        """
        fields_map = binding.fields_map
        key_names: tuple[str, ...] = getattr(relation, "source_fields", ())
        if key_names and all(name in fields_map for name in key_names):
            return any(fields_map[name].sensitive for name in key_names)
        related_meta = relation.get_related_binding()  # type: ignore[attr-defined]
        return any(related_meta.fields_map[name].sensitive for name in related_meta.primary_key_attribute_names)

    @staticmethod
    @ModelCache.fact(depends_on_other_models=True)
    def reaches_sensitive_fields(binding: ModelBinding) -> bool:
        """Whether a sensitive field is on the model or on a model its relations lead to - answered for
        every model the walk meets at once, so the models of one relation graph are walked once.

        Args:
            binding: The model binding.

        Returns:
            True when a filter key from the model can reach one.
        """
        seen = {binding}
        pending = [binding]
        referring_models: dict[ModelBinding, list[ModelBinding]] = {}
        reaching: set[ModelBinding] = set()
        while pending:
            current_model = pending.pop()
            meta = current_model
            if meta.sensitive_fields:
                reaching.add(current_model)
            for field in meta.fields_map.values():
                related_model = getattr(field, "related_model", None)
                if related_model is None:
                    continue
                related_binding = field.get_related_binding()  # type: ignore[attr-defined]
                referring_models.setdefault(related_binding, []).append(current_model)
                if related_binding not in seen:
                    seen.add(related_binding)
                    pending.append(related_binding)
        pending = list(reaching)
        while pending:
            for referring_model in referring_models.get(pending.pop(), ()):
                if referring_model not in reaching:
                    reaching.add(referring_model)
                    pending.append(referring_model)
        cache = SensitiveFilterKeys.reaches_sensitive_fields.cache  # type: ignore[attr-defined]
        for seen_model in seen:
            cache[seen_model] = seen_model in reaching
        return binding in reaching
