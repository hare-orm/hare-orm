from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any

from hare.classes.value_descriptions import ValueDescriptions
from hare.exceptions import ConfigurationError, ValidationError
from hare.fields.data.constants import ARRAY_VALUE_TYPE
from hare.fields.data.containers.constants import (
    ARRAY_ELEMENT_CODEC_MIN_LENGTH,
    ARRAY_LENGTH_PATH_SEGMENT,
    ARRAY_LIST_LOOKUPS,
    ARRAY_SLICE_PATH_PATTERN,
    CONTAINER_INDEX_PATH_PATTERN,
)
from hare.fields.data.containers.container_field import ContainerField
from hare.fields.enums import HeldValueStep
from hare.fields.field import Field
from hare.query.enums import Lookup, LookupValueShape

if TYPE_CHECKING:
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.models import Model
    from hare.query.filters.lookups.field_lookup import FieldLookup
    from hare.sql.terms.term import Term


class ArrayField(ContainerField):
    """An array of values of ``base_field`` - any field, an array, a map or a tuple too::

        tags = ArrayField(fields.CharField(max_length=20))
        scores = ArrayField(ArrayField(fields.IntField()))

    ``tags__0`` reads an element (0-indexed, negative from the end), ``tags__0_2`` a slice,
    ``tags__len`` the length; ``__contains``, ``__contained_by``, ``__overlap``, ``__len`` and
    ``__item=(index, value)`` compare it. A set is written sorted.

    Args:
        base_field: The field of the elements.

    Raises:
        ConfigurationError: ``base_field`` isn't a field instance.
    """

    argument_value_type = ARRAY_VALUE_TYPE

    field_type = list

    def __init__(self, base_field: Field[Any], **kwargs: Any) -> None:
        if not isinstance(base_field, Field):
            raise ConfigurationError(
                f"ArrayField(base_field=...) takes a field instance, got "
                f"{ValueDescriptions.get_shown_value(base_field)}"
            )
        self.base_field = base_field
        super().__init__(**kwargs)

    def get_json_value(self, value: Any) -> Any:
        """The JSON form of a value of the field - each element in the JSON form of the element field.

        Args:
            value: A value of the field, None included.

        Returns:
            The value's JSON form.

        Raises:
            TypeError: A value inside it has no JSON form.
        """
        if value is None:
            return None
        return [self.base_field.get_json_value(element) for element in value]

    def get_child_fields(self) -> tuple[Field[Any], ...]:
        return (self.base_field,)

    def get_held_steps(self) -> tuple[tuple[tuple[HeldValueStep, int | None], Field[Any]], ...]:
        return (((HeldValueStep.ELEMENT, None), self.base_field),)

    def encode_value(
        self, value: Any, instance: type[Model] | Model | None, types: TypeRegistry | None, path: str
    ) -> Any:
        """Write elements in order through the shared child validator and error-path handling.

        Long scalar arrays reuse a standard registry's bound writer. Custom registry or child
        dispatch and nested fields keep per-element selection; every field conversion still runs.
        """
        if not isinstance(value, (list, tuple, set)):
            # A value that can't be iterated raises ValidationError, not a TypeError.
            raise ValidationError(f"{path}: expected a list/tuple/set, got {self.get_value_for_message(value)}")
        if isinstance(value, set):
            # A set has no stable order - stored sorted.
            value = sorted(value)
        if (
            types is not None
            and (type(value) is list or type(value) is tuple)
            and len(value) >= ARRAY_ELEMENT_CODEC_MIN_LENGTH
            and not isinstance(self.base_field, ContainerField)
            and getattr(self.encode_child, "__func__", None) is ContainerField.encode_child
        ):
            from hare.dialects.base.types.type_registry import TypeRegistry

            if (
                getattr(types.get_db_value, "__func__", None) is TypeRegistry.get_db_value
                and getattr(types.get_db_converter, "__func__", None) is TypeRegistry.get_db_converter
            ):
                writer = TypeRegistry.get_db_writer(types, self.base_field)
                return [
                    self.encode_child(self.base_field, element, instance, types, f"{path}[{index}]", writer=writer)
                    for index, element in enumerate(value)
                ]
        return [
            self.encode_child(self.base_field, element, instance, types, f"{path}[{index}]")
            for index, element in enumerate(value)
        ]

    def decode_value(self, value: Any, types: TypeRegistry | None) -> Any:
        """Read elements in order, binding a scalar field's standard codec once for long arrays.

        NULL elements bypass the field reader. Small arrays, nested containers, unsized iterables
        and custom registry or child-dispatch methods retain their per-element path. A bound reader
        still calls every non-NULL element's field conversion and propagates its first exception.
        """
        if isinstance(value, str):
            # A str here is an undecoded value or a scalar passed for an array - iterating it would
            # give a list of characters.
            raise ValidationError(
                f"{self.get_path()}: expected an array value from the database, got a string "
                f"({self.get_value_for_message(value)})"
            )
        if (
            types is not None
            and (type(value) is list or type(value) is tuple)
            and len(value) >= ARRAY_ELEMENT_CODEC_MIN_LENGTH
            and not isinstance(self.base_field, ContainerField)
            and getattr(self.decode_child, "__func__", None) is ContainerField.decode_child
        ):
            from hare.dialects.base.types.type_registry import TypeRegistry

            if (
                getattr(types.get_python_value, "__func__", None) is TypeRegistry.get_python_value
                and getattr(types.get_python_converter, "__func__", None) is TypeRegistry.get_python_converter
            ):
                reader = TypeRegistry.get_python_reader(types, self.base_field)
                return [None if element is None else reader(element) for element in value]
        return [self.decode_child(self.base_field, element, types) for element in value]

    def normalize_value(self, value: Any, path: str) -> Any:
        if not isinstance(value, (list, tuple, set)):
            return value
        return [
            self.normalize_child(self.base_field, element, f"{path}[{index}]") for index, element in enumerate(value)
        ]

    def get_lookups(self) -> dict[str, FieldLookup]:
        # Local import: the lookups import the fields package.
        from hare.query.filters.lookups.containers.container_field_lookups import ContainerFieldLookups

        return ContainerFieldLookups.get_array_lookups(self)

    def get_path_transform(self, segment: str) -> tuple[Callable[[Term], Term], Field[Any]] | None:
        """An element (``tags__0``), a slice (``tags__0_2``) or the length (``tags__len``) of the array."""
        # Local import: hare.sql's terms import the fields package.
        from hare.query.expressions.numeric.numeric_typing import NumericTyping
        from hare.sql.terms.containers import ArrayElementTerm, ArrayLengthTerm, ArraySliceTerm

        if CONTAINER_INDEX_PATH_PATTERN.fullmatch(segment):
            return partial(ArrayElementTerm, index=int(segment), element_field=self.base_field), self.base_field
        if slice_match := ARRAY_SLICE_PATH_PATTERN.fullmatch(segment):
            return partial(ArraySliceTerm, start=int(slice_match.group(1)), end=int(slice_match.group(2))), self
        if segment == ARRAY_LENGTH_PATH_SEGMENT:
            length_field: Field[Any] = NumericTyping.INTEGER_OUTPUT_FIELD
            return ArrayLengthTerm, length_field
        return None

    def get_lookup_value_description(self, lookup: str) -> tuple[LookupValueShape, Any] | None:
        """Equality and the containment lookups take a list of elements, ``item`` an
        ``(index, value)`` pair."""
        if lookup in ARRAY_LIST_LOOKUPS:
            # An element of a field holding NULL may be None in the list too.
            element_type = self.base_field.field_type
            return LookupValueShape.LIST, element_type | None if self.base_field.null else element_type
        if lookup == Lookup.ITEM:
            return LookupValueShape.VALUE, tuple
        return None

    def get_python_type(self) -> Any:
        # The element type, not the bare `list` of field_type - what a pydantic model generated from
        # the field accepts.
        return list[self.base_field.get_annotation()]  # type: ignore[misc]
