from __future__ import annotations

import inspect
import typing
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.contrib.frameworks.litestar.constants import DTO_PATH_SEPARATOR, TO_MANY_RELATION_TYPES
from hare.contrib.frameworks.litestar.declarations import DTOExtraField
from hare.core.caching.cache import Cache
from hare.exceptions import ConfigurationError, NoValuesFetched

if TYPE_CHECKING:
    from hare.fields.relations.fields.relational_field import RelationalField
    from hare.models import Model


class HareDTOExtraFields:
    """What a ``HareDTO`` answers with besides a model's fields - ``extra_fields``, ``computed`` and
    ``related_values`` - by the model they belong to: a name written as a path through the relations
    (``"author.books_count"``) belongs to the related model, and its rows get the value wherever the
    response holds them.

    Args:
        model_type: The model of the DTO.
        extra_fields: Values read from the row by name - a query's annotation, an attribute set on the row
            after it was read - with their types.
        computed: Properties and methods of the model, typed by their return annotations.
        related_values: Values of a related row, flattened: a name to the path of the related field,
            through relations to one row (``{"author_name": "author.name"}``).
        read_attribute: Reads an attribute of a row - None for a relation the query didn't load.

    Raises:
        ConfigurationError: A path goes through what is no relation to one row or a relation to many
            rows, a computed name is no property or method of the model or has no return annotation,
            a related value names no value field, or a name is a field of the model or declared twice.
    """

    #: The extra fields each set of extra fields found for a class of rows through the class's bases, kept in a
    #: bucket of the set.
    found_row_class_fields: ClassVar[Cache[Any]] = Cache(holds_sql=False, keyed_by_model=False)

    def __init__(
        self,
        model_type: type[Model],
        extra_fields: Mapping[str, Any],
        computed: tuple[str, ...],
        related_values: Mapping[str, str],
        read_attribute: Callable[[object, str], Any],
    ) -> None:
        #: Each model's extra fields, by name.
        self.fields_by_model: dict[type[Model], dict[str, DTOExtraField]] = {}
        #: The computed names of each model.
        self.computed_names_by_model: dict[type[Model], set[str]] = {}
        #: The buckets of the caches these extra fields read (``Cache.get_owner_bucket()``).
        self.cache_buckets: dict[int, Any] = {}
        #: The extra fields of each class of the rows read - its own and its bases'.
        self.fields_by_row_class: dict[type, dict[str, DTOExtraField]] = (
            HareDTOExtraFields.found_row_class_fields.get_owner_bucket(self)
        )
        for path, annotation in extra_fields.items():
            owner, name = HareDTOExtraFields.get_owner(model_type, path, "extra_fields")
            self.add(owner, name, DTOExtraField(annotation | None, HareDTOExtraFields.get_attribute_reader(name)))
        for path in computed:
            owner, name = HareDTOExtraFields.get_owner(model_type, path, "computed")
            self.add(owner, name, HareDTOExtraFields.get_computed_field(owner, name))
            self.computed_names_by_model.setdefault(owner, set()).add(name)
        for path, value_path in related_values.items():
            owner, name = HareDTOExtraFields.get_owner(model_type, path, "related_values")
            self.add(owner, name, HareDTOExtraFields.get_related_value_field(owner, name, value_path, read_attribute))

    def add(self, owner: type[Model], name: str, extra_field: DTOExtraField) -> None:
        """Adds an extra field of a model.

        Raises:
            ConfigurationError: The name is a field of the model or declared already.
        """
        model_fields = self.fields_by_model.setdefault(owner, {})
        if name in owner._meta.fields_map or name in model_fields:
            raise ConfigurationError(
                f"HareDTO of {owner.__name__}: {name!r} is a field of the model or declared twice - an extra "
                "field takes a name of its own"
            )
        model_fields[name] = extra_field

    def get_fields(self, model_type: type[Model]) -> dict[str, DTOExtraField]:
        """The extra fields of a model's rows.

        Args:
            model_type: The model.

        Returns:
            The fields by name.
        """
        return self.fields_by_model.get(model_type, {})

    def find(self, row: object, name: str) -> DTOExtraField | None:
        """The extra field of a row's model - of its class or a base it inherits it from.

        Args:
            row: A row.
            name: The field's name.

        Returns:
            The field, None for no extra field of the name.
        """
        row_class = type(row)
        row_class_fields = self.fields_by_row_class.get(row_class)
        if row_class_fields is None:
            row_class_fields = self.fields_by_row_class[row_class] = self.get_row_class_fields(row_class)
        return row_class_fields.get(name)

    def get_row_class_fields(self, row_class: type) -> dict[str, DTOExtraField]:
        """The extra fields of a class of rows - its own and its bases', a computed one reading the property or
        method of that class: a subclass of the model may override it.

        Args:
            row_class: The class.

        Returns:
            The fields by name.
        """
        row_class_fields: dict[str, DTOExtraField] = {}
        for model_type in reversed(row_class.__mro__):
            computed_names = self.computed_names_by_model.get(model_type, ())
            for name, extra_field in self.fields_by_model.get(model_type, {}).items():
                row_class_fields[name] = (
                    extra_field._replace(read=HareDTOExtraFields.get_computed_reader(model_type, name, row_class))
                    if name in computed_names
                    else extra_field
                )
        return row_class_fields

    @staticmethod
    def get_owner(model_type: type[Model], path: str, option: str) -> tuple[type[Model], str]:
        """The model a declared name belongs to, and the name on it.

        Args:
            model_type: The model of the DTO.
            path: The name, written as a path through the relations or not.
            option: The option it is declared in, for the error message.

        Returns:
            The model and the name.

        Raises:
            ConfigurationError: A part of the path before the name is no relation of its model.
        """
        *relation_names, name = path.split(DTO_PATH_SEPARATOR)
        owner = model_type
        for relation_name in relation_names:
            field = owner._meta.fields_map.get(relation_name)
            if field is None or field.relation_type is None:
                raise ConfigurationError(
                    f"HareDTO of {model_type.__name__}, {option}: {path!r} - {relation_name!r} is no relation of "
                    f"{owner.__name__}"
                )
            owner = field.related_model  # type: ignore[attr-defined]
        return owner, name

    @staticmethod
    def get_attribute_reader(name: str) -> Callable[[object], Any]:
        """Reads an attribute of a row - None where the row has none (an annotation the query didn't make)."""

        # A closure - the reader of every row of a response; a partial of a method costs a third more a call.
        def read(row: object) -> Any:
            return getattr(row, name, None)

        return read

    @staticmethod
    def get_computed_field(owner: type[Model], name: str) -> DTOExtraField:
        """The extra field of a property or method of a model.

        Raises:
            ConfigurationError: The name is no property or method of the model, or it has no return
                annotation.
        """
        attribute = inspect.getattr_static(owner, name, None)
        if isinstance(attribute, property):
            function = attribute.fget
        elif inspect.isfunction(attribute):
            function = attribute
        else:
            raise ConfigurationError(f"HareDTO of {owner.__name__}, computed: {name!r} is no property or method of it")
        annotation = typing.get_type_hints(function).get("return") if function is not None else None
        if annotation is None:
            raise ConfigurationError(
                f"HareDTO of {owner.__name__}, computed: {name!r} has no return annotation - the response's type"
            )
        return DTOExtraField(annotation | None, HareDTOExtraFields.get_computed_reader(owner, name, owner))

    @staticmethod
    def get_computed_reader(owner: type[Model], name: str, row_class: type) -> Callable[[object], Any]:
        """Reads a computed value from the rows of a class - its property or method of the name.

        Args:
            owner: The model the value is declared on.
            name: The property or method.
            row_class: The class of the rows - the model or a subclass of it.

        Returns:
            The reader.
        """
        row_attribute = inspect.getattr_static(row_class, name)
        row_function = cast(
            "Callable[[object], Any]", row_attribute.fget if isinstance(row_attribute, property) else row_attribute
        )

        def read(row: object) -> Any:
            # Called directly, not through getattr(): an AttributeError of a property makes Python ask the
            # model's __getattr__, which names the property missing instead of the field it didn't fetch.
            try:
                return row_function(row)
            except NoValuesFetched:
                return None
            except AttributeError as error:
                # Litestar's transfer code takes an AttributeError for a missing value and answers null - a
                # mistake inside the property is no missing value.
                raise RuntimeError(f"{owner.__name__}.{name} of a HareDTO response failed: {error}") from error

        return read

    @staticmethod
    def get_related_value_field(
        owner: type[Model], name: str, value_path: str, read_attribute: Callable[[object, str], Any]
    ) -> DTOExtraField:
        """The extra field of a value of a related row - each step read with ``read_attribute``.

        Raises:
            ConfigurationError: The path goes through what is no relation to one row, or ends at no value
                field.
        """
        *relation_names, field_name = value_path.split(DTO_PATH_SEPARATOR)
        related_model = owner
        for relation_name in relation_names:
            field = related_model._meta.fields_map.get(relation_name)
            if field is None or field.relation_type is None or field.relation_type in TO_MANY_RELATION_TYPES:
                raise ConfigurationError(
                    f"HareDTO of {owner.__name__}, related_values: {name!r} - {relation_name!r} is no relation of "
                    f"{related_model.__name__} to one row"
                )
            related_model = cast("RelationalField[Any]", field).related_model
        value_field = related_model._meta.fields_map.get(field_name)
        if not relation_names or value_field is None or value_field.relation_type is not None:
            raise ConfigurationError(
                f"HareDTO of {owner.__name__}, related_values: {name!r} - {value_path!r} names no value field of a "
                "related row"
            )

        path_parts = (*relation_names, field_name)

        def read(row: object) -> Any:
            value: Any = row
            for part in path_parts:
                value = read_attribute(value, part)
                # A relation the query didn't load, or one of an empty key - nothing on the other side.
                if value is None:
                    return None
            return value

        return DTOExtraField(value_field.get_annotation() | None, read)
