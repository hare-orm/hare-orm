from __future__ import annotations

from enum import StrEnum


class SqliteRegexMatching(StrEnum):
    POSIX_REGEX = " REGEXP "
    IPOSIX_REGEX = " MATCH "


class SqliteRegexSyntax(StrEnum):
    """The syntax a Postgres regular expression is read in - an advanced one unless its embedded
    options or its director say otherwise."""

    #: The advanced syntax - escapes, constraints, lookaround.
    ADVANCED = "advanced"
    #: The extended syntax (``(?e)``) - a backslash takes the next character literally.
    EXTENDED = "extended"
    #: The basic syntax (``(?b)``) - ``\(``, ``\)`` and ``\{`` group and repeat, the bare ones are
    #: literal.
    BASIC = "basic"
    #: Literal text (``(?q)``, ``***=``).
    LITERAL = "literal"


class SpatialiteMetadata(StrEnum):
    """Which spatial metadata hare creates in a database SpatiaLite is loaded into without any -
    ``spatialite_metadata``."""

    #: The WGS84 reference systems only - longitude/latitude 4326 and its UTM zones; created in
    #: milliseconds.
    WGS84 = "WGS84"
    #: Every EPSG reference system SpatiaLite knows.
    FULL = "FULL"
    #: None - no geography and no spatial index.
    NONE = "NONE"


class SqliteConvertedValueType(StrEnum):
    """The type of a column's values a type change converts, named as PostgreSQL names it - the
    casts between them are the ones PostgreSQL makes."""

    INTEGER = "integer"
    BIGINT = "bigint"
    SMALLINT = "smallint"
    DOUBLE_PRECISION = "double precision"
    NUMERIC = "numeric"
    BOOLEAN = "boolean"
    TEXT = "text"
    DATE = "date"
    TIMESTAMP = "timestamp"
    TIME = "time"
