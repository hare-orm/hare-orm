from __future__ import annotations

from hare.fields.enums import RelationType
from hare.query.enums import Lookup

#: The inner class a request query declares its options in.
META_CLASS_NAME = "Meta"

#: The key a request query class's checked declaration is kept under in the class's bucket.
DECLARATION_BUCKET_KEY = "declaration"

#: The constructor argument a request query receives the request it was built from as.
REQUEST_ARGUMENT_NAME = "request"

#: The prefix of a method that turns a parameter's value into a condition of its own.
FILTER_METHOD_PREFIX = "filter_"

#: What separates the values of a composite key and the items of a comma-separated list in one
#: parameter (``?pk=2,1``, ``?ids=1,2,3``).
VALUE_SEPARATOR = ","

#: What separates the names in a request's ordering, fields and include parameters
#: (``?ordering=-created_at,name``, ``?fields=id,title``, ``?include=author,tags``).
NAME_SEPARATOR = ","

#: The prefix of a descending ordering name.
DESCENDING_PREFIX = "-"

#: What joins a field path and its lookup in a filter key (``published_at__gte``).
LOOKUP_SEPARATOR = "__"

#: The name ``Meta.filters`` may give the exact lookup, which has no suffix of its own - its
#: parameter is the field path itself.
EXACT_LOOKUP_NAME = "exact"

#: The lookups giving a field's lower bound, and its upper bound - a request's pair of them leaving
#: no value between is refused.
LOWER_BOUND_LOOKUPS = frozenset({Lookup.GT, Lookup.GTE})
UPPER_BOUND_LOOKUPS = frozenset({Lookup.LT, Lookup.LTE})

#: The bound lookups excluding the bound's own value.
STRICT_BOUND_LOOKUPS = frozenset({Lookup.GT, Lookup.LT})

#: The relations holding many related rows for one row.
TO_MANY_RELATION_TYPES = frozenset({RelationType.MANY_TO_MANY, RelationType.BACKWARD_FOREIGN_KEY})

#: The relations a row may have no related row of, whatever its own columns hold.
BACKWARD_RELATION_TYPES = frozenset(
    {RelationType.MANY_TO_MANY, RelationType.BACKWARD_FOREIGN_KEY, RelationType.BACKWARD_ONE_TO_ONE}
)

#: The type of the error of bounds leaving no value between them.
BOUNDS_ERROR_TYPE = "range"

#: The lookups comparing a field's own values - a parameter of ``Meta.filters`` with one of them
#: takes the field's enum, when it has one.
ENUM_VALUE_LOOKUPS = frozenset({Lookup.EXACT, Lookup.NOT, Lookup.IN, Lookup.NOT_IN})

#: The page size a pagination returns when the request names none.
DEFAULT_PAGE_LIMIT = 100

#: The largest page size a pagination accepts by default.
DEFAULT_MAX_PAGE_LIMIT = 1000

#: The largest page size a pagination may be configured to accept at all - a sanity ceiling
#: against a typo such as ``max_limit=1000000``.
MAX_PAGE_LIMIT_CEILING = 100_000

#: The default names of the parameters the options of a request query add.
DEFAULT_SEARCH_PARAMETER = "search"
DEFAULT_ORDERING_PARAMETER = "ordering"
DEFAULT_LIMIT_PARAMETER = "limit"
DEFAULT_OFFSET_PARAMETER = "offset"
DEFAULT_CURSOR_PARAMETER = "cursor"
DEFAULT_FIELDS_PARAMETER = "fields"
DEFAULT_INCLUDE_PARAMETER = "include"
DEFAULT_DELETED_PARAMETER = "deleted"

#: The ``Meta`` options that add parameters to a request query, in the order their parameters
#: are added.
PARAMETER_OPTION_NAMES = ("search", "ordering", "fields", "include", "deleted", "pagination")

#: The fields of a ``VersionedModel`` naming a record and its version.
VERSIONED_RECORD_FIELD = "id"
OPTIMISTIC_LOCK_FIELD = "version"

#: The annotation ``count_by()`` counts the rows of each value into - named so no model field
#: collides with it.
ROW_COUNT_ANNOTATION = "request_query_row_count"

#: The SQL counting the rows of a group - every dialect has it, and it counts a model without a
#: primary key too.
ROW_COUNT_SQL = "COUNT(*)"
