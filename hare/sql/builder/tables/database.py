from __future__ import annotations

from hare.sql.builder.tables.schema import Schema
from hare.sql.builder_methods import BuilderMethods


class Database(Schema):
    @BuilderMethods.ignore_copy
    def __getattr__(self, item: str) -> Schema:  # type:ignore[override]
        return Schema(item, parent=self)
