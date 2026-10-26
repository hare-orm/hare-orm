from __future__ import annotations

from dataclasses import dataclass

from hare.dialects.base.features import Features


@dataclass(frozen=True, slots=True)
class ClickhouseFeatures(Features):
    """What ClickHouse and its driver support beyond what every dialect is asked - read by ClickHouse's own
    parts.

    Attributes:
        supports_json_type: A JSON value is stored by a type of its own holding its paths typed (a ``JSON``
            type), not as its text - an object alone, its NULL values and empty objects dropped.
    """

    supports_json_type: bool = False
