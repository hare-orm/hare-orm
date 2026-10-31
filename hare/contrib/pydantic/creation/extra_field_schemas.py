from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from pydantic import AliasChoices, AliasPath, Field as PydanticField

from hare.contrib.pydantic.constants import TO_MANY_RELATION_TYPES
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.contrib.pydantic.creation.pydantic_model_creator import PydanticModelCreator
    from hare.fields.field import Field
    from hare.models import Model


class ExtraFieldSchemas:
    """The fields a model's schema gets besides the model's: ``extra_fields`` - values read from the row by
    name, a query's annotation or an attribute set on the row after it was read, with their types - and
    ``related_values`` - a value of a related row, flattened (``{"author_name": "author.name"}``). Both are
    ``None`` where the row has no value; a name written as a path through the relations belongs to the
    related model's schema."""

    @staticmethod
    def raise_if_wrong(
        model: type[Model],
        extra_fields: Mapping[str, Any],
        related_values: Mapping[str, str],
        taken_names: set[str],
    ) -> None:
        """Refuses a declaration of the model's own schema that names nothing it can hold.

        Args:
            model: The model.
            extra_fields: The extra fields, by name or path.
            related_values: The related values, by name or path.
            taken_names: The names the schema has already - fields, relations, computed fields.

        Raises:
            ConfigurationError: A path goes through what is no relation, a name of the model's own schema is
                taken or declared twice, or a related value's path goes through what is no relation to one
                row or ends at no value field.
        """
        declared: set[str] = set()
        for option, names in (("extra_fields", extra_fields), ("related_values", related_values)):
            for path in names:
                relation_name, _, rest = path.partition(".")
                if rest:
                    field = model._meta.fields_map.get(relation_name)
                    if field is None or field.relation_type is None:
                        raise ConfigurationError(
                            f"pydantic_model_creator({model.__name__}, {option}=...): {path!r} - {relation_name!r} "
                            "is no relation of the model"
                        )
                    continue
                if path in taken_names or path in declared:
                    raise ConfigurationError(
                        f"pydantic_model_creator({model.__name__}, {option}=...): {path!r} is a field of the schema "
                        "or declared twice - an extra field takes a name of its own"
                    )
                declared.add(path)
        for name, value_path in related_values.items():
            if "." not in name:
                ExtraFieldSchemas.get_related_value_field(model, name, value_path)

    @staticmethod
    def get_shown_related_values(creator: PydanticModelCreator) -> dict[str, str]:
        """The related values selected by ``include``/``exclude`` - none of a ``sensitive=True`` field under
        ``exclude_sensitive``, like the field itself.

        Args:
            creator: The creator of the schema.

        Returns:
            The related values, by name.
        """
        related_values = {
            name: value_path
            for name, value_path in creator.meta.model_related_values.items()
            if creator.meta.selects(name)
        }
        if not creator._exclude_sensitive:
            return related_values
        model = creator._model_class
        return {
            name: value_path
            for name, value_path in related_values.items()
            if not ExtraFieldSchemas.get_related_value_field(model, name, value_path).sensitive
        }

    @staticmethod
    def get_related_value_field(model: type[Model], name: str, value_path: str) -> Field[Any]:
        """The field of a related row a related value reads.

        Args:
            model: The model the value is declared on.
            name: The value's name.
            value_path: The path of the related field.

        Returns:
            The field.

        Raises:
            ConfigurationError: The path goes through what is no relation to one row, or ends at no value
                field.
        """
        *relation_names, field_name = value_path.split(".")
        related_model = model
        for relation_name in relation_names:
            field = related_model._meta.fields_map.get(relation_name)
            if field is None or field.relation_type is None or field.relation_type in TO_MANY_RELATION_TYPES:
                raise ConfigurationError(
                    f"pydantic_model_creator({model.__name__}, related_values=...): {name!r} - {relation_name!r} is "
                    f"no relation of {related_model.__name__} to one row"
                )
            related_model = field.related_model  # type: ignore[attr-defined]
        value_field = related_model._meta.fields_map.get(field_name)
        if not relation_names or value_field is None or value_field.relation_type is not None:
            raise ConfigurationError(
                f"pydantic_model_creator({model.__name__}, related_values=...): {name!r} - {value_path!r} names no "
                "value field of a related row"
            )
        return value_field

    @staticmethod
    def add_to_schema(creator: PydanticModelCreator) -> None:
        """Adds the model's own extra fields and related values to the schema being built - read from the
        row, ``None`` where it has no value; a related value is also taken by its own name from a mapping.
        A write schema (``exclude_readonly``) gets none: a row is never written with them. Sorted with the
        other fields under ``sort_alphabetically``; a related value of a sensitive field is dropped under
        ``exclude_sensitive``.

        Args:
            creator: The creator of the schema.
        """
        if creator._exclude_read_only:
            return
        model = creator._model_class
        extra_fields = creator.meta.model_extra_fields
        related_values = ExtraFieldSchemas.get_shown_related_values(creator)
        for name, annotation in extra_fields.items():
            if creator.meta.selects(name):
                creator._properties[name] = (annotation | None, PydanticField(default=None))
        for name, value_path in related_values.items():
            annotation = ExtraFieldSchemas.get_related_value_field(model, name, value_path).get_annotation()
            alias = AliasChoices(name, AliasPath(*value_path.split(".")))
            creator._properties[name] = (annotation | None, PydanticField(default=None, validation_alias=alias))
        if creator.meta.sort_alphabetically and (extra_fields or related_values):
            creator._properties = dict(sorted(creator._properties.items()))
