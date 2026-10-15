from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.base.schema.tables.table_creation import TableCreation

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


class ClickhouseTableCreation(TableCreation):
    """The ``CREATE TABLE`` of ClickHouse - the primary key, composite too, is the ``PRIMARY KEY``
    clause of the table's engine (``ClickhouseTableOptions``)."""

    __slots__ = ()

    def get_composite_pk_constraint_sql(self, model: type[Model], field_names: list[str]) -> str:
        return ""
