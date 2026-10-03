from __future__ import annotations

from typing import TYPE_CHECKING

from hare.sql.utils import ignore_copy

if TYPE_CHECKING:
    pass
from hare.sql.queries.tables.schema import Schema


class Database(Schema):
    @ignore_copy
    def __getattr__(self, item: str) -> Schema:  # type:ignore[override]
        return Schema(item, parent=self)
