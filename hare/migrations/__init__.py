"""Vendored migrations package (experimental)."""

from hare.migrations.execution.runner import MigrationRunner
from hare.migrations.loading.recorder.migration_recorder import MigrationRecorder
from hare.migrations.migration import Migration
from hare.migrations.operations import (
    AddConstraint,
    AddField,
    AddIndex,
    AddTrigger,
    AlterField,
    AlterModelOptions,
    AlterTrigger,
    CreateModel,
    CreateSchema,
    DeleteModel,
    DropSchema,
    HareOperation,
    Operation,
    RemoveConstraint,
    RemoveField,
    RemoveIndex,
    RemoveTrigger,
    RenameConstraint,
    RenameField,
    RenameIndex,
    RenameModel,
    RenameTrigger,
    RunPython,
    RunSQL,
    SQLOperation,
)
from hare.migrations.reports.operation_effect import OperationEffect
from hare.migrations.reports.operation_plan import OperationPlan
from hare.migrations.state.project.state import State
from hare.migrations.swappable import SwappableDependency, swappable_dependency
from hare.migrations.writer.migration_writer import MigrationWriter

__all__ = [
    "AddConstraint",
    "AddField",
    "AddIndex",
    "AddTrigger",
    "AlterField",
    "AlterModelOptions",
    "AlterTrigger",
    "CreateModel",
    "CreateSchema",
    "DeleteModel",
    "DropSchema",
    "Migration",
    "MigrationRecorder",
    "MigrationRunner",
    "MigrationWriter",
    "Operation",
    "OperationEffect",
    "OperationPlan",
    "RemoveConstraint",
    "RemoveField",
    "RemoveIndex",
    "RemoveTrigger",
    "RenameConstraint",
    "RenameField",
    "RenameIndex",
    "RenameModel",
    "RenameTrigger",
    "RunPython",
    "RunSQL",
    "SQLOperation",
    "State",
    "HareOperation",
    "SwappableDependency",
    "swappable_dependency",
]
