from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

from hare.classes.class_path import ClassPath
from hare.ddl.constants import SEQUENCE_CACHE_MAX, SEQUENCE_VALUE_MAX, SEQUENCE_VALUE_MIN
from hare.ddl.schema_objects.named_schema_object import NamedSchemaObject
from hare.exceptions import ConfigurationError


@dataclass(frozen=True)
class DatabaseSequence(NamedSchemaObject):
    """A sequence a model declares in ``Meta.sequences`` - a counter in the model's schema handing
    out numbers (``QuerySet.get_next_sequence_value()``), created before the model's table, so a
    column default can take from it.

    Args:
        name: The sequence's name.
        start: The first number; None for ``minimum`` counting up, ``maximum`` counting down.
        increment: What each number adds - negative counts down; never 0.
        minimum: The smallest number; None for the database's default.
        maximum: The largest number; None for the database's default.
        cycle: Start again from the other end after the last number instead of failing.
        cache: How many numbers a session takes in advance.
        owned_by: A field of the model - the sequence is dropped with its column.

    Raises:
        ConfigurationError: A number isn't an integer within the 64-bit range, ``increment`` is 0,
            ``cache`` is out of 1..``SEQUENCE_CACHE_MAX``, ``minimum`` isn't below ``maximum``,
            ``start`` is outside them, or ``owned_by`` isn't a non-empty string.
    """

    start: int | None = None
    increment: int = 1
    minimum: int | None = None
    maximum: int | None = None
    cycle: bool = False
    cache: int = 1
    owned_by: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        for attribute in ("start", "increment", "minimum", "maximum", "cache"):
            value = getattr(self, attribute)
            if value is None and attribute in {"start", "minimum", "maximum"}:
                continue
            if not isinstance(value, int) or isinstance(value, bool):
                raise ConfigurationError(
                    f"DatabaseSequence {self.name!r}: {attribute} must be an integer, got {value!r}"
                )
            if not SEQUENCE_VALUE_MIN <= value <= SEQUENCE_VALUE_MAX:
                raise ConfigurationError(
                    f"DatabaseSequence {self.name!r}: {attribute} must be within "
                    f"{SEQUENCE_VALUE_MIN}..{SEQUENCE_VALUE_MAX}, got {value}"
                )
        if self.increment == 0:
            raise ConfigurationError(f"DatabaseSequence {self.name!r}: increment can't be 0")
        if not 1 <= self.cache <= SEQUENCE_CACHE_MAX:
            raise ConfigurationError(
                f"DatabaseSequence {self.name!r}: cache must be within 1..{SEQUENCE_CACHE_MAX}, got {self.cache}"
            )
        if self.minimum is not None and self.maximum is not None and self.minimum >= self.maximum:
            raise ConfigurationError(
                f"DatabaseSequence {self.name!r}: minimum ({self.minimum}) must be below maximum ({self.maximum})"
            )
        if self.start is not None and (
            (self.minimum is not None and self.start < self.minimum)
            or (self.maximum is not None and self.start > self.maximum)
        ):
            raise ConfigurationError(
                f"DatabaseSequence {self.name!r}: start ({self.start}) must be within minimum..maximum"
            )
        if not isinstance(self.cycle, bool):
            raise ConfigurationError(f"DatabaseSequence {self.name!r}: cycle must be a bool, got {self.cycle!r}")
        if self.owned_by is not None and (not isinstance(self.owned_by, str) or not self.owned_by):
            raise ConfigurationError(
                f"DatabaseSequence {self.name!r}: owned_by must be a field name, got {self.owned_by!r}"
            )

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        kwargs: dict[str, Any] = {"name": self.name}
        for declared_field in fields(self):
            value = getattr(self, declared_field.name)
            if declared_field.name != "name" and value != declared_field.default:
                kwargs[declared_field.name] = value
        return ClassPath.get(type(self)), [], kwargs
