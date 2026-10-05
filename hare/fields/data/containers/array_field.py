from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any

from hare.exceptions import ConfigurationError, ValidationError
from hare.fields.data.containers.constants import (
    ARRAY_LENGTH_PATH_SEGMENT,
    ARRAY_LIST_LOOKUPS,
    ARRAY_SLICE_PATH_PATTERN,
    CONTAINER_INDEX_PATH_PATTERN,
)
from hare.fields.data.containers.container_field import ContainerField
from hare.fields.enums import HeldValueStep
from hare.fields.field import Field
from hare.query.enums import Lookup, LookupValueShape

if TYPE_CHECKING:  # pragma: nocoverage
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

    field_type = list

    def __init__(self, base_field: Field[Any], **kwargs: Any) -> None:
        if not isinstance(base_field, Field):
            raise ConfigurationError(f"ArrayField(base_field=...) takes a field instance, got {base_field!r}")
        self.base_field = base_field
        super().__init__(**kwargs)

    def get_child_fields(self) -> tuple[Field[Any], ...]:
        return (self.base_field,)

    def get_held_steps(self) -> tuple[tuple[tuple[HeldValueStep, int | None], Field[Any]], ...]:
        return (((HeldValueStep.ELEMENT, None), self.base_field),)

    def encode_value(
        self, value: Any, instance: type[Model] | Model | None, types: TypeRegistry | None, path: str
    ) -> Any:
        if not isinstance(value, (list, tuple, set)):
            # A value that can't be iterated raises ValidationError, not a TypeError.
            raise ValidationError(f"{path}: expected a list/tuple/set, got {self.get_value_for_message(value)}")
        if isinstance(value, set):
            # A set has no stable order - stored sorted.
            value = sorted(value)
        return [
            self.encode_child(self.base_field, element, instance, types, f"{path}[{index}]")
            for index, element in enumerate(value)
        ]

    def decode_value(self, value: Any, types: TypeRegistry | None) -> Any:
        if isinstance(value, str):
            # A str here is an undecoded value or a scalar passed for an array - iterating it would
            # give a list of characters.
            raise ValidationError(
                f"{self.get_path()}: expected an array value from the database, got a string "
                f"({self.get_value_for_message(value)})"
            )
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
            length_field: Field[Any] = NumericTyping.INTEGER_OUTPUT_FIELD  # type: ignore[arg-type]
            return ArrayLengthTerm, length_field
        return None

    def get_lookup_value_description(self, lookup: str) -> tuple[LookupValueShape, Any] | None:
        """Equality and the containment lookups take a list of elements, ``item`` an
        ``(index, value)`` pair."""
        if lookup in ARRAY_LIST_LOOKUPS:
            return LookupValueShape.LIST, self.base_field.field_type
        if lookup == Lookup.ITEM:
            return LookupValueShape.VALUE, tuple
        return None

    def get_python_type(self) -> Any:
        # The element type, not the bare `list` of field_type - what a pydantic model generated from
        # the field accepts.
        return list[self.get_child_python_type(self.base_field)]  # type: ignore[misc]

    def get_read_codec_specification(
        self, types: TypeRegistry, zone_name: str | None
    ) -> tuple[str, dict[str, Any]] | None:
        # Each element is read as the element field reads a value of an expression - the codec
        # repeats decode_value() below, unless a subclass reads otherwise. An element held in a
        # container of another shape is read by decode_value() itself.
        field_class = type(self)
        if (
            not self.reads_values_by_types()
            or field_class.decode_value is not ArrayField.decode_value
            or field_class.normalize_value is not ArrayField.normalize_value
            or (isinstance(self.base_field, ContainerField) and not isinstance(self.base_field, ArrayField))
        ):
            return None
        # Imported here: the rows package imports the fields package.
        from hare.query.rows.enums import ReadCodecType
        from hare.query.rows.native.field_codecs import FieldCodecs

        element_type, element_options = FieldCodecs.get_expression_read_specification(
            self.base_field.model_field_name or self.model_field_name, self.base_field, types, zone_name
        )
        return ReadCodecType.ARRAY, {
            "element_type": element_type,
            "element_options": element_options,
            "fallback": partial(self.get_python_value, types=types),
        }
