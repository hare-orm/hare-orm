from __future__ import annotations

from hare.inspectdb.introspection.enums import SchemaPartType
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
    RemovePolicy,
    RemoveSequence,
    RemoveTrigger,
)

#: The objects a model declares by name, compared part by part: the ``MetaInfo`` attribute listing them,
#: and the operations adding one the database lacks, removing one it has of its own (None where only the
#: model's objects are read) and altering one it has otherwise.
DRIFT_NAMED_OBJECT_OPERATIONS = {
    SchemaPartType.TRIGGER: ("triggers", AddTrigger, RemoveTrigger, AlterTrigger),
    SchemaPartType.POLICY: ("policies", AddPolicy, RemovePolicy, AlterPolicy),
    SchemaPartType.SEQUENCE: ("sequences", AddSequence, RemoveSequence, AlterSequence),
    SchemaPartType.VIEW: ("views", AddView, None, AlterView),
    SchemaPartType.MATERIALIZED_VIEW: ("materialized_views", AddMaterializedView, None, AlterMaterializedView),
    SchemaPartType.FUNCTION: ("functions", AddFunction, None, AlterFunction),
    SchemaPartType.DICTIONARY: ("dictionaries", AddDictionary, None, AlterDictionary),
}

#: The objects of a schema a model declares, whose definitions are read by name.
DRIFT_SCHEMA_OBJECT_PART_TYPES = (
    SchemaPartType.VIEW,
    SchemaPartType.MATERIALIZED_VIEW,
    SchemaPartType.FUNCTION,
    SchemaPartType.DICTIONARY,
)
