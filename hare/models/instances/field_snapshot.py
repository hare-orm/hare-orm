from __future__ import annotations

import math
from collections.abc import Iterator, Mapping
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from hare.fields.constants import SENSITIVE_VALUE_PLACEHOLDER
from hare.models.constants import PLACEHOLDER_FIELD_VALUE_TYPES, UNCOPIED_FIELD_VALUE_TYPES
from hare.time.timezone import Timezone

if TYPE_CHECKING:
    from hare.models.model import Model
    from hare.models.model_binding import ModelBinding


class FieldSnapshot(Mapping[str, Any]):
    """A read-only copy of an instance's direct field values at one moment - built by
    ``Model.snapshot()``, compared by ``Model.diff_against()``. Mutable values are deep-copied.
    """

    __slots__ = ("_binding", "_values")

    _binding: ModelBinding
    _values: Mapping[str, Any]

    def __init__(self, binding: ModelBinding, values: dict[str, Any]) -> None:
        """
        Args:
            binding: The binding of the model whose values were captured.
            values: Already-copied field values keyed by model field name.
        """
        object.__setattr__(self, "_binding", binding)
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
        """Whether a field's value changed. Dict/list/tuple values and every value with ``compare_types``
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
        if compare_types or isinstance(old_value, (dict, list, tuple)):
            return not FieldSnapshot.is_same_typed_value(old_value, new_value)
        if isinstance(old_value, datetime) and isinstance(new_value, datetime):
            return Timezone.get_comparison_datetime(old_value) != Timezone.get_comparison_datetime(new_value)
        # A NaN compares unequal to itself but represents the same stored value as another NaN.
        return old_value != new_value and not (FieldSnapshot.is_nan(old_value) and FieldSnapshot.is_nan(new_value))

    @staticmethod
    def is_nan(value: Any) -> bool:
        """Whether a value is a float or ``Decimal`` NaN.

        Args:
            value: A value.
        """
        if isinstance(value, float):
            return math.isnan(value)
        return isinstance(value, Decimal) and value.is_nan()

    @staticmethod
    def is_same_typed_value(first_value: Any, second_value: Any) -> bool:
        """Compare types and stored values at every level of dicts, lists and tuples.

        Args:
            first_value: A value.
            second_value: Another value.
        """
        if type(first_value) is not type(second_value):
            return False
        if isinstance(first_value, dict):
            return first_value.keys() == second_value.keys() and all(
                FieldSnapshot.is_same_typed_value(nested_value, second_value[key])
                for key, nested_value in first_value.items()
            )
        if isinstance(first_value, (list, tuple)):
            return len(first_value) == len(second_value) and all(
                FieldSnapshot.is_same_typed_value(first_item, second_item)
                for first_item, second_item in zip(first_value, second_value, strict=True)
            )
        return not FieldSnapshot.values_differ(first_value, second_value)

    @property
    def model_class(self) -> type[Model]:
        """Model class the snapshot was taken from."""
        return self._binding._model

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
        sensitive_fields = self._binding.sensitive_fields
        shown_values = ", ".join(
            f"{field_name!r}: {SENSITIVE_VALUE_PLACEHOLDER if field_name in sensitive_fields else repr(value)}"
            for field_name, value in self._values.items()
        )
        return f"<{type(self).__name__} {self._binding._model.__name__} {{{shown_values}}}>"
