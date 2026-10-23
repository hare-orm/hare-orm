from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from hare.exceptions import QueryError
from hare.fields.relations.relation_values import RelationValues

if TYPE_CHECKING:
    from hare.fields.relations.fields.relational_field import RelationalField
    from hare.models import Model


class RelationKeyValues:
    """The values a write puts into the key columns of a forward relation it is given - a related object's
    referenced fields, None to clear the relation, or the key's values as a tuple for a composite key."""

    @staticmethod
    def get_key_values(model: type[Model], name: str, value: Any) -> tuple[Any, ...]:
        """The key column values a written relation takes.

        Args:
            model: The written model.
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

        relation = cast("RelationalField[Model]", model._meta.fields_map[name])
        column_count = len(relation.source_fields)
        if value is None:
            return (None,) * column_count
        if column_count > 1 and not isinstance(value, Model):
            if isinstance(value, tuple) and len(value) == column_count:
                return value
            raise QueryError(
                f"{name!r} is a relation to a composite key - write it as a {relation.related_model.__name__} "
                f"instance, a {column_count}-tuple or None, got {value!r}"
            )
        RelationValues.validate_relation_type(model, name, value)
        to_field_names = [to_field.model_field_name for to_field in relation.to_field_instances]
        return tuple(RelationValues.get_relation_key_values(value, to_field_names, f"The write of {name!r}"))
