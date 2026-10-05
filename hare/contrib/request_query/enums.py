from __future__ import annotations

from enum import StrEnum


class DeletedRows(StrEnum):
    """Which rows of a soft-deleted model a request asks for."""

    EXCLUDE = "exclude"
    INCLUDE = "include"
    ONLY = "only"


class EachRowOnce(StrEnum):
    """How a query whose conditions join a relation to many rows takes each row once."""

    #: ``SELECT DISTINCT`` - for reading rows and counting them.
    DISTINCT = "distinct"
    #: The rows whose primary key the query selects - for grouping and locking rows.
    BY_KEY = "by_key"
    #: Nothing to do - a delete or an update changes each matching row once anyway.
    NOT_NEEDED = "not_needed"


class ParameterType(StrEnum):
    """What a parameter of a request query does - ``RequestQuery.describe_parameters()``."""

    #: Filters by a ``.filter()`` key.
    FILTER = "filter"
    #: Filters through the class's ``filter_<parameter>`` method.
    FILTER_METHOD = "filter_method"
    #: A parameter the query reads but doesn't filter by (``NoFilter``).
    NO_FILTER = "no_filter"
    #: The text of ``Meta.search``.
    SEARCH = "search"
    #: The names ``Meta.ordering`` orders by.
    ORDERING = "ordering"
    #: The page size of ``Meta.pagination``.
    LIMIT = "limit"
    #: The rows an offset pagination skips.
    OFFSET = "offset"
    #: The cursor of a cursor pagination.
    CURSOR = "cursor"
    #: The fields ``Meta.fields`` loads.
    FIELDS = "fields"
    #: The relations ``Meta.include`` loads.
    INCLUDE = "include"
    #: The soft-deleted rows ``Meta.deleted`` shows.
    DELETED = "deleted"
    #: A parameter of an option of the application's own.
    OPTION = "option"


class BoundSide(StrEnum):
    """Which bound of a field a filter parameter gives - ``RequestQuery.describe_parameters()``."""

    #: A lower bound - ``gt``/``gte``.
    LOWER = "lower"
    #: An upper bound - ``lt``/``lte``.
    UPPER = "upper"
    #: Both bounds as one ``range`` value.
    RANGE = "range"


class CursorDirection(StrEnum):
    """Which way a cursor of a cursor pagination reads from its boundary row."""

    NEXT = "next"
    PREVIOUS = "previous"
