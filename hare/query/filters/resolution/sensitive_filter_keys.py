from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.core.caching.model_cache import ModelCache
from hare.models.class_building.constants import LOOKUP_SEPARATOR

if TYPE_CHECKING:
    from hare.fields.field import Field
    from hare.models import Model


class SensitiveFilterKeys:
    """Which filter keys compare a ``sensitive=True`` field's values - a filter on such a key binds
    them hidden (``QueryParameters``), wherever the key reaches the field: by its name, through a
    relation, as a part of ``pk``, as a key inside a JSON value, or as a relation compared by a
    sensitive key."""

    @staticmethod
    def reads_sensitive_values(model: type[Model], key: str) -> bool:
        """Whether a filter key compares values of a sensitive field.

        Args:
            model: The filtered model.
            key: The filter key.

        Returns:
            True when the values compared are a sensitive field's - or the related keys a relation
            is compared by are.
        """
        if not SensitiveFilterKeys.reaches_sensitive_fields(model):
            return False
        relation: Field[Any] | None = None
        relation_model = model
        for segment in key.split(LOOKUP_SEPARATOR):
            meta = model._meta
            names = meta.primary_key_attribute_names if segment == "pk" else (segment,)
            fields = [meta.fields_map.get(name) for name in names]
            if None in fields:
                # A lookup or a key inside a value - after a relation, it compares the related keys.
                return relation is not None and SensitiveFilterKeys.compares_sensitive_keys(relation_model, relation)
            if any(field is not None and field.sensitive for field in fields):
                return True
            related_model = getattr(fields[0], "related_model", None) if len(fields) == 1 else None
            if related_model is None:
                return False
            relation, relation_model, model = fields[0], model, related_model
        return relation is not None and SensitiveFilterKeys.compares_sensitive_keys(relation_model, relation)

    @staticmethod
    def compares_sensitive_keys(model: type[Model], relation: Field[Any]) -> bool:
        """Whether the values a relation is compared by - its key columns' or the related primary
        key's - are a sensitive field's.

        Args:
            model: The model of the relation.
            relation: The relation.

        Returns:
            True for sensitive keys.
        """
        fields_map = model._meta.fields_map
        key_names: tuple[str, ...] = getattr(relation, "source_fields", ())
        if key_names and all(name in fields_map for name in key_names):
            return any(fields_map[name].sensitive for name in key_names)
        related_meta = relation.related_model._meta  # type: ignore[attr-defined]
        return any(related_meta.fields_map[name].sensitive for name in related_meta.primary_key_attribute_names)

    @staticmethod
    @ModelCache.fact(depends_on_other_models=True)
    def reaches_sensitive_fields(model: type[Model]) -> bool:
        """Whether a sensitive field is on the model or on a model its relations lead to.

        Args:
            model: The model.

        Returns:
            True when a filter key from the model can reach one.
        """
        seen = {model}
        pending = [model]
        while pending:
            meta = pending.pop()._meta
            if meta.sensitive_fields:
                return True
            for field in meta.fields_map.values():
                related_model = getattr(field, "related_model", None)
                if related_model is not None and related_model not in seen:
                    seen.add(related_model)
                    pending.append(related_model)
        return False
