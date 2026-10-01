from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.identifiers import Identifiers
from hare.sql.context import SqlContext
from hare.sql.terms.base.node import Node
from hare.sql.terms.field import Field
from hare.sql.terms.star import Star
from hare.sql.utils import builder, ignore_copy

if TYPE_CHECKING:
    from typing import Self

    from hare.sql.queries.tables.table import Table


class Selectable(Node):
    def __init__(self, alias: str | None) -> None:
        self.alias = alias

    @builder
    def as_(self, alias: str) -> Self:  # type:ignore[return]
        # Join aliases are built cumulatively (previous alias + "__" + hop) with no length cap, so
        # two long multi-hop paths would collide once the database truncates them to its
        # identifier limit - they are shortened with a digest of the whole alias instead.
        self.alias = Identifiers.get_within_limit(alias)

    def field(self, name: str) -> Field:
        return Field(name, table=self)

    def replace_table(self, current_table: Table | None, new_table: Table | None) -> Self:
        """Replaces every occurrence of a table with another one - returns self unless a subclass can
        replace it.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            The selectable with the table replaced.
        """
        return self

    @property
    def star(self) -> Star:
        return Star(self)

    @ignore_copy
    def __getattr__(self, name: str) -> Field:
        return self.field(name)

    @ignore_copy
    def __getitem__(self, name: str) -> Field:
        return self.field(name)

    def get_table_name(self) -> str | None:
        return self.alias

    def get_sql(self, ctx: SqlContext) -> str:
        raise NotImplementedError()
