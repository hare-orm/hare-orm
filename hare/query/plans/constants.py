from __future__ import annotations

#: Marks the origin of a value a build may bind nowhere - of a condition folded into JOINs it may not
#: make (``PlanParts.get_optional_origins()``): such a value with no reference binds nothing.
OPTIONAL_VALUE_ORIGIN = "optional"

#: The calls of a queryset's call signature whose filter values a plan binds (``filter()``,
#: ``exclude()``, ``get()`` of a queryset nothing else changed) - the other calls are part of the plan
#: key as they are.
CALL_SIGNATURE_FILTER_CALLS = frozenset({"filter", "exclude", "get", "filter_conditions", "exclude_conditions"})

#: The calls of a call signature adding annotations - each annotation's expression is a value of
#: the call, described by its own description.
CALL_SIGNATURE_ANNOTATION_CALLS = frozenset({"annotate", "alias"})

#: The filter calls of a call signature given ``Q`` conditions too - their count follows the kwargs'
#: keys in the call.
CALL_SIGNATURE_CONDITION_FILTER_CALLS = frozenset({"filter_conditions", "exclude_conditions"})

#: The types of filter values a plan doesn't bind one to one, as given - a None or an ``__isnull``
#: boolean rendered into the SQL text, a list rendering a parameter per item.
CALL_SIGNATURE_DESCRIBED_VALUE_TYPES = frozenset({type(None), bool, list, tuple, set})

#: The query options ``select_for_update()`` sets - a direct ``get()`` takes them in its plan key.
SELECT_FOR_UPDATE_OPTION_NAMES = (
    "select_for_update",
    "select_for_update_nowait",
    "select_for_update_skip_locked",
    "select_for_update_of",
    "select_for_update_strength",
)
