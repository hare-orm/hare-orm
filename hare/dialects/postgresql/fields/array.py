from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any

from hare.dialects.enums import DialectName
from hare.dialects.postgresql.constants import (
    ARRAY_ITEM_PATH_PATTERN,
    ARRAY_LENGTH_PATH_SEGMENT,
    ARRAY_LIST_LOOKUPS,
    ARRAY_SLICE_PATH_PATTERN,
    POSTGRESQL_DIALECT,
)
from hare.dialects.registry import DialectRegistry
from hare.exceptions import ValidationError
from hare.fields import Field
from hare.query.enums import Lookup, LookupValueShape

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.models import Model
    from hare.query.filters.field_lookup import FieldLookup
    from hare.sql.terms.base.term import Term


class ArrayField(Field[Any], list[Any]):  # type: ignore[misc]
    """A Postgres array, with the element type given as a field instance (Django-style
    ``ArrayField(base_field=...)``) rather than a SQL type name string.

    Example:
        tags = ArrayField(base_field=fields.TextField())
        scores = ArrayField(base_field=fields.CharField(max_length=32))
    """

    SUPPORTED_DIALECTS = frozenset({DialectName.POSTGRESQL})
    holds_container_value = True

    def __init__(self, base_field: Field[Any], **kwargs: Any):
        super().__init__(**kwargs)
        self.base_field = base_field
        if self.sensitive:
            # An element's own error message must hide the element the same way.
            base_field.sensitive = True

    @property
    def SQL_TYPE(self) -> str:  # type: ignore[override]
        element_sql_type = self.base_field.get_column_type(DialectRegistry.get_dialect(DialectName.POSTGRESQL))
        return f"{element_sql_type}[]"

    def get_lookups(self) -> dict[str, FieldLookup]:
        # Local import: the array lookups import this module.
        from hare.dialects.postgresql.lookups.array.postgresql_array_field_lookups import PostgresqlArrayFieldLookups

        return PostgresqlArrayFieldLookups.get_lookups(self)

    def get_path_transform(self, segment: str) -> tuple[Callable[[Term], Term], Field[Any]] | None:
        """An item (``tags__0``, 0-indexed, negative from the end - an item of a nested array is
        its row), a slice (``tags__0_2``) or the length (``tags__len``) of the array."""
        # Local import: the array functions import this module.
        from hare.dialects.postgresql.functions.array.array_slice import ArraySlice
        from hare.dialects.postgresql.functions.array.array_subscript import ArraySubscript
        from hare.dialects.postgresql.lookups.array.array_length import ArrayLength
        from hare.query.expressions.numeric.numeric_typing import NumericTyping

        if ARRAY_ITEM_PATH_PATTERN.fullmatch(segment):
            subarray_field = self.base_field if isinstance(self.base_field, ArrayField) else None
            return partial(ArraySubscript, index=int(segment), subarray_field=subarray_field), self.base_field
        if slice_match := ARRAY_SLICE_PATH_PATTERN.fullmatch(segment):
            start, end = int(slice_match.group(1)), int(slice_match.group(2))
            return partial(ArraySlice, start=start, end=end), self
        if segment == ARRAY_LENGTH_PATH_SEGMENT:
            length_field: Field[Any] = NumericTyping.INTEGER_OUTPUT_FIELD  # type: ignore[arg-type]
            return ArrayLength.get_term, length_field
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
        return list[self.base_field.get_python_type()]  # type: ignore[misc]

    def to_db_value(self, value: Any, instance: type[Model] | Model) -> Any:
        # The inherited Field.to_db_value only coerces the outer container to list - it never
        # runs base_field's own to_db_value on each element, so e.g. an array of CharEnumField
        # values reached the driver as raw enum members instead of their .value strings.
        self.validate(value)
        if value is None:
            return None
        if not isinstance(value, (list, tuple, set)):
            # A value that can't be iterated raises ValidationError, not a TypeError.
            raise ValidationError(
                f"{self.model_field_name}: expected a list/tuple/set, got {self.get_value_for_message(value)}"
            )
        if isinstance(value, set):
            # A set has no stable order - stored sorted.
            value = sorted(value)
        return [self.encode_element(index, element, instance) for index, element in enumerate(value)]

    def encode_element(self, index: int, element: Any, instance: type[Model] | Model) -> Any:
        """Converts one array element through ``base_field``.

        Args:
            index: The element's position, named in an error message.
            element: The element; ``None`` stays a NULL element.
            instance: The model class or instance.

        Returns:
            The element's DB value.

        Raises:
            ValidationError: ``base_field`` rejects the element.
        """
        if element is None:
            # A Postgres array element can always be NULL, whatever its base type - base_field's
            # own non-null validators describe a column, not an array element.
            return None
        try:
            return POSTGRESQL_DIALECT.types.get_db_value(self.base_field, element, instance)
        except ValidationError as exc:
            # base_field is never registered on a model, so its own message carries no field
            # name - name the array field and the element position instead.
            message = str(exc).removeprefix(f"{self.base_field.model_field_name or ''}: ")
            validation_error = self.get_validation_error(exc, element, f"{self.model_field_name}[{index}]: {message}")
        raise validation_error

    def get_read_codec_spec(self, types: TypeRegistry, zone_name: str | None) -> tuple[str, dict[str, Any]] | None:
        # Each element is read as the element field reads a value of an expression - the codec
        # repeats from_db_value() below, unless a subclass reads otherwise.
        field_class = type(self)
        if (
            field_class.from_db_value is not ArrayField.from_db_value
            or field_class.to_python is not ArrayField.to_python
        ):
            return None
        # Imported here: the rows package imports the fields package.
        from hare.query.rows.enums import ReadCodecKind
        from hare.query.rows.field_codecs import FieldCodecs

        element_kind, element_options = FieldCodecs.get_expression_read_spec(
            self.base_field.model_field_name or self.model_field_name,
            self.base_field,
            POSTGRESQL_DIALECT.types,
            zone_name,
        )
        return ReadCodecKind.ARRAY, {
            "element_kind": element_kind,
            "element_options": element_options,
            "fallback": self.from_db_value,
        }

    def from_db_value(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            # A str here is an undecoded value or a scalar passed for an array - iterating it would
            # give a list of characters.
            raise ValidationError(
                f"{self.model_field_name}: expected an array value from the database, got a string "
                f"({self.get_value_for_message(value)})"
            )
        return [
            None if element is None else POSTGRESQL_DIALECT.types.get_python_value(self.base_field, element)
            for element in value
        ]

    def get_assign_normalized_types(self) -> frozenset[type]:
        # Every element may still need base_field's own normalization.
        return frozenset()

    def to_python(self, value: Any) -> Any:
        # Each element goes through base_field.to_python(), like an assigned value of that field. A
        # value that isn't a list, tuple or set is left for to_db_value() to reject.
        if not isinstance(value, (list, tuple, set)):
            return value
        return [self.base_field.to_python(element) for element in value]
