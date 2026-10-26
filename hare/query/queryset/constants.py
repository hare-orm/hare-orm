from __future__ import annotations

import re

from hare.sql.constants import IDENTIFIER_LENGTH_LIMIT

#: The slots of a query its copies don't take over: the object a query made again for each
#: description or build stands for (``PlanOrigins``) - set where it is made, never carried by a clone -
#: and the weak reference slot of a relation object.
UNCOPIED_QUERY_SLOTS = frozenset({"_plan_origin", "__weakref__"})

#: The most calls a queryset's call signature records - a longer chain is built as it goes: each call
#: would copy the whole signature, making a chain of thousands of calls quadratic.
MAX_CALL_SIGNATURE_LENGTH = 256

#: A write called with a method of a dialect that changes how rows are read.
WRITE_REFUSED_EXTENSION_MESSAGE = (
    "{method_name}() changes how rows are read - an UPDATE or DELETE matches its rows by its condition alone"
)

#: Characters and sequences an annotation, alias or aggregate name may not contain - quotes,
#: brackets, a semicolon, whitespace or an SQL comment, like Django's own check.
FORBIDDEN_ANNOTATION_NAME_PATTERN = re.compile(r"['`\"\]\[;\s]|--|/\*|\*/")
#: The length of an annotation name no shorter than which its bytes are counted - one of fewer
#: characters, four bytes each at most, fits ``IDENTIFIER_LENGTH_LIMIT``.
ANNOTATION_NAME_COUNTED_LENGTH = IDENTIFIER_LENGTH_LIMIT // 4 + 1

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
#: The start of the alias an expression ``order_by()`` orders by is selected under - never selected,
#: followed by its place among the orderings.
EXPRESSION_ORDERING_ALIAS_PREFIX = "hare_expression_ordering_"

#: How many parsed ``order_by()`` calls of plain field names are kept per model.
PLAIN_ORDERINGS_CACHE_SIZE = 256

#: How many relation classes made for a manager's own queryset class are kept.
RELATION_CLASS_CACHE_SIZE = 256

#: The largest limit or offset - a signed 64-bit count, the most every database takes.
MAX_ROW_COUNT = 2**63 - 1

#: The refusal of a summary - count(), exists(), aggregate() - of a select_for_update() queryset.
LOCKED_SUMMARY_MESSAGE = (
    "{method_name}() of a select_for_update() queryset locks no row - SQL locks none it summarizes (FOR UPDATE "
    "is not allowed with aggregate functions), and leaving the lock out would let a caller believe the rows "
    "are locked. Lock the rows by reading them, e.g. len(await queryset), or summarize a queryset without "
    "select_for_update()."
)

#: How a group_by() name of an aggregate annotation is refused.
AGGREGATE_GROUP_NAME_MESSAGE = (
    "Cannot group by {name!r} - it is an aggregate annotation, computed from the groups themselves. Group by a "
    "field or a non-aggregate annotation instead."
)
