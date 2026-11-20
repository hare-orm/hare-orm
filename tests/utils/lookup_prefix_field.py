from typing import Any

from hare.fields import CharField
from hare.models import Model


class LookupPrefixField(CharField):
    """A lookup-only exchange prefix must never be written into the stored value."""

    def to_lookup_value(self, value: Any, instance: type[Model] | Model | None) -> Any:
        if isinstance(value, str):
            value = value.removeprefix("find:")
        return super().to_lookup_value(value, instance)
