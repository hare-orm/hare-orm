from __future__ import annotations

#: The collation providers CreateCollation takes.
COLLATION_PROVIDERS = frozenset({"libc", "icu"})

#: The error of an operation naming a model the migrations before it don't create.
MISSING_MODEL_MESSAGE = "Model {app_label}.{model_name} is not present - no migration before this one creates it"

#: The error of an operation naming a model by a name no model class can have.
UNUSABLE_MODEL_NAME_MESSAGE = "{operation}: {name!r} can't name a model - it is empty or holds a null byte"
