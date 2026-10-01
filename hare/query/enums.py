from enum import StrEnum


class Lookup(StrEnum):
    """Lookup suffix of a filter keyword (``name__icontains``)."""

    EXACT = ""
    NOT = "not"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    NOT_IN = "not_in"
    RANGE = "range"
    ISNULL = "isnull"
    NOT_ISNULL = "not_isnull"
    IEXACT = "iexact"
    CONTAINS = "contains"
    ICONTAINS = "icontains"
    STARTSWITH = "startswith"
    ISTARTSWITH = "istartswith"
    ENDSWITH = "endswith"
    IENDSWITH = "iendswith"
    POSIX_REGEX = "posix_regex"
    IPOSIX_REGEX = "iposix_regex"
    SEARCH = "search"
    CONTAINED_BY = "contained_by"
    OVERLAP = "overlap"
    LEN = "len"
    ITEM = "item"
    HAS_KEY = "has_key"
    HAS_KEYS = "has_keys"
    HAS_ANY_KEYS = "has_any_keys"
    FILTER = "filter"


class LookupValueShape(StrEnum):
    """What a lookup's filter value holds: one value, a list, or a two-item range."""

    VALUE = "value"
    LIST = "list"
    RANGE = "range"


class LookupTarget(StrEnum):
    """What a filter key compares at the end of its relations."""

    #: A field's value, or a value read inside it (a date part, an array item, a range bound).
    FIELD = "field"
    #: The value at a key path of a JSON field.
    JSON_PATH = "json_path"
    #: A relation itself - the related row's key, or whether there is one.
    RELATION = "relation"
    #: A composite primary key.
    PRIMARY_KEY = "primary_key"
    #: An annotation's value.
    ANNOTATION = "annotation"


class Connector(StrEnum):
    """How a ``Q`` joins its conditions - ``Q.with_connector(Connector.OR, a=1, b=2)``."""

    #: Every condition holds.
    AND = "AND"
    #: Any condition holds.
    OR = "OR"


class RecordedBuildStep(StrEnum):
    """A step of an ``aggregate()`` build that records value references - kept with its plan, so
    a later query lists its values in the order the build recorded them."""

    #: ``get_filters()``: the annotations it resolved, the filters, the keyset boundary.
    FILTERS = "filters"
    #: ``_apply_with_ctes()``: the values of the CTE bodies.
    CTES = "ctes"


class RowShape(StrEnum):
    """What a row of a queryset is - a model instance, or the values ``.values()``/
    ``.values_list()`` select."""

    MODEL = "model"
    #: ``.values()``: a dict by selected name.
    DICT = "dict"
    #: ``.values_list()``: a tuple of the selected values.
    TUPLE = "tuple"
    #: ``.values_list(flat=True)``: the one selected value alone.
    FLAT = "flat"
    #: ``.values_list(named=True)``: a namedtuple of the selected values.
    NAMED = "named"
