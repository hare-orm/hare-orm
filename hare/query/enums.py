from __future__ import annotations

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
    LENGTH = "len"
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
    #: A generic foreign key - the object of the branch set, or the branch's name
    #: (``target__type``).
    GENERIC_RELATION = "generic_relation"


class Connector(StrEnum):
    """How a ``Q`` joins its conditions - ``Q.with_connector(Connector.OR, a=1, b=2)``."""

    #: Every condition holds.
    AND = "AND"
    #: Any condition holds.
    OR = "OR"


class RecordedBuildStep(StrEnum):
    """A step of an ``aggregate()`` build that records value references - kept with its plan, so
    a later query lists the values the steps bind."""

    #: ``QueryConditions.get_filters()``: the annotations it resolved, the filters, the keyset boundary.
    FILTERS = "filters"
    #: ``QueryCtes.apply_with_ctes()``: the values of the CTE bodies.
    CTES = "ctes"
    #: The metrics resolved over a ``.group_by()`` queryset's grouped rows: their keys.
    METRICS = "metrics"


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


class GetException(StrEnum):
    """What ``get()`` raises when its exception parameter is left as it is."""

    #: ``DoesNotExist`` for no matching row, ``MultipleObjectsReturned`` for more than one.
    STANDARD = "standard"


class TableSampleMethod(StrEnum):
    """How ``sample()`` picks a table's rows."""

    #: Each row is kept with the given chance - the whole table is read.
    BERNOULLI = "BERNOULLI"
    #: Each storage block is kept with the given chance, with every row in it - faster, rows of one
    #: block come together.
    SYSTEM = "SYSTEM"


class SpecificationSlotPlanRole(StrEnum):
    """How a setting of a query's specification meets the key of the plan the query keeps."""

    #: Written into the SQL text - every plan key holds it, as it is or as its presence.
    KEYED = "keyed"
    #: Holds values a plan binds - the query describes it: its structure keyed, its values bound.
    DESCRIBED = "described"
    #: Chooses the connection - the key holds the connection's dialect and name.
    CONNECTION = "connection"
    #: Shapes how the result is returned, not the SQL text.
    RESULT = "result"
    #: Bookkeeping of the queryset itself - never part of a statement.
    BOOKKEEPING = "bookkeeping"
