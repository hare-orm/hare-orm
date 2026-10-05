"""The operations of a migration - what ``makemigrations`` writes into a migration file as
``ops.<Operation>(...)``."""

from __future__ import annotations

from hare.migrations.operations.columns import AlterColumnNotNullSafe, BackfillColumn
from hare.migrations.operations.constraints import (
    AddConstraint,
    RemoveConstraint,
    RenameConstraint,
    ValidateConstraint,
)
from hare.migrations.operations.database_functions import AddFunction, AlterFunction, RemoveFunction, RenameFunction
from hare.migrations.operations.dictionaries import AddDictionary, AlterDictionary, RemoveDictionary, RenameDictionary
from hare.migrations.operations.enum_types import AlterEnumType, CreateEnumType, DropEnumType
from hare.migrations.operations.extensions import CreateCollation, CreateExtension, RemoveCollation, RemoveExtension
from hare.migrations.operations.fields import AddField, AlterField, RemoveField, RenameField
from hare.migrations.operations.grants import AddGrant, RemoveGrant
from hare.migrations.operations.hare_operation import HareOperation
from hare.migrations.operations.indexes import AddIndex, RemoveIndex, RenameIndex
from hare.migrations.operations.materialized_views import (
    AddMaterializedView,
    AlterMaterializedView,
    RefreshMaterializedView,
    RemoveMaterializedView,
    RenameMaterializedView,
)
from hare.migrations.operations.models import (
    AlterModelOptions,
    AlterModelSchema,
    AlterModelTable,
    CreateModel,
    DeleteModel,
    RenameModel,
)
from hare.migrations.operations.operation import Operation
from hare.migrations.operations.partitions import AddPartition, RemovePartition
from hare.migrations.operations.policies import (
    AddPolicy,
    AlterPolicy,
    AlterRowLevelSecurity,
    RemovePolicy,
    RenamePolicy,
)
from hare.migrations.operations.schema_objects import (
    AddSchemaObject,
    AlterSchemaObject,
    RemoveSchemaObject,
    RenameSchemaObject,
)
from hare.migrations.operations.schemas import CreateSchema, DropSchema
from hare.migrations.operations.sequences import AddSequence, AlterSequence, RemoveSequence, RenameSequence
from hare.migrations.operations.special import SeparateDatabaseAndState, SynchronizeKeySeries
from hare.migrations.operations.special.run_python import RunPython
from hare.migrations.operations.special.run_sql import RunSQL
from hare.migrations.operations.special.sql_operation import SQLOperation
from hare.migrations.operations.triggers import AddTrigger, AlterTrigger, RemoveTrigger, RenameTrigger
from hare.migrations.operations.views import AddView, AlterView, RemoveView, RenameView

__all__ = [
    "Operation",
    "SQLOperation",
    "HareOperation",
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
    "AddView",
    "AlterView",
    "RemoveView",
    "RenameView",
    "AddMaterializedView",
    "AlterMaterializedView",
    "RemoveMaterializedView",
    "RenameMaterializedView",
    "RefreshMaterializedView",
    "SynchronizeKeySeries",
    "AddDictionary",
    "AlterDictionary",
    "RemoveDictionary",
    "RenameDictionary",
    "AddFunction",
    "AlterFunction",
    "RemoveFunction",
    "RenameFunction",
    "AddSequence",
    "AlterSequence",
    "RemoveSequence",
    "RenameSequence",
    "AlterRowLevelSecurity",
    "AddPolicy",
    "AlterPolicy",
    "RemovePolicy",
    "RenamePolicy",
    "AddGrant",
    "RemoveGrant",
    "RunPython",
    "RunSQL",
    "SeparateDatabaseAndState",
    "CreateSchema",
    "DropSchema",
    "CreateExtension",
    "CreateEnumType",
    "AlterEnumType",
    "DropEnumType",
    "RemoveExtension",
    "CreateCollation",
    "RemoveCollation",
]
