from __future__ import annotations

import re

from hare.query.plans.enums import PlanPartType

#: The slots of a query its copies don't take over: the object a query made again for each
#: description or build stands for (``PlanOrigins``) - set where it is made, never carried by a clone -
#: and the weak reference slot of a relation object.
UNCOPIED_QUERY_SLOTS = frozenset({"_plan_origin", "__weakref__"})

#: Characters and sequences an annotation, alias or aggregate name may not contain - quotes,
#: brackets, a semicolon, whitespace or an SQL comment, like Django's own check.
FORBIDDEN_ANNOTATION_NAME_PATTERN = re.compile(r"['`\"\]\[;\s]|--|/\*|\*/")

#: The error of ``latest()``/``earliest()`` given no field on a model without ``Meta.get_latest_by``.
LATEST_WITHOUT_FIELDS_MESSAGE = (
    "{method}() needs the fields to order by, as arguments or as the model's Meta.get_latest_by"
)

#: The alias of the truncated value ``dates()``/``datetimes()`` read.
DATES_VALUE_ALIAS = "hare_dates_value"

#: The units ``dates()`` truncates to, and those ``datetimes()`` does.
DATES_TRUNC_TYPES = ("year", "quarter", "month", "week", "day")

DATETIMES_TRUNC_TYPES = ("year", "quarter", "month", "week", "day", "hour", "minute", "second")

#: The orders ``dates()``/``datetimes()`` return their values in.
DATES_ORDERS = ("ASC", "DESC")

#: The error of ``aggregate()`` over the groups of a ``Rollup``/``Cube``/``GroupingSets`` - a row of
#: each grouping holds subtotals of the others' rows, so summed again they count rows more than once.
AGGREGATE_OVER_GROUPING_SETS_MESSAGE = (
    "aggregate() over the groups of {grouping_set!r} would count rows once per grouping - aggregate the plain "
    "groups, or read the total row of the groupings"
)

#: The largest ``sample(percent)`` - every row.
MAX_TABLE_SAMPLE_PERCENT = 100

#: The largest ``sample(seed=...)`` - a seed is an int in ``0..MAX_TABLE_SAMPLE_SEED``.
MAX_TABLE_SAMPLE_SEED = 2_147_483_647

#: The ``order_by()`` name ordering the rows at random.
RANDOM_ORDERING = "?"

#: The alias of the random number ``order_by("?")`` orders by - never selected.
RANDOM_ORDERING_ALIAS = "hare_random_ordering"

#: How many parsed ``order_by()`` calls of plain field names are kept per model.
PLAIN_ORDERINGS_CACHE_SIZE = 256

#: How many relation classes made for a manager's own queryset class are kept.
RELATION_CLASS_CACHE_SIZE = 256

#: The plan parts of an expression holding the expressions it is built of, one or a sequence -
#: what ``RowMultiplication`` walks looking for an aggregate.
EXPRESSION_HOLDING_PLAN_PARTS = frozenset(
    {
        PlanPartType.EXPRESSION,
        PlanPartType.EXPRESSIONS,
        PlanPartType.ARGUMENT,
        PlanPartType.ARGUMENTS,
        PlanPartType.ENCODED_ARGUMENT,
        PlanPartType.FIELD,
        PlanPartType.FIELDS,
    }
)
