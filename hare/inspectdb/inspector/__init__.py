"""``hare inspectdb``: model source from an existing database. The database's types don't map 1:1 onto
fields - review the result. SchemaIntrospector reads the schema; ModelSourceBuilder renders it.
Works on every dialect with an introspector.
"""

from hare.inspectdb.exceptions import (
    DuplicateModelClassNameError,
    SchemaNotFoundError,
    TableNotFoundError,
    UnsupportedDialectError,
)
from hare.inspectdb.inspector.schema_inspector import SchemaInspector

__all__ = [
    "DuplicateModelClassNameError",
    "SchemaInspector",
    "SchemaNotFoundError",
    "TableNotFoundError",
    "UnsupportedDialectError",
]
