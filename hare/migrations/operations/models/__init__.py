"""Operations on models and their tables."""

from __future__ import annotations

from hare.migrations.operations.models.alter_model_options import AlterModelOptions
from hare.migrations.operations.models.alter_model_schema import AlterModelSchema
from hare.migrations.operations.models.alter_model_table import AlterModelTable
from hare.migrations.operations.models.create_model import CreateModel
from hare.migrations.operations.models.delete_model import DeleteModel
from hare.migrations.operations.models.model_table_operation import ModelTableOperation
from hare.migrations.operations.models.rename_model import RenameModel
from hare.migrations.operations.models.table_renaming_operation import TableRenamingOperation

__all__ = [
    "AlterModelOptions",
    "AlterModelSchema",
    "AlterModelTable",
    "CreateModel",
    "DeleteModel",
    "ModelTableOperation",
    "RenameModel",
    "TableRenamingOperation",
]
