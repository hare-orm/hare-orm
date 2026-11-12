from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import ValidationError
from hare.fields.enums import HeldValueStep
from hare.fields.field import Field

if TYPE_CHECKING:
    from hare.ddl.schema_objects.enum_type import EnumType
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.fields.narrowing.narrowing_limit import NarrowingLimit
    from hare.models import Model

    #: The way from a container to the values it holds - each step, with a tuple element's position.
    HeldValuePath = tuple[tuple[HeldValueStep, int | None], ...]


class ContainerField(Field[Any]):
    """A field whose value holds values of other fields - an array, a map, a tuple, nested rows. The
    held fields are any fields, containers too, to any depth: each value inside is written and read
    by the field holding it, through the dialect's types; a dialect giving no column type for one
    of them gives none for the container. An error names the path to the value it is about
    (``tags[2]``, ``prices['eur']``, ``point.1``).
    """

    COLUMN_TYPE_FROM_DIALECT = True
    holds_container_value: ClassVar[bool] = True

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if self.sensitive:
            # A held value's own error message must hide it the same way.
            for child_field in self.get_child_fields():
                child_field.sensitive = True

    def get_child_fields(self) -> tuple[Field[Any], ...]:
        """The fields of the values the container holds.

        Returns:
            The fields.
        """
        raise NotImplementedError

    def get_required_extension(self, dialect: Dialect) -> str | None:
        # The column of a container needs what the type of a value it holds needs.
        return next(
            (
                extension
                for child in self.get_child_fields()
                if (extension := child.get_required_extension(dialect)) is not None
            ),
            None,
        )

    @property
    def requires_enum_type(self) -> EnumType | None:  # type: ignore[override]
        return next(
            (child.requires_enum_type for child in self.get_child_fields() if child.requires_enum_type is not None),
            None,
        )

    def encode_value(
        self, value: Any, instance: type[Model] | Model | None, types: TypeRegistry | None, path: str
    ) -> Any:
        """The value bound for a container that isn't None.

        Args:
            value: The Python value.
            instance: The model (class) it is written or compared for.
            types: The dialect's types each held value is written through; None for the fields' own.
            path: The path to the value, named in an error.

        Returns:
            The value to bind.

        Raises:
            ValidationError: The value isn't of the container's shape, or a held field refuses its value.
        """
        raise NotImplementedError

    def decode_value(self, value: Any, types: TypeRegistry | None) -> Any:
        """The Python value of a container read from the database that isn't None.

        Args:
            value: The value the driver returned.
            types: The dialect's types each held value is read through; None for the fields' own.

        Returns:
            The Python value.
        """
        raise NotImplementedError

    def normalize_value(self, value: Any, path: str) -> Any:
        """A value assigned to an instance, each held value as its field holds it.

        Args:
            value: The value, not None.
            path: The path to the value, named in an error.

        Returns:
            The value - one of another shape as it is, for ``encode_value()`` to refuse.
        """
        raise NotImplementedError

    def get_held_steps(self) -> tuple[tuple[tuple[HeldValueStep, int | None], Field[Any]], ...]:
        """The step to each value the container holds, with the value's field.

        Returns:
            The steps.
        """
        raise NotImplementedError

    def get_held_narrowing_limits(self, old_field: Field[Any]) -> list[tuple[HeldValuePath, NarrowingLimit]]:
        """What the values the container holds must fit, at any depth, when an ``AlterField`` turns
        ``old_field`` into this field - a held field narrowed as ``get_narrowing_limit()`` narrows a
        column.

        Args:
            old_field: The previous definition.

        Returns:
            The way to each narrowed held value and its limit; empty when the old field is a
            container of another shape.
        """
        if type(old_field) is not type(self):
            return []
        old_steps = old_field.get_held_steps()  # type: ignore[attr-defined]
        new_steps = self.get_held_steps()
        if [step for step, _field in old_steps] != [step for step, _field in new_steps]:
            return []
        limits: list[tuple[HeldValuePath, NarrowingLimit]] = []
        for (step, old_child), (_step, new_child) in zip(old_steps, new_steps, strict=True):
            if isinstance(new_child, ContainerField):
                limits.extend(
                    ((step, *held_path), limit) for held_path, limit in new_child.get_held_narrowing_limits(old_child)
                )
            elif (limit := new_child.get_narrowing_limit(old_child)) is not None:
                limits.append(((step,), limit))
        return limits

    def get_path(self) -> str:
        """The path an error of the container's value starts with.

        Returns:
            The field's name.
        """
        return self.model_field_name or type(self).__name__

    def get_db_value(
        self, value: Any, instance: type[Model] | Model | None, types: TypeRegistry | None, path: str | None = None
    ) -> Any:
        """The value bound for a column of the container.

        Args:
            value: The Python value.
            instance: The model (class) it is written or compared for.
            types: The dialect's types each held value is written through; None for the fields' own.

        Returns:
            The value to bind - None for None.

        Raises:
            ValidationError: The value is None and the field holds no NULL - a database storing a
                container without NULL would write an empty or a zero one in its place.
        """
        return self._write_container(value, instance, path, self.get_value_encoder(types))

    def get_python_value(self, value: Any, types: TypeRegistry | None) -> Any:
        """Read one container through the same prepared traversal used for a column."""
        return self._read_container(value, self.get_value_decoder(types))

    def get_value_encoder(self, types: TypeRegistry | None) -> Callable[..., Any]:
        """Prepare non-NULL writes; custom containers retain their encode_value hook."""
        return lambda value, instance, path: self.encode_value(value, instance, types, path)

    def get_value_decoder(self, types: TypeRegistry | None) -> Callable[[Any], Any]:
        """Prepare non-NULL reads; custom containers retain their decode_value hook."""
        return lambda value: self.decode_value(value, types)

    def get_value_writer(self, types: TypeRegistry | None) -> Callable[..., Any]:
        """Prepare a path-aware container writer without retaining values or instances."""
        if getattr(self.get_db_value, "__func__", None) is not ContainerField.get_db_value:
            return lambda value, instance, path=None: self.get_db_value(value, instance, types, path)
        encode = self.get_value_encoder(types)
        return lambda value, instance, path=None: self._write_container(value, instance, path, encode)

    def get_value_reader(self, types: TypeRegistry | None) -> Callable[[Any], Any]:
        """Prepare a container reader while retaining a custom get_python_value hook."""
        if getattr(self.get_python_value, "__func__", None) is not ContainerField.get_python_value:
            return lambda value: self.get_python_value(value, types)
        decode = self.get_value_decoder(types)
        return lambda value: self._read_container(value, decode)

    def _write_container(self, value: Any, instance: Any, path: str | None, encode: Callable[..., Any]) -> Any:
        """Validate the outer value before its sole container traversal."""
        path = self.get_path() if path is None else path
        if value is None and not self.null:
            raise ValidationError(f"{path}: a value is required, got None")
        self.validate(value)
        return None if value is None else encode(value, instance, path)

    @staticmethod
    def _read_container(value: Any, decode: Callable[[Any], Any]) -> Any:
        return None if value is None else decode(value)

    def get_dialect_db_value(self, value: Any, instance: type[Model] | Model | None, types: TypeRegistry) -> Any:
        """Write a root container through its dialect; nested values retain indexed paths."""
        return self.get_db_value(value, instance, types)

    def get_dialect_python_value(self, value: Any, types: TypeRegistry) -> Any:
        """Read a root container through its dialect."""
        return self.get_python_value(value, types)

    def get_dialect_db_writer(self, types: TypeRegistry) -> Callable[[Any, Any], Any]:
        """Prepare the root writer without bypassing a dialect-entry override."""
        if getattr(self.get_dialect_db_value, "__func__", None) is not ContainerField.get_dialect_db_value:
            return lambda value, instance: self.get_dialect_db_value(value, instance, types)
        return self.get_value_writer(types)

    def get_dialect_python_reader(self, types: TypeRegistry) -> Callable[[Any], Any]:
        """Prepare the root reader without bypassing a dialect-entry override."""
        if getattr(self.get_dialect_python_value, "__func__", None) is not ContainerField.get_dialect_python_value:
            return lambda value: self.get_dialect_python_value(value, types)
        return self.get_value_reader(types)

    def encode_child(
        self,
        child_field: Field[Any],
        value: Any,
        instance: type[Model] | Model | None,
        types: TypeRegistry | None,
        path: str,
    ) -> Any:
        """Write one held value with the same NULL and error policy as a prepared child."""
        return self._prepare_child_encoder(child_field, types)(value, instance, path)

    def get_child_encoder(self, child: Field[Any], types: TypeRegistry | None) -> Callable[..., Any]:
        """Prepare a child once, retaining custom child hooks at every nesting depth."""
        if getattr(self.encode_child, "__func__", None) is not ContainerField.encode_child:
            return lambda value, instance, path: self.encode_child(child, value, instance, types, path)
        return self._prepare_child_encoder(child, types)

    def _prepare_child_encoder(self, child: Field[Any], types: TypeRegistry | None) -> Callable[..., Any]:
        from hare.dialects.base.types.type_registry import TypeRegistry

        nested = isinstance(child, ContainerField)
        write: Callable[..., Any] = (
            child.get_value_writer(types)  # type: ignore[attr-defined]
            if nested
            else child.to_db_value
            if types is None
            else TypeRegistry.get_db_writer(types, child)
        )
        nullable = child.null or (types is not None and types.container_values_always_nullable)

        def write_child(value: Any, instance: Any, path: str) -> Any:
            if value is None:
                if not nullable:
                    raise ValidationError(f"{path}: a value is required, got None")
                return None
            if nested:
                return write(value, instance, path)
            validation_error = None
            try:
                return write(value, instance)
            except ValidationError as error:
                message = str(error).removeprefix(f"{child.model_field_name}: ").removeprefix(": ")
                validation_error = self.get_validation_error(error, value, f"{path}: {message}")
            raise validation_error

        return write_child

    def decode_child(self, child_field: Field[Any], value: Any, types: TypeRegistry | None) -> Any:
        """Read one held value through the prepared child conversion."""
        return self._prepare_child_decoder(child_field, types)(value)

    def get_child_decoder(self, child: Field[Any], types: TypeRegistry | None) -> Callable[[Any], Any]:
        """Prepare reads with the same custom-hook policy as writes."""
        if getattr(self.decode_child, "__func__", None) is not ContainerField.decode_child:
            return lambda value: self.decode_child(child, value, types)
        return self._prepare_child_decoder(child, types)

    def _prepare_child_decoder(self, child: Field[Any], types: TypeRegistry | None) -> Callable[[Any], Any]:
        from hare.dialects.base.types.type_registry import TypeRegistry

        read = (
            child.get_value_reader(types)
            if isinstance(child, ContainerField)
            else child.from_db_value
            if types is None
            else TypeRegistry.get_python_reader(types, child)
        )
        return lambda value: None if value is None else read(value)

    def normalize_child(self, child_field: Field[Any], value: Any, path: str) -> Any:
        """A held value as its field holds it.

        Args:
            child_field: The field of the held value.
            value: The value.
            path: The path to the held value.

        Returns:
            The value.

        Raises:
            ValidationError: The field refuses the value.
        """
        if value is None:
            return None
        if isinstance(child_field, ContainerField):
            return child_field.normalize_value(value, path)
        validation_error = None
        try:
            return child_field.to_python(value)
        except ValidationError as error:
            message = str(error).removeprefix(f"{child_field.model_field_name}: ").removeprefix(": ")
            validation_error = self.get_validation_error(error, value, f"{path}: {message}")
        raise validation_error

    def to_db_value(self, value: Any, instance: type[Model] | Model | None) -> Any:
        return self.get_db_value(value, instance, None)

    def from_db_value(self, value: Any) -> Any:
        return self.get_python_value(value, None)

    def to_python(self, value: Any) -> Any:
        if value is None:
            return None
        return self.normalize_value(value, self.get_path())

    def get_assign_normalized_types(self) -> frozenset[type]:
        # Every held value may still need its field's own normalization.
        return frozenset()
