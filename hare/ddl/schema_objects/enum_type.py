from __future__ import annotations

from dataclasses import dataclass


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
