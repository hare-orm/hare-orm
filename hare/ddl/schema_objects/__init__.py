"""What a model declares beside its table - views, materialized views, dictionaries, functions,
sequences, enum types and triggers."""

from __future__ import annotations

from hare.ddl.schema_objects.database_function import DatabaseFunction
from hare.ddl.schema_objects.database_sequence import DatabaseSequence
from hare.ddl.schema_objects.dictionary import Dictionary
from hare.ddl.schema_objects.enum_type import EnumType
from hare.ddl.schema_objects.materialized_view import MaterializedView
from hare.ddl.schema_objects.view import View

__all__ = ["DatabaseFunction", "DatabaseSequence", "Dictionary", "EnumType", "MaterializedView", "View"]
