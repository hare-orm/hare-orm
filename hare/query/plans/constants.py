from __future__ import annotations

from hare.query.plans.enums import PlanKeyForm, PlanPartType
from hare.query.plans.statement.declarations import CallPlanLookup

#: Parts whose attribute is a method called for the value.
CALLED_PLAN_PART_TYPES = frozenset(
    {
        PlanPartType.VALUE_METHOD,
        PlanPartType.ARGUMENT_METHOD,
        PlanPartType.EXPRESSION_METHOD,
        PlanPartType.JOIN_CONDITION_METHOD,
        PlanPartType.QUERY_METHOD,
    }
)

#: Parts bound as one literal value.
LITERAL_PLAN_PART_TYPES = frozenset({PlanPartType.LITERAL, PlanPartType.VALUE_METHOD})

#: Parts described as one argument.
ARGUMENT_PLAN_PART_TYPES = frozenset(
    {PlanPartType.ARGUMENT, PlanPartType.ARGUMENT_METHOD, PlanPartType.ENCODED_ARGUMENT}
)

#: Parts described by the expression they hold.
EXPRESSION_PLAN_PART_TYPES = frozenset(
    {PlanPartType.EXPRESSION, PlanPartType.EXPRESSION_METHOD, PlanPartType.JOIN_CONDITION_METHOD}
)

#: Parts holding a sequence of arguments, fields or expressions.
SEQUENCE_PLAN_PART_TYPES = frozenset({PlanPartType.ARGUMENTS, PlanPartType.FIELDS, PlanPartType.EXPRESSIONS})

#: Parts holding a query.
QUERY_PLAN_PART_TYPES = frozenset({PlanPartType.QUERY, PlanPartType.QUERY_METHOD})

#: Parts read after nested descriptions have prepared their structural state.
KEY_ONLY_PLAN_PART_TYPES = frozenset(
    {PlanPartType.KEY, PlanPartType.KEY_METHOD, PlanPartType.PLAN_KEY, PlanPartType.KEYS}
)

#: Query slots that contribute no bound parameters.
KEY_ONLY_PLAN_KEY_FORMS = frozenset(
    {PlanKeyForm.VALUE, PlanKeyForm.TUPLE, PlanKeyForm.SORTED_TUPLE, PlanKeyForm.PRESENCE, PlanKeyForm.METHOD}
)

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

#: The message of a query whose expressions nest deeper than Python's stack lets it describe or
#: build them.
EXPRESSIONS_NESTED_TOO_DEEPLY_MESSAGE = (
    "The query's expressions are nested too deeply to build - write a long sum or chain as a few shorter ones"
)

#: A lookup of a queryset's calls finding no plan and keeping nothing.
NO_CALL_PLAN = CallPlanLookup()
