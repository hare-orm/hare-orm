from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from hare.exceptions import ConfigurationError


@dataclass(frozen=True, slots=True)
class EnumType:
    """A database ``ENUM`` type a column is of - its schema, its name and its labels, in their order.
    Types of one name in two schemas are two types.

    Attributes:
        name: The type's name.
        labels: The labels.
        schema: The schema it lives in - None for the connection's default one.
    """

    name: str
    labels: tuple[str, ...]
    schema: str | None = None

    @property
    def key(self) -> tuple[str | None, str]:
        """What tells the type from another - its schema and name."""
        return self.schema, self.name

    @staticmethod
    def collect(
        types_by_connection: Iterable[tuple[str | None, Iterable[EnumType | None]]],
    ) -> dict[tuple[str | None, str | None, str], EnumType]:
        """Collect declarations by connection, schema and name in first-use order."""
        collected: dict[tuple[str | None, str | None, str], EnumType] = {}
        for alias, types in types_by_connection:
            for enum_type in types:
                if enum_type is None:
                    continue
                known = collected.setdefault((alias, *enum_type.key), enum_type)
                if known.labels != enum_type.labels:
                    raise ConfigurationError(
                        f"Two fields name the ENUM type {enum_type.name!r} with different labels: "
                        f"{list(known.labels)} and {list(enum_type.labels)}"
                    )
        return collected
