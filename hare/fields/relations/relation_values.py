from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.exceptions import FieldError, QueryError, ValidationError
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance

if TYPE_CHECKING:
    from hare.core.hare_context import HareContext
    from hare.models.model import Model
    from hare.models.model_binding import ModelBinding


class RelationValues:
    """The values a relation of an instance is made of: the fields of the instance a relation
    references it by, and the check that an instance assigned to a relation is a saved instance of
    the related model."""

    @staticmethod
    def get_default_component(value: Any, position: int) -> Any:
        """Read one component of a returned default, preserving indexable inputs and NULL."""
        return None if value is None else value[position]

    @staticmethod
    def raise_if_not_of_model(obj: Any, model: type[Model], filter_key: str) -> None:
        """Refuses an instance a relation filter is given that isn't of the related model itself - a
        concrete subclass's row lives in a table of its own, its key no row of the related table's.

        Args:
            obj: The instance.
            model: The related model.
            filter_key: The filter's key, for the message.

        Raises:
            QueryError: The instance is of another model.
        """
        if type(obj) is not model:
            raise QueryError(
                f"{filter_key!r} expects {model.__name__} instances or key values, got a {type(obj).__name__} instance"
            )

    @staticmethod
    def get_relation_key_values(obj: Model, field_names: Iterable[str], usage: str) -> tuple[Any, ...]:
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
            try:
                value = getattr(obj, field_name)
            except AttributeError:
                model_name = type(obj).__name__
                raise QueryError(
                    f"{usage} needs {model_name}.{field_name}, which this {model_name} instance didn't load "
                    f"(left out by .only()/.defer()) - load {field_name!r} too"
                ) from None
            values.append(value)
        return tuple(values)

    @staticmethod
    def get_checked_relation_key_values(
        binding: ModelBinding, field_key: str, value: Model | None, *, context: HareContext | None = None
    ) -> tuple[Any, ...]:
        """Checks an instance assigned to a forward relation: an instance of the related model, saved,
        with the field the relation references set.

        Args:
            binding: The prepared model binding declaring the relation.
            field_key: The relation's name.
            value: The assigned instance, None for no related row.
            context: The application context for checking the assigned instance's origin.

        Returns:
            The referenced key values after validation, or NULL components for None.

        Raises:
            FieldError: ``field_key`` isn't a foreign key or one-to-one relation.
            ValidationError: ``value`` isn't an instance of the related model.
            QueryError: ``value`` is unsaved, or saved without the referenced field.
        """
        field = binding.fields_map[field_key]
        if not isinstance(field, ForeignKeyFieldInstance):
            raise FieldError(
                f"Field {field_key!r} must be a OneToOne or ForeignKey relation, got {type(field).__name__}"
            )

        if value is None:
            return (None,) * len(field.source_fields)

        expected_model = field.related_model
        received_model = type(value)
        if received_model is not expected_model:
            raise ValidationError(
                f"Invalid type for relationship field {field_key!r}. "
                f"Expected model type {expected_model.__name__!r}, but got {received_model.__name__!r}. "
                "Make sure you're using the correct model class for this relationship."
            )
        from hare.models.instances.instance_origins import InstanceOrigins

        InstanceOrigins.check(field.get_related_binding(), context, value, f"Relation {field_key!r}")
        if not value._saved_in_db:
            raise QueryError(f"You should first call .save() on {value!r} before referring to it")
        key_values = RelationValues.get_relation_key_values(value, field.to_field_names, f"Assigning {field_key!r}")
        for to_field_name, key_value in zip(field.to_field_names, key_values, strict=True):
            if key_value is None:
                raise QueryError(
                    f"{type(value).__name__} is marked saved but its {to_field_name!r} is None (saved by "
                    f"bulk_create() without returning=True?), so {field_key!r} would silently be stored as "
                    "NULL - use bulk_create(returning=True), or reload the object first"
                )
        return key_values
