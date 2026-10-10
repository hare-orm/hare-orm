from __future__ import annotations

#: The inner class a request query declares its options in.
META_CLASS_NAME = "Meta"

#: The key a request query class's checked declaration is kept under in the class's bucket.
DECLARATION_BUCKET_KEY = "declaration"

#: The key a request query class's options that add parameters are kept under in the class's bucket.
REQUEST_OPTIONS_BUCKET_KEY = "request_options"

#: The key the names of a request query class's parameters taking every value of a repeated query
#: parameter are kept under in the class's bucket.
MANY_VALUE_PARAMETERS_BUCKET_KEY = "many_value_parameters"

#: The constructor argument a request query receives the request it was built from as.
REQUEST_ARGUMENT_NAME = "request"


#: The prefix of a descending ordering name.
DESCENDING_PREFIX = "-"

#: What joins a field path and its lookup in a filter key (``published_at__gte``).
LOOKUP_SEPARATOR = "__"


#: The ``Meta`` options that add parameters to a request query, in the order their parameters
#: are added.
PARAMETER_OPTION_NAMES = ("search", "ordering", "fields", "include", "deleted", "pagination")


#: The annotation ``count_by()`` counts the rows of each value into - named so no model field
#: collides with it.
ROW_COUNT_ANNOTATION = "request_query_row_count"

#: The SQL counting the rows of a group - every dialect has it, and it counts a model without a
#: primary key too.
ROW_COUNT_SQL = "COUNT(*)"

#: The generic containers a list-shaped value may be annotated with.
LIST_CONTAINERS = (list, set, frozenset, tuple)
