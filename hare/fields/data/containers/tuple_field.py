from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from functools import partial
from typing import TYPE_CHECKING, Any, TypedDict

from hare.exceptions import ConfigurationError, ValidationError
from hare.fields.data.containers.constants import CONTAINER_INDEX_PATH_PATTERN
from hare.fields.data.containers.container_field import ContainerField
from hare.fields.data.containers.declarations import TupleValue
from hare.fields.enums import HeldValueStep
from hare.fields.field import Field
from hare.query.enums import Lookup, LookupValueShape

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.models import Model
    from hare.query.filters.lookups.field_lookup import FieldLookup
    from hare.sql.terms.term import Term


class TupleField(ContainerField):
    """A fixed number of values, each of its own field - any field, a container too::

        bounds = TupleField([fields.IntField(), fields.IntField()])            # (1, 9)
        point = TupleField({"lon": fields.FloatField(), "lat": fields.FloatField()})  # {"lon": 2.3, "lat": 48.8}

    A tuple of unnamed elements is a Python tuple; of named ones, a dict of them (a tuple or list in
    their order is taken too). ``point__0`` - or ``point__lat`` - reads an element.

    Args:
        element_fields: The fields of the elements in order, or by name.

    Raises:
        ConfigurationError: ``element_fields`` is empty, or holds a value that isn't a field
            instance or a name that isn't an identifier.
    """

    field_type = tuple

    def __init__(self, element_fields: Sequence[Field[Any]] | Mapping[str, Field[Any]], **kwargs: Any) -> None:
        # Checked as given - a caller may pass anything.
        given_fields: object = element_fields
        if isinstance(given_fields, Mapping):
            names: tuple[str, ...] | None = tuple(given_fields)
            fields = tuple(given_fields.values())
        elif isinstance(given_fields, Sequence) and not isinstance(given_fields, str):
            names = None
            fields = tuple(given_fields)
        else:
            raise ConfigurationError(f"TupleField takes a list or a dict of fields, got {element_fields!r}")
        if not fields or not all(isinstance(field, Field) for field in fields):
            raise ConfigurationError(f"TupleField takes at least one field instance, got {element_fields!r}")
        if names is not None and not all(isinstance(name, str) and name.isidentifier() for name in names):
            raise ConfigurationError(f"TupleField's element names are identifiers, got {list(names)!r}")
        self.element_fields = element_fields
        self.element_names = names
        self.fields_in_order = fields
        super().__init__(**kwargs)

    def get_child_fields(self) -> tuple[Field[Any], ...]:
        return self.fields_in_order

    def get_held_steps(self) -> tuple[tuple[tuple[HeldValueStep, int | None], Field[Any]], ...]:
        return tuple(
            ((HeldValueStep.TUPLE_ELEMENT, position), field) for position, field in enumerate(self.fields_in_order)
        )

    def get_ordered_values(self, value: Any, path: str) -> list[Any]:
        """The element values of a value in the elements' order.

        Args:
            value: A tuple or list, or a dict of a named tuple's elements.
            path: The path to the value, named in an error.

        Returns:
            The values.

        Raises:
            ValidationError: The value is of another shape, or holds another number of elements or
                other names.
        """
        names = self.element_names
        if isinstance(value, Mapping) and names is not None:
            if set(value) != set(names):
                raise ValidationError(f"{path}: expected the elements {list(names)}, got {sorted(value)!r}")
            return [value[name] for name in names]
        if not isinstance(value, (tuple, list)):
            raise ValidationError(f"{path}: expected a tuple, got {self.get_value_for_message(value)}")
        if len(value) != len(self.fields_in_order):
            raise ValidationError(f"{path}: expected {len(self.fields_in_order)} elements, got {len(value)}")
        return list(value)

    def get_element_path(self, path: str, position: int) -> str:
        """The path to one element.

        Args:
            path: The path to the tuple.
            position: The element's position.

        Returns:
            ``path.<name>`` of a named element, ``path.<position>`` else.
        """
        return f"{path}.{self.element_names[position] if self.element_names is not None else position}"

    def encode_value(
        self, value: Any, instance: type[Model] | Model | None, types: TypeRegistry | None, path: str
    ) -> Any:
        return TupleValue(
            self.encode_child(field, element, instance, types, self.get_element_path(path, position))
            for position, (field, element) in enumerate(
                zip(self.fields_in_order, self.get_ordered_values(value, path), strict=True)
            )
        )

    def decode_value(self, value: Any, types: TypeRegistry | None) -> Any:
        names = self.element_names
        elements = [value[name] for name in names] if isinstance(value, Mapping) and names is not None else value
        decoded = [
            self.decode_child(field, element, types)
            for field, element in zip(self.fields_in_order, elements, strict=True)
        ]
        if names is not None:
            return dict(zip(names, decoded, strict=True))
        return tuple(decoded)

    def normalize_value(self, value: Any, path: str) -> Any:
        names = self.element_names
        if isinstance(value, Mapping) and names is not None and set(value) == set(names):
            value = [value[name] for name in names]
        elif not isinstance(value, (tuple, list)) or len(value) != len(self.fields_in_order):
            return value
        normalized = [
            self.normalize_child(field, element, self.get_element_path(path, position))
            for position, (field, element) in enumerate(zip(self.fields_in_order, value, strict=True))
        ]
        return dict(zip(names, normalized, strict=True)) if names is not None else tuple(normalized)

    def get_lookups(self) -> dict[str, FieldLookup]:
        # Local import: the lookups import the fields package.
        from hare.query.filters.lookups.containers.container_field_lookups import ContainerFieldLookups

        return ContainerFieldLookups.get_whole_value_lookups()

    def get_path_transform(self, segment: str) -> tuple[Callable[[Term], Term], Field[Any]] | None:
        """An element by its position (``point__0``) or name (``point__lat``)."""
        # Local import: hare.sql's terms import the fields package.
        from hare.sql.terms.containers import TupleElementTerm

        if CONTAINER_INDEX_PATH_PATTERN.fullmatch(segment):
            position = int(segment)
            if not 0 <= position < len(self.fields_in_order):
                return None
        elif self.element_names is not None and segment in self.element_names:
            position = self.element_names.index(segment)
        else:
            return None
        return partial(TupleElementTerm, index=position), self.fields_in_order[position]

    def get_lookup_value_description(self, lookup: str) -> tuple[LookupValueShape, Any] | None:
        """Equality takes the whole tuple."""
        if lookup in {Lookup.EXACT, Lookup.NOT}:
            return LookupValueShape.VALUE, dict if self.element_names is not None else tuple
        return None

    def get_python_type(self) -> Any:
        element_types = [self.get_child_python_type(field) for field in self.fields_in_order]
        if self.element_names is not None:
            # A dict of exactly the named elements, each of its type.
            return TypedDict(  # type: ignore[operator]
                f"{type(self).__name__}Elements", dict(zip(self.element_names, element_types, strict=True))
            )
        return tuple.__class_getitem__(tuple(element_types))
