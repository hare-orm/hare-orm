from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import partial
from typing import TYPE_CHECKING, Any

from hare.exceptions import ConfigurationError, ValidationError
from hare.fields.data.containers.constants import (
    ARRAY_LENGTH_PATH_SEGMENT,
    MAP_KEY_TYPES,
    MAP_KEYS_PATH_SEGMENT,
    MAP_VALUES_PATH_SEGMENT,
)
from hare.fields.data.containers.container_field import ContainerField
from hare.fields.data.containers.declarations import MapValue
from hare.fields.enums import HeldValueStep
from hare.fields.field import Field
from hare.query.enums import Lookup, LookupValueShape

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.models import Model
    from hare.query.filters.lookups.field_lookup import FieldLookup
    from hare.sql.terms.term import Term


class MapField(ContainerField):
    """A map of keys of ``key_field`` to values of ``value_field`` - any field, a container too::

        prices = MapField(fields.CharField(max_length=3), fields.DecimalField(max_digits=10, decimal_places=2))
        tags_by_lang = MapField(fields.CharField(max_length=5), ArrayField(fields.TextField()))

    ``prices__eur`` reads the value under a key - the value type's default for a missing key -,
    ``prices__keys``/``prices__values`` the keys and the values as arrays, ``prices__len`` the number
    of keys; ``__has_key``, ``__has_keys``, ``__has_any_keys`` and ``__len`` check it.

    Args:
        key_field: The field of the keys - an integer, a text, a UUID, a date, a moment or an enum,
            holding no NULL.
        value_field: The field of the values.

    Raises:
        ConfigurationError: A field isn't a field instance, or the key field is of another type or
            holds NULL.
    """

    field_type = dict

    def __init__(self, key_field: Field[Any], value_field: Field[Any], **kwargs: Any) -> None:
        if not isinstance(key_field, Field) or not isinstance(value_field, Field):
            raise ConfigurationError(f"MapField takes field instances, got {key_field!r} and {value_field!r}")
        key_type = key_field.field_type
        if (
            isinstance(key_field, ContainerField)
            or key_field.null
            or key_type is bool
            or not (isinstance(key_type, type) and issubclass(key_type, MAP_KEY_TYPES))
        ):
            raise ConfigurationError(
                "MapField(key_field=...) is a field of integers, texts, UUIDs, dates, moments or an enum, "
                f"holding no NULL - got {type(key_field).__name__}"
            )
        self.key_field = key_field
        self.value_field = value_field
        super().__init__(**kwargs)

    def get_child_fields(self) -> tuple[Field[Any], ...]:
        return self.key_field, self.value_field

    def get_held_steps(self) -> tuple[tuple[tuple[HeldValueStep, int | None], Field[Any]], ...]:
        return ((HeldValueStep.KEY, None), self.key_field), ((HeldValueStep.VALUE, None), self.value_field)

    def encode_value(
        self, value: Any, instance: type[Model] | Model | None, types: TypeRegistry | None, path: str
    ) -> Any:
        if not isinstance(value, Mapping):
            raise ValidationError(f"{path}: expected a dict, got {self.get_value_for_message(value)}")
        encoded = MapValue()
        for key, item in value.items():
            key_path = f"{path}[{key!r}]"
            if key is None:
                raise ValidationError(f"{key_path}: a map key is never None")
            encoded[self.encode_child(self.key_field, key, instance, types, key_path)] = self.encode_child(
                self.value_field, item, instance, types, key_path
            )
        return encoded

    def decode_value(self, value: Any, types: TypeRegistry | None) -> Any:
        items = value.items() if isinstance(value, Mapping) else value
        return {
            self.decode_child(self.key_field, key, types): self.decode_child(self.value_field, item, types)
            for key, item in items
        }

    def normalize_value(self, value: Any, path: str) -> Any:
        if not isinstance(value, Mapping):
            return value
        return {
            self.normalize_child(self.key_field, key, f"{path}[{key!r}]"): self.normalize_child(
                self.value_field, item, f"{path}[{key!r}]"
            )
            for key, item in value.items()
        }

    def get_lookups(self) -> dict[str, FieldLookup]:
        # Local import: the lookups import the fields package.
        from hare.query.filters.lookups.containers.container_field_lookups import ContainerFieldLookups

        return ContainerFieldLookups.get_map_lookups()

    def get_path_transform(self, segment: str) -> tuple[Callable[[Term], Term], Field[Any]] | None:
        """The value under a key (``prices__eur``), the keys (``prices__keys``), the values
        (``prices__values``) or the number of keys (``prices__len``)."""
        # Local imports: hare.sql's terms and the containers package import the fields package.
        from hare.fields.data.containers.array_field import ArrayField
        from hare.query.expressions.numeric.numeric_typing import NumericTyping
        from hare.sql.terms.containers import ArrayLengthTerm, MapKeysTerm, MapValuesTerm, MapValueTerm

        if segment == MAP_KEYS_PATH_SEGMENT:
            return MapKeysTerm, ArrayField(self.key_field)
        if segment == MAP_VALUES_PATH_SEGMENT:
            return MapValuesTerm, ArrayField(self.value_field)
        if segment == ARRAY_LENGTH_PATH_SEGMENT:
            length_field: Field[Any] = NumericTyping.INTEGER_OUTPUT_FIELD  # type: ignore[arg-type]
            return ArrayLengthTerm, length_field
        key = self.get_path_key(segment)
        if key is None:
            return None
        return partial(MapValueTerm, key=key, value_field=self.value_field), self.value_field

    def get_path_key(self, segment: str) -> Any:
        """The key a path segment names, as the key field binds it.

        Args:
            segment: The segment.

        Returns:
            The key, None when the segment isn't one of the key field's values.
        """
        try:
            return self.key_field.to_db_value(self.key_field.to_python(segment), None)  # type: ignore[arg-type]
        except ValidationError:
            return None

    def get_lookup_value_description(self, lookup: str) -> tuple[LookupValueShape, Any] | None:
        """``has_key`` takes a key, ``has_keys``/``has_any_keys`` a list of keys, equality a dict."""
        if lookup == Lookup.HAS_KEY:
            return LookupValueShape.VALUE, self.key_field.field_type
        if lookup in {Lookup.HAS_KEYS, Lookup.HAS_ANY_KEYS}:
            return LookupValueShape.LIST, self.key_field.field_type
        if lookup in {Lookup.EXACT, Lookup.NOT}:
            return LookupValueShape.VALUE, dict
        return None

    def get_python_type(self) -> Any:
        return dict[self.get_child_python_type(self.key_field), self.get_child_python_type(self.value_field)]  # type: ignore[misc]
