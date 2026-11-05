from __future__ import annotations

from hare.query.enums import Lookup

#: The inner class a request query declares its options in.
META_CLASS_NAME = "Meta"

#: The key of the completed parameter schema and checked declaration in the class's bucket.
GENERATION_BUCKET_KEY = "generation"

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


#: The lookups an empty text matches every value of - a filter given one filters nothing, as a blank
#: search does.
EMPTY_TEXT_MATCHING_LOOKUPS = frozenset(
    {
        Lookup.CONTAINS,
        Lookup.ICONTAINS,
        Lookup.STARTSWITH,
        Lookup.ISTARTSWITH,
        Lookup.ENDSWITH,
        Lookup.IENDSWITH,
        Lookup.POSIX_REGEX,
        Lookup.IPOSIX_REGEX,
    }
)


#: The ``Meta`` options that add parameters to a request query, in the order their parameters
#: are added.
PARAMETER_OPTION_NAMES = ("search", "ordering", "fields", "include", "deleted", "pagination")

#: The methods used for every option adding request parameters.
PARAMETER_OPTION_METHODS = ("get_parameter_fields", "get_parameters", "check_request")

#: Methods and attributes read by the declaration builder and request executor. Compatible
#: objects need these members, without inheriting the built-in option classes.
META_OPTION_MEMBERS = {
    "queryset": ((), ("model",)),
    "search": ((*PARAMETER_OPTION_METHODS, "get_filter_key", "get_condition"), ("fields",)),
    "ordering": ((*PARAMETER_OPTION_METHODS, "get_requested"), ("fields", "default")),
    "fields": ((*PARAMETER_OPTION_METHODS, "get_requested_names", "apply"), ("fields",)),
    "include": ((*PARAMETER_OPTION_METHODS, "apply"), ("relations", "parameter")),
    "deleted": ((*PARAMETER_OPTION_METHODS, "get_requested", "apply", "check_model"), ("parameter",)),
    "pagination": ((*PARAMETER_OPTION_METHODS, "check_model", "check_ordering", "get_page"), ()),
    "versions": (("check_model", "get_condition"), ()),
}


#: The annotation ``count_by()`` counts the rows of each value into - named so no model field
#: collides with it.
ROW_COUNT_ANNOTATION = "request_query_row_count"

#: The generic containers a list-shaped value may be annotated with.
LIST_CONTAINERS = (list, set, frozenset, tuple)
#: The type of the error of a request value a field refuses.
INVALID_VALUE_ERROR_TYPE = "value_error"
#: The type of the error of a request changing rows that filters none.
MISSING_FILTER_ERROR_TYPE = "missing_filter"
