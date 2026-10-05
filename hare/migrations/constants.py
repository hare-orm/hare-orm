from __future__ import annotations

import re

from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.models.enums import ModelOption

RELATION_FIELDS = (ForeignKeyFieldInstance, OneToOneFieldInstance, ManyToManyFieldInstance)


MIGRATION_NUMBER_RE = re.compile(r"^(\d{4})_")


#: Sentinel migration name resolving to the newest migration in an app.
LATEST_MIGRATION = "__latest__"


#: Sentinel migration name a dependency gives for the app's first (root) migration.
FIRST_MIGRATION = "__first__"


#: The module name a migration read from its source (``Migration.from_source()``) runs under -
#: followed by ``<app_label>.<migration_name>``.
RUNTIME_MIGRATION_MODULE_PREFIX = "hare_runtime_migration."


#: Model options each changed by an operation of its own (AlterModelTable, AlterModelSchema,
#: AddIndex, AddConstraint, AddTrigger, ...) - every other option is changed by AlterModelOptions,
#: which carries the whole set of them.
MODEL_OPTIONS_WITH_OWN_OPERATIONS = frozenset(
    {
        ModelOption.TABLE,
        ModelOption.TABLE_IS_EXPLICIT,
        ModelOption.SCHEMA,
        ModelOption.APP,
        ModelOption.INDEXES,
        ModelOption.CONSTRAINTS,
        ModelOption.TRIGGERS,
        ModelOption.PRIMARY_KEY_ATTRIBUTE,
        ModelOption.PK_WITHOUT_OVERLAPS,
        ModelOption.TENANT_SCHEMA,
        ModelOption.VIEWS,
        ModelOption.MATERIALIZED_VIEWS,
        ModelOption.DICTIONARIES,
        ModelOption.FUNCTIONS,
        ModelOption.SEQUENCES,
        ModelOption.POLICIES,
        ModelOption.GRANTS,
        ModelOption.ROW_LEVEL_SECURITY,
    }
)

#: The error of a migration the graph names but no file holds.
MISSING_MIGRATION_MESSAGE_TEMPLATE = "Missing migration for {key}"
#: The error of a migration target the graph doesn't hold.
UNKNOWN_MIGRATION_TARGET_MESSAGE_TEMPLATE = "Unknown migration target {target}"


#: The module of each name the package exports - each loaded when the name is first read.
EXPORTED_MODULES = {
    "AddConstraint": "hare.migrations.operations",
    "AddField": "hare.migrations.operations",
    "AddIndex": "hare.migrations.operations",
    "AddTrigger": "hare.migrations.operations",
    "AlterField": "hare.migrations.operations",
    "AlterModelOptions": "hare.migrations.operations",
    "AlterTrigger": "hare.migrations.operations",
    "CreateModel": "hare.migrations.operations",
    "CreateSchema": "hare.migrations.operations",
    "DeleteModel": "hare.migrations.operations",
    "DropSchema": "hare.migrations.operations",
    "HareOperation": "hare.migrations.operations",
    "Migration": "hare.migrations.migration",
    "MigrationRecorder": "hare.migrations.loading.recorder.migration_recorder",
    "MigrationRunner": "hare.migrations.execution.migration_runner",
    "MigrationWriter": "hare.migrations.writer.migration_writer",
    "Operation": "hare.migrations.operations",
    "OperationEffect": "hare.migrations.reports.operation_effect",
    "OperationPlan": "hare.migrations.reports.operation_plan",
    "RemoveConstraint": "hare.migrations.operations",
    "RemoveField": "hare.migrations.operations",
    "RemoveIndex": "hare.migrations.operations",
    "RemoveTrigger": "hare.migrations.operations",
    "RenameConstraint": "hare.migrations.operations",
    "RenameField": "hare.migrations.operations",
    "RenameIndex": "hare.migrations.operations",
    "RenameModel": "hare.migrations.operations",
    "RenameTrigger": "hare.migrations.operations",
    "RunPython": "hare.migrations.operations",
    "RunSQL": "hare.migrations.operations",
    "SQLOperation": "hare.migrations.operations",
    "State": "hare.migrations.state.state",
}
