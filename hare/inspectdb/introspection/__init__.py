"""Reads an existing database's schema into plain dataclasses (TableInfo/ColumnInfo/...) - the
intermediate representation ModelSourceGenerator renders back out as hare-orm model source. Each
dialect reads its own catalog through its ``SchemaIntrospector`` subclass (``Dialect.introspector_class``).
"""

from __future__ import annotations

from hare.inspectdb.exceptions import SchemaNotFoundError, TableNotFoundError, UnsupportedDialectError
from hare.inspectdb.introspection.column_info import ColumnInfo
from hare.inspectdb.introspection.composite_foreign_key_info import CompositeForeignKeyInfo
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.inspectdb.introspection.foreign_key_info import ForeignKeyInfo
from hare.inspectdb.introspection.index_info import IndexInfo
from hare.inspectdb.introspection.schema_introspector import SchemaIntrospector
from hare.inspectdb.introspection.table_info import TableInfo

__all__ = [
    "DatabaseCatalog",
    "ColumnInfo",
    "CompositeForeignKeyInfo",
    "ForeignKeyInfo",
    "IndexInfo",
    "SchemaIntrospector",
    "TableInfo",
    "UnsupportedDialectError",
    "SchemaNotFoundError",
    "TableNotFoundError",
]
