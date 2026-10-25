from __future__ import annotations

from dataclasses import dataclass

from hare.dialects.base.features import Features


@dataclass(frozen=True, slots=True)
class SqliteFeatures(Features):
    """What SQLite and its driver support beyond what every dialect is asked - read by SQLite's own parts.

    Attributes:
        supports_unhex: ``unhex()`` decodes hex text to bytes.
        supports_strict_tables: A table is created ``STRICT`` - each column holds values of its declared type
            only.
        supports_spatial_index: A spatial index kept in a table of its own (``SpatialiteIndex``) - registered
            in the spatial metadata.
    """

    supports_unhex: bool = False
    supports_strict_tables: bool = False
    supports_spatial_index: bool = False
