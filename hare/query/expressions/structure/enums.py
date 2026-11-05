from enum import StrEnum


class ArgumentRole(StrEnum):
    VALUE = "value"
    FIELD = "field"
    OUTER_FIELD = "outer_field"
    ORDERING = "ordering"
    FILTERS = "filters"
    FILTER_VALUE = "filter_value"
    FIELD_MAPPING = "field_mapping"
    LOOKUP = "lookup"
