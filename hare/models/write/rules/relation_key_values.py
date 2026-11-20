from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.exceptions import QueryError
from hare.fields.relations.relation_values import RelationValues

if TYPE_CHECKING:
    from hare.fields.relations.fields.relational_field import RelationalField
    from hare.models import Model
    from hare.models.model_binding import ModelBinding


class RelationKeyValues:
    """The values a write puts into the key columns of a forward relation it is given - a related object's
    referenced fields, None to clear the relation, or the key's values as a tuple for a composite key."""

    @staticmethod
    def get_key_values(binding: ModelBinding, name: str, value: Any) -> tuple[Any, ...]:
        """The key column values a written relation takes.

        Args:
            binding: The written model.
            name: The relation's name.
            value: A related object, None, or - for a composite key - a tuple of the key's values.

        Returns:
            One value per key column, in key order.

        Raises:
            ValidationError: The value is an object of another model, or no object at all.
            QueryError: The object is unsaved or didn't load a referenced field; or a tuple of another length
                is given for a composite key.
        """
        # Local import: the models package imports the write package.
        from hare.models import Model

        relation: RelationalField[Model] = binding.fields_map[name]  # type: ignore[assignment]
        column_count = len(relation.source_fields)
        if value is None:
            return (None,) * column_count
        if column_count > 1 and not isinstance(value, Model):
            if isinstance(value, tuple) and len(value) == column_count:
                return value
            message_field = next(
                (field for field in (relation, *relation.to_field_instances) if field.sensitive), relation
            )
            raise QueryError(
                f"{name!r} is a relation to a composite key - write it as a {relation.related_model.__name__} "
                f"instance, a {column_count}-tuple or None, got {message_field.get_value_for_message(value)}"
            )
        return RelationValues.get_checked_relation_key_values(binding, name, value)
