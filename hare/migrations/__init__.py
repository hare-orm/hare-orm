"""Vendored migrations package (experimental)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hare.classes.lazy_exports import LazyExports
from hare.migrations.constants import EXPORTED_MODULES

# Imported with the package: the function is named as its module, which, imported on first use, would
# take the function's place on the package.
from hare.migrations.swappable_dependency import SwappableDependency, swappable_dependency

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.migrations.execution.migration_runner import MigrationRunner
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
    from hare.migrations.state.state import State
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


# A module of the package (its exceptions, read by the schema editor) is imported without the rest of
# it - the migration machinery loads only once a name of it is read.
__getattr__ = LazyExports(__name__, EXPORTED_MODULES).get
