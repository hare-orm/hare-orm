from __future__ import annotations

from collections.abc import Iterator, Mapping
from copy import deepcopy
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from hare.fields.constants import SENSITIVE_VALUE_PLACEHOLDER
from hare.models.constants import PLACEHOLDER_FIELD_VALUE_TYPES, UNCOPIED_FIELD_VALUE_TYPES

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models.model import Model


class FieldSnapshot(Mapping[str, Any]):
    """A read-only copy of an instance's direct field values at one moment - built by
    ``Model.snapshot()``, compared by ``Model.diff_against()``. Mutable values are deep-copied.
    """

    __slots__ = ("_model_class", "_values")

    _model_class: type[Model]
    _values: Mapping[str, Any]

    def __init__(self, model_class: type[Model], values: dict[str, Any]) -> None:
        """
        Args:
            model_class: Model class the values were captured from.
            values: Already-copied field values keyed by model field name.
        """
        object.__setattr__(self, "_model_class", model_class)
        object.__setattr__(self, "_values", MappingProxyType(values))

    @staticmethod
    def copy_value(value: Any) -> Any:
        """Returns ``value`` itself when it's immutable or a placeholder (a ``db_default`` marker,
        an unresolved expression), otherwise a deep copy of it."""
        if isinstance(value, UNCOPIED_FIELD_VALUE_TYPES):
            return value
        return deepcopy(value)

    @staticmethod
    def values_differ(old_value: Any, new_value: Any, *, compare_types: bool = False) -> bool:
        """Whether a field's value changed. Dict/list values - and every value with ``compare_types`` -
        also count as changed when a nested value only changed type: ``1``, ``1.0`` and ``True`` are
        stored differently in JSON.

        Args:
            old_value: The baseline value.
            new_value: The current value.
            compare_types: Compare types at the top level too.
        """
        if isinstance(old_value, PLACEHOLDER_FIELD_VALUE_TYPES) or isinstance(
            new_value, PLACEHOLDER_FIELD_VALUE_TYPES
        ):
            return old_value is not new_value
        if old_value != new_value:
            return True
        if compare_types or isinstance(old_value, (dict, list)):
            return not FieldSnapshot.is_same_typed_value(old_value, new_value)
        return False

    @staticmethod
    def is_same_typed_value(first_value: Any, second_value: Any) -> bool:
        """Whether two equal values also have the same type, recursively through dicts and lists.

        Args:
            first_value: A value.
            second_value: A value equal to ``first_value``.
        """
        if type(first_value) is not type(second_value):
            return False
        if isinstance(first_value, dict):
            return all(
                FieldSnapshot.is_same_typed_value(nested_value, second_value[key])
                for key, nested_value in first_value.items()
            )
        if isinstance(first_value, (list, tuple)):
            return all(
                FieldSnapshot.is_same_typed_value(first_item, second_item)
                for first_item, second_item in zip(first_value, second_value, strict=True)
            )
        return True

    @property
    def model_class(self) -> type[Model]:
        """Model class the snapshot was taken from."""
        return self._model_class

    @property
    def fields(self) -> frozenset[str]:
        """Names of the fields captured in the snapshot."""
        return frozenset(self._values)

    def __getitem__(self, field_name: str) -> Any:
        return self._values[field_name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __repr__(self) -> str:
        sensitive_fields = self._model_class._meta.sensitive_fields
        shown_values = ", ".join(
            f"{field_name!r}: {SENSITIVE_VALUE_PLACEHOLDER if field_name in sensitive_fields else repr(value)}"
            for field_name, value in self._values.items()
        )
        return f"<{type(self).__name__} {self._model_class.__name__} {{{shown_values}}}>"
