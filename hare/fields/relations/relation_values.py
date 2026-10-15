from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.exceptions import FieldError, QueryError, ValidationError
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models.model import Model


class RelationValues:
    """The values a relation of an instance is made of: the fields of the instance a relation
    references it by, and the check that an instance assigned to a relation is a saved instance of
    the related model."""

    @staticmethod
    def get_relation_key_values(obj: Model, field_names: Iterable[str], usage: str) -> list[Any]:
        """The values of the fields a relation references this obj by.

        Args:
            obj: The model obj.
            field_names: The referenced (``to_field``) field names.
            usage: What needs the values, for the error message.

        Returns:
            The values, in ``field_names`` order.

        Raises:
            QueryError: One of the fields was left unloaded by ``.only()``/``.defer()``.
        """
        values = []
        for field_name in field_names:
            if not hasattr(obj, field_name):
                model_name = type(obj).__name__
                raise QueryError(
                    f"{usage} needs {model_name}.{field_name}, which this {model_name} instance didn't load "
                    f"(left out by .only()/.defer()) - load '{field_name}' too"
                )
            values.append(getattr(obj, field_name))
        return values

    @staticmethod
    def validate_relation_type(model: type[Model], field_key: str, value: Model | None) -> None:
        """Checks an instance assigned to a forward relation: an instance of the related model, saved,
        with the field the relation references set.

        Args:
            model: The model the relation is declared on.
            field_key: The relation's name.
            value: The assigned instance, None for no related row.

        Raises:
            FieldError: ``field_key`` isn't a foreign key or one-to-one relation.
            ValidationError: ``value`` isn't an instance of the related model.
            QueryError: ``value`` is unsaved, or saved without the referenced field.
        """
        if value is None:
            return

        field = model._meta.fields_map[field_key]
        if not isinstance(field, (OneToOneFieldInstance, ForeignKeyFieldInstance)):
            raise FieldError(
                f"Field '{field_key}' must be a OneToOne or ForeignKey relation, got {type(field).__name__}"
            )

        expected_model = field.related_model
        received_model = type(value)
        if received_model is not expected_model:
            raise ValidationError(
                f"Invalid type for relationship field '{field_key}'. "
                f"Expected model type '{expected_model.__name__}', but got '{received_model.__name__}'. "
                "Make sure you're using the correct model class for this relationship."
            )
        if not value._saved_in_db:
            raise QueryError(f"You should first call .save() on {value!r} before referring to it")
        for to_field_instance in field.to_field_instances:
            to_field_name = to_field_instance.model_field_name
            # hasattr() first: a .only()/.defer() instance that never loaded the target column
            # has no attribute at all, which is not the same as a loaded, still-None one.
            if hasattr(value, to_field_name) and getattr(value, to_field_name) is None:
                raise QueryError(
                    f"{type(value).__name__} is marked saved but its '{to_field_name}' is None (saved by "
                    f"bulk_create() without returning=True?), so '{field_key}' would silently be stored as "
                    "NULL - use bulk_create(returning=True), or reload the object first"
                )
