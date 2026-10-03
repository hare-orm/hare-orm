"""The operations of a migration - what ``makemigrations`` writes into a migration file as
``ops.<Operation>(...)``."""

from hare.migrations.operations.base import HareOperation, ModelBoundOperation, Operation
from hare.migrations.operations.columns import AlterColumnNotNullSafe, BackfillColumn
from hare.migrations.operations.constraints import (
    AddConstraint,
    RemoveConstraint,
    RenameConstraint,
    ValidateConstraint,
)
from hare.migrations.operations.extensions import CreateCollation, CreateExtension, RemoveCollation, RemoveExtension
from hare.migrations.operations.fields import AddField, AlterField, RemoveField, RenameField
from hare.migrations.operations.indexes import AddIndex, RemoveIndex, RenameIndex
from hare.migrations.operations.models import (
    AlterModelOptions,
    AlterModelSchema,
    AlterModelTable,
    CreateModel,
    DeleteModel,
    RenameModel,
)
from hare.migrations.operations.partitions import AddPartition, RemovePartition
from hare.migrations.operations.python import RunPython
from hare.migrations.operations.schema_objects import (
    AddSchemaObject,
    AlterSchemaObject,
    RemoveSchemaObject,
    RenameSchemaObject,
)
from hare.migrations.operations.schemas import CreateSchema, DropSchema
from hare.migrations.operations.sql import RunSQL, SQLOperation
from hare.migrations.operations.triggers import AddTrigger, AlterTrigger, RemoveTrigger, RenameTrigger

__all__ = [
    "Operation",
    "SQLOperation",
    "HareOperation",
    "ModelBoundOperation",
    "CreateModel",
    "RenameModel",
    "DeleteModel",
    "AlterModelOptions",
    "AlterModelTable",
    "AlterModelSchema",
    "AddPartition",
    "RemovePartition",
    "AddField",
    "RemoveField",
    "AlterField",
    "RenameField",
    "BackfillColumn",
    "AlterColumnNotNullSafe",
    "AddSchemaObject",
    "RemoveSchemaObject",
    "RenameSchemaObject",
    "AlterSchemaObject",
    "AddIndex",
    "RemoveIndex",
    "RenameIndex",
    "AddConstraint",
    "RemoveConstraint",
    "RenameConstraint",
    "ValidateConstraint",
    "AddTrigger",
    "RemoveTrigger",
    "AlterTrigger",
    "RenameTrigger",
    "RunPython",
    "RunSQL",
    "CreateSchema",
    "DropSchema",
    "CreateExtension",
    "RemoveExtension",
    "CreateCollation",
    "RemoveCollation",
]
