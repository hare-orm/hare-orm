from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar, get_origin, is_typeddict

from pydantic import ConfigDict, TypeAdapter

from hare.contrib.request_query.paginations.constants import CURSOR_SCALAR_TYPES
from hare.core.caching.cache import Cache
from hare.fields.data.containers.container_field import ContainerField
from hare.fields.data.json.json_field import JSONField
from hare.fields.field import Field
from hare.fields.generated_field import GeneratedField


class CursorValues:
    """Reversible cursor values, sharing the fields' existing container traversal.

    Enum member names identify members without serializing their arbitrary Python values.
    Only adapters for built-in scalar types are cached; application fields and enum classes
    belong to the page's prepared conversion and are not retained globally.
    """

    __slots__ = ()

    container_values_always_nullable = True
    scalar_adapters: ClassVar[Cache[TypeAdapter[Any]]] = Cache(len(CURSOR_SCALAR_TYPES))

    def get_db_writer(self, field: Field[Any]) -> Callable[[Any, Any], Any]:
        field = GeneratedField.get_effective_field(field)
        if isinstance(field, ContainerField):
            return field.get_value_writer(self)
        if field.enum_type is not None:
            return lambda value, instance: value.name
        return lambda value, instance: value

    def get_python_reader(self, field: Field[Any]) -> Callable[[Any], Any]:
        field = GeneratedField.get_effective_field(field)
        if isinstance(field, ContainerField):
            return field.get_value_reader(self)
        if field.enum_type is not None:
            return field.enum_type.__getitem__
        if isinstance(field, JSONField) and field.declared_value_type is None:
            return lambda value: value
        return self.get_type_reader(field.get_python_type())

    def get_reader(self, value_type: Any) -> Callable[[Any], Any]:
        return (
            self.get_python_reader(value_type) if isinstance(value_type, Field) else self.get_type_reader(value_type)
        )

    @classmethod
    def get_type_reader(cls, value_type: Any) -> Callable[[Any], Any]:
        """Adapt Python value types and scalar leaves with the same byte encoding."""
        if value_type is None:
            return lambda value: value
        if any(value_type is scalar_type for scalar_type in CURSOR_SCALAR_TYPES):
            adapter = cls.scalar_adapters.get((value_type,))
            if adapter is None:
                adapter = (
                    TypeAdapter(bytes, config=ConfigDict(val_json_bytes="base64"))
                    if value_type is bytes
                    else TypeAdapter(value_type)
                )
                cls.scalar_adapters[(value_type,)] = adapter
            return adapter.validate_python
        if get_origin(value_type) in (list, dict, tuple) or is_typeddict(value_type):
            container_type: Any = list.__class_getitem__(value_type)
            adapter = TypeAdapter(container_type, config=ConfigDict(val_json_bytes="base64"))
            return lambda value: adapter.validate_python([value])[0]
        return TypeAdapter(value_type).validate_python
