"""Dataclasses SchemaIntrospector fills in while reading an existing database schema, before
ModelSourceGenerator renders them back out as hare-orm model source."""

from hare.inspectdb.types.column_info import ColumnInfo
from hare.inspectdb.types.composite_foreign_key_info import CompositeForeignKeyInfo
from hare.inspectdb.types.foreign_key_info import ForeignKeyInfo
from hare.inspectdb.types.index_info import IndexInfo
from hare.inspectdb.types.table_info import TableInfo

__all__ = [
    "ColumnInfo",
    "ForeignKeyInfo",
    "CompositeForeignKeyInfo",
    "IndexInfo",
    "TableInfo",
]
