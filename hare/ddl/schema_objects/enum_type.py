from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EnumType:
    """A database ``ENUM`` type a column is of - its name and its labels, in their order.

    Attributes:
        name: The type's name.
        labels: The labels.
    """

    name: str
    labels: tuple[str, ...]
