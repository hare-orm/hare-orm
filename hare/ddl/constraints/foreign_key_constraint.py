from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.utils.class_path import ClassPath

if TYPE_CHECKING:
    pass


@dataclass(frozen=True)
class ForeignKeyConstraint:
    """A table-level composite FOREIGN KEY - built for a relation to a composite key; a single-column
    relation is an inline ``REFERENCES``. Not declared by hand.
    """

    fields: tuple[str, ...]
    to_table: str
    to_fields: tuple[str, ...]
    on_delete: str
    name: str

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        path = ClassPath.get(self.__class__)
        return (
            path,
            [],
            {
                "fields": self.fields,
                "to_table": self.to_table,
                "to_fields": self.to_fields,
                "on_delete": self.on_delete,
                "name": self.name,
            },
        )
