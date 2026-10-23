from __future__ import annotations

from hare.migrations.operations import (
    AddConstraint,
    AddField,
    AddIndex,
    AddTrigger,
    AlterField,
    AlterModelSchema,
    AlterModelTable,
    AlterTrigger,
    RemoveField,
    RenameField,
)
from hare.models.enums import ModelOption

#: Options of a new model its own operations add once every table of the migration exists - a view,
#: dictionary, function or policy may read any of them. Its sequences stay with ``CreateModel``: a column
#: default may take from one.
CREATE_MODEL_DEFERRED_OPTIONS = frozenset(
    {
        ModelOption.VIEWS,
        ModelOption.MATERIALIZED_VIEWS,
        ModelOption.DICTIONARIES,
        ModelOption.FUNCTIONS,
        ModelOption.POLICIES,
        ModelOption.GRANTS,
        ModelOption.ROW_LEVEL_SECURITY,
    }
)

#: The operations of a model changing its columns or table - what a model's declared objects may read.
COLUMN_CHANGING_OPERATIONS = (AddField, AlterField, RemoveField, RenameField, AlterModelTable, AlterModelSchema)

#: The operations adding or changing an index, a constraint or a trigger of a model - made after its fields
#: change.
OBJECT_ADDING_OPERATIONS = (AddIndex, AddConstraint, AddTrigger, AlterTrigger)

#: Appended to an automatic M2M through table's name while it's moved aside, so a through model's
#: table (or the other way around) can take over the same name and receive its rows.
MANY_TO_MANY_THROUGH_TABLE_SWAP_SUFFIX = "__swap"

#: Options naming a field of the model - each must name an existing field in every state, so a change
#: of one is applied after the fields it names are added and before the fields it named are removed.
FIELD_NAMING_MODEL_OPTIONS = (
    ModelOption.TENANT_FIELD,
    ModelOption.SOFT_DELETE_FIELD,
    ModelOption.OPTIMISTIC_LOCK_FIELD,
)
