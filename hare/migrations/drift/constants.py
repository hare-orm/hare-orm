from __future__ import annotations

from hare.ddl.enums import SchemaPartType
from hare.migrations.operations import (
    AddDictionary,
    AddFunction,
    AddMaterializedView,
    AddPolicy,
    AddSequence,
    AddTrigger,
    AddView,
    AlterDictionary,
    AlterFunction,
    AlterMaterializedView,
    AlterPolicy,
    AlterSequence,
    AlterTrigger,
    AlterView,
    RemoveDictionary,
    RemoveFunction,
    RemoveMaterializedView,
    RemovePolicy,
    RemoveSequence,
    RemoveTrigger,
    RemoveView,
)
from hare.models.enums import ModelOption

#: The objects a model declares by name, compared part by part: the ``ModelBinding`` attribute listing them,
#: and the operations adding, removing and altering it in a repair.
DRIFT_NAMED_OBJECT_OPERATIONS = {
    SchemaPartType.TRIGGER: ("triggers", AddTrigger, RemoveTrigger, AlterTrigger),
    SchemaPartType.POLICY: ("policies", AddPolicy, RemovePolicy, AlterPolicy),
    SchemaPartType.SEQUENCE: ("sequences", AddSequence, RemoveSequence, AlterSequence),
    SchemaPartType.VIEW: ("views", AddView, RemoveView, AlterView),
    SchemaPartType.MATERIALIZED_VIEW: (
        "materialized_views",
        AddMaterializedView,
        RemoveMaterializedView,
        AlterMaterializedView,
    ),
    SchemaPartType.FUNCTION: ("functions", AddFunction, RemoveFunction, AlterFunction),
    SchemaPartType.DICTIONARY: ("dictionaries", AddDictionary, RemoveDictionary, AlterDictionary),
}

#: The objects of a schema a model declares, whose definitions are read by name.
DRIFT_SCHEMA_OBJECT_PART_TYPES = (
    SchemaPartType.SEQUENCE,
    SchemaPartType.VIEW,
    SchemaPartType.MATERIALIZED_VIEW,
    SchemaPartType.FUNCTION,
    SchemaPartType.DICTIONARY,
)
#: What an unmanaged model declares beside its table that the shadow leaves out - the shadow holds its
#: table only for the relations pointing at it, and the table of a model over a view takes no INSTEAD OF
#: trigger, nor anything else made for the view.
DRIFT_UNMANAGED_LEFT_OUT_OPTIONS = frozenset(
    {
        ModelOption.TRIGGERS,
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
