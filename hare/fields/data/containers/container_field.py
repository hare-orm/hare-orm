from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.exceptions import ValidationError
from hare.fields.enums import HeldValueStep
from hare.fields.field import Field

if TYPE_CHECKING:  # pragma: nocoverage
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
        raise NotImplementedError  # pragma: nocoverage

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
        raise NotImplementedError  # pragma: nocoverage

    def decode_value(self, value: Any, types: TypeRegistry | None) -> Any:
        """The Python value of a container read from the database that isn't None.

        Args:
            value: The value the driver returned.
            types: The dialect's types each held value is read through; None for the fields' own.

        Returns:
            The Python value.
        """
        raise NotImplementedError  # pragma: nocoverage

    def normalize_value(self, value: Any, path: str) -> Any:
        """A value assigned to an instance, each held value as its field holds it.

        Args:
            value: The value, not None.
            path: The path to the value, named in an error.

        Returns:
            The value - one of another shape as it is, for ``encode_value()`` to refuse.
        """
        raise NotImplementedError  # pragma: nocoverage

    def get_held_steps(self) -> tuple[tuple[tuple[HeldValueStep, int | None], Field[Any]], ...]:
        """The step to each value the container holds, with the value's field.

        Returns:
            The steps.
        """
        raise NotImplementedError  # pragma: nocoverage

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
        old_steps = cast("ContainerField", old_field).get_held_steps()
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

    def get_db_value(self, value: Any, instance: type[Model] | Model | None, types: TypeRegistry | None) -> Any:
        """The value bound for a column of the container.

        Args:
            value: The Python value.
            instance: The model (class) it is written or compared for.
            types: The dialect's types each held value is written through; None for the fields' own.

        Returns:
            The value to bind - None for None.
        """
        self.validate(value)
        if value is None:
            return None
        return self.encode_value(value, instance, types, self.get_path())

    def get_python_value(self, value: Any, types: TypeRegistry | None) -> Any:
        """The Python value of a column of the container.

        Args:
            value: The value the driver returned.
            types: The dialect's types each held value is read through; None for the fields' own.

        Returns:
            The Python value - None for None.
        """
        if value is None:
            return None
        return self.decode_value(value, types)

    def get_dialect_db_value(self, value: Any, instance: type[Model] | Model | None, types: TypeRegistry) -> Any:
        """The value a dialect binds for a column of the container - through its types, unless a
        subclass writes its values its own way (``to_db_value()``).

        Args:
            value: The Python value.
            instance: The model (class) it is written or compared for.
            types: The dialect's types.

        Returns:
            The value to bind.
        """
        if type(self).to_db_value is not ContainerField.to_db_value:
            return self.to_db_value(value, instance)  # type: ignore[arg-type]
        return self.get_db_value(value, instance, types)

    def get_dialect_python_value(self, value: Any, types: TypeRegistry) -> Any:
        """The Python value of a column of the container a dialect reads - through its types, unless a
        subclass reads its values its own way (``from_db_value()``).

        Args:
            value: The value the driver returned.
            types: The dialect's types.

        Returns:
            The Python value.
        """
        if type(self).from_db_value is not ContainerField.from_db_value:
            return self.from_db_value(value)
        return self.get_python_value(value, types)

    def reads_values_by_types(self) -> bool:
        """Whether the container's values are read as ``get_python_value()`` reads them - no subclass
        reading them its own way.

        Returns:
            True when they are.
        """
        field_class = type(self)
        return all(
            getattr(field_class, name) is getattr(ContainerField, name)
            for name in ("from_db_value", "to_python", "get_python_value")
        )

    def encode_child(
        self,
        child_field: Field[Any],
        value: Any,
        instance: type[Model] | Model | None,
        types: TypeRegistry | None,
        path: str,
    ) -> Any:
        """The value bound for one held value.

        Args:
            child_field: The field of the held value.
            value: The held value.
            instance: The model (class) it is written or compared for.
            types: The dialect's types; None for the fields' own.
            path: The path to the held value.

        Returns:
            The value to bind.

        Raises:
            ValidationError: The field refuses the value - a None where the field holds no NULL
                and the dialect's containers don't hold one for every field.
        """
        if value is None:
            if not child_field.null and (types is None or not types.container_values_always_nullable):
                raise ValidationError(f"{path}: a value is required, got None")
            if (
                types is not None
                and types.null_container_value is not None
                and isinstance(child_field, ContainerField)
            ):
                return types.null_container_value(child_field)
            return None
        if isinstance(child_field, ContainerField):
            return child_field.encode_value(value, instance, types, path)
        validation_error = None
        try:
            if types is None:
                return child_field.to_db_value(value, instance)  # type: ignore[arg-type]
            return types.get_db_value(child_field, value, instance)
        except ValidationError as error:
            # The held field is bound to no model: its own message names no field - the path does.
            message = str(error).removeprefix(f"{child_field.model_field_name}: ").removeprefix(": ")
            validation_error = self.get_validation_error(error, value, f"{path}: {message}")
        raise validation_error

    def decode_child(self, child_field: Field[Any], value: Any, types: TypeRegistry | None) -> Any:
        """The Python value of one held value read from the database.

        Args:
            child_field: The field of the held value.
            value: The value the driver returned.
            types: The dialect's types; None for the fields' own.

        Returns:
            The Python value.
        """
        if value is None:
            return None
        if isinstance(child_field, ContainerField):
            return child_field.decode_value(value, types)
        if types is None:
            return child_field.from_db_value(value)
        return types.get_python_value(child_field, value)

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

    @staticmethod
    def get_child_python_type(child_field: Field[Any]) -> Any:
        """The Python type of a held value - ``| None`` when its field holds NULL.

        Args:
            child_field: The field of the held value.

        Returns:
            The type.
        """
        return child_field.get_annotation()

    def to_db_value(self, value: Any, instance: type[Model] | Model) -> Any:
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
