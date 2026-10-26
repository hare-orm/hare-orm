from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

from hare.contrib.request_query.api_getter.constants import API_PATH_SEPARATOR
from hare.contrib.request_query.api_getter.enums import ApiValueMappingMode
from hare.exceptions import ConfigurationError
from hare.fields.field import Field


@dataclasses.dataclass(frozen=True, slots=True)
class ApiValueMapping:
    """Where a value goes - read from a source's answer item, or, given in ``Meta.api_getter`` itself, from
    the object.

    Args:
        set_key: The path of the attribute (or key) set on the object (``"partner.user"``) - lists on the
            way are walked. Of a source's mapping, the object is the one holding the source's key.
        key: The path of the value in the answer item (or on the object) - the whole item when None.
        mode: Which value a target set by several mappings keeps.
        required: The target can't stay empty - ``ApiValueMissingError`` otherwise.
        default: The value of a target left empty.
        field: A field of hare the value is read through - ``DatetimeField()`` makes a datetime of an ISO
            text.
        convert: A function the value goes through - after ``field`` when both are given.

    Raises:
        ConfigurationError: ``required`` with a ``default``.
    """

    set_key: str
    key: str | None = None
    mode: ApiValueMappingMode = ApiValueMappingMode.LAST
    required: bool = False
    default: Any = None
    field: Field[Any] | None = None
    convert: Callable[[Any], Any] | None = None

    def __post_init__(self) -> None:
        if self.required and self.default is not None:
            raise ConfigurationError(f"ApiValueMapping {self.set_key!r}: required and default can't both be given")

    def get_set_name(self) -> str:
        """The name of the target in the object holding it - the last part of ``set_key``."""
        return self.set_key.rsplit(API_PATH_SEPARATOR, 1)[-1]

    def get_converted(self, value: Any) -> Any:
        """A value read through ``field`` and ``convert`` - None stays None.

        Args:
            value: The value.

        Returns:
            The value converted.
        """
        if value is None:
            return None
        if self.field is not None:
            value = self.field.to_python(value)
        if self.convert is not None:
            value = self.convert(value)
        return value
