from __future__ import annotations

from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance

#: The collation providers CreateCollation takes.
COLLATION_PROVIDERS = frozenset({"libc", "icu"})

DIRECT_RELATION_FIELDS = (
    ForeignKeyFieldInstance,
    ManyToManyFieldInstance,
    OneToOneFieldInstance,
)

#: The error of an operation naming a model the migrations before it don't create.
MISSING_MODEL_MESSAGE = "Model {app_label}.{model_name} is not present - no migration before this one creates it"

#: The error of an operation naming a model by a name no model class can have.
UNUSABLE_MODEL_NAME_MESSAGE = "{operation}: {name!r} can't name a model - it is empty or holds a null byte"
