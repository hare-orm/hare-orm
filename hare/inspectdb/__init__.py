"""Reverse-engineering models from an existing database: read its schema (``SchemaIntrospector``),
then write model source (``SchemaInspector``, ``ModelSourceGenerator``) or build model classes
(``ModelFactory``)."""

from __future__ import annotations

from hare.inspectdb.exceptions import DuplicateModelClassNameError, ManyToManyThroughTableSkippedError
from hare.inspectdb.generation.column_type_mapper import ColumnTypeMapper
from hare.inspectdb.generation.declarations import ModelGenerationOptions
from hare.inspectdb.generation.inspected_model import InspectedModel
from hare.inspectdb.generation.inspected_model_builder import InspectedModelBuilder
from hare.inspectdb.generation.model_factory import ModelFactory
from hare.inspectdb.generation.model_source_generator import ModelSourceGenerator
from hare.inspectdb.introspection import SchemaNotFoundError, TableNotFoundError, UnsupportedDialectError
from hare.inspectdb.introspection.column_info import ColumnInfo
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.inspectdb.introspection.foreign_key_info import ForeignKeyInfo
from hare.inspectdb.introspection.index_info import IndexInfo
from hare.inspectdb.introspection.schema_introspector import SchemaIntrospector
from hare.inspectdb.introspection.table_info import TableInfo
from hare.inspectdb.schema_inspector import SchemaInspector

__all__ = [
    "DatabaseCatalog",
    "SchemaInspector",
    "SchemaIntrospector",
    "SchemaNotFoundError",
    "ModelSourceGenerator",
    "ModelFactory",
    "InspectedModel",
    "ModelGenerationOptions",
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
