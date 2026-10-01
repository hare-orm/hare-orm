"""Reverse-engineering models from an existing database: read its schema (``SchemaIntrospector``),
then write model source (``SchemaInspector``, ``ModelSourceGenerator``) or build model classes
(``ModelFactory``)."""

from hare.inspectdb.exceptions import DuplicateModelClassNameError, ManyToManyThroughTableSkippedError
from hare.inspectdb.inspected_model import InspectedModel
from hare.inspectdb.inspected_model_builder import InspectedModelBuilder
from hare.inspectdb.inspector.schema_inspector import SchemaInspector
from hare.inspectdb.introspector import SchemaNotFoundError, TableNotFoundError, UnsupportedDialectError
from hare.inspectdb.introspector.schema_introspector import SchemaIntrospector
from hare.inspectdb.model_factory import ModelFactory
from hare.inspectdb.model_source_generator import ModelSourceGenerator
from hare.inspectdb.type_mapping import ColumnTypeMapper
from hare.inspectdb.types.column_info import ColumnInfo
from hare.inspectdb.types.foreign_key_info import ForeignKeyInfo
from hare.inspectdb.types.index_info import IndexInfo
from hare.inspectdb.types.table_info import TableInfo

__all__ = [
    "SchemaInspector",
    "SchemaIntrospector",
    "SchemaNotFoundError",
    "ModelSourceGenerator",
    "ModelFactory",
    "InspectedModel",
    "InspectedModelBuilder",
    "ColumnTypeMapper",
    "ManyToManyThroughTableSkippedError",
    "DuplicateModelClassNameError",
    "TableNotFoundError",
    "UnsupportedDialectError",
    "ColumnInfo",
    "ForeignKeyInfo",
    "IndexInfo",
    "TableInfo",
]
