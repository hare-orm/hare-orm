"""Reads an existing database's schema into plain dataclasses (TableInfo/ColumnInfo/...) - the
intermediate representation ModelSourceGenerator renders back out as hare-orm model source. Each
dialect reads its own catalog through its ``SchemaIntrospector`` subclass (``Dialect.introspector_class``).
"""

from hare.inspectdb.exceptions import SchemaNotFoundError, TableNotFoundError, UnsupportedDialectError
from hare.inspectdb.introspector.schema_introspector import SchemaIntrospector

__all__ = [
    "SchemaIntrospector",
    "UnsupportedDialectError",
    "SchemaNotFoundError",
    "TableNotFoundError",
]
