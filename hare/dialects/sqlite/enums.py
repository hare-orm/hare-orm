from __future__ import annotations

from enum import StrEnum


class SqliteRegexMatching(StrEnum):
    POSIX_REGEX = " REGEXP "
    IPOSIX_REGEX = " MATCH "


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
