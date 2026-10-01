from enum import StrEnum


class TsWeight(StrEnum):
    """A Postgres tsquery/tsvector weight label."""

    A = "A"
    B = "B"
    C = "C"
    D = "D"


class SearchType(StrEnum):
    """Which Postgres tsquery-building function `SearchQuery` uses."""

    PLAIN = "plain"
    PHRASE = "phrase"
    RAW = "raw"
    WEBSEARCH = "websearch"
