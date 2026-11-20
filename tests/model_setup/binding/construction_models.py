from __future__ import annotations

from typing import Any, Self

from hare import Model, fields


class ConstructionAuthor(Model):
    name = fields.CharField(max_length=40)


class ConstructionBook(Model):
    title = fields.CharField(max_length=40)
    author = fields.ForeignKeyField(ConstructionAuthor, related_name="books", null=True)

    def __new__(cls, **values: Any) -> Self:
        obj = super().__new__(cls, **values)
        obj.new_author = ConstructionAuthor(name="from allocation")
        return obj

    def __init__(self, **values: Any) -> None:
        super().__init__(**values)
        self.initial_author = ConstructionAuthor(name="from initialization")


__models__ = [ConstructionAuthor, ConstructionBook]
