from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Scenario:
    """One timed scenario, as the charts and the pages label it.

    Attributes:
        key: The name in results.json.
        english: The English label - ``{rows}``, ``{batch}``, ``{in_count}``, ``{large_rows}`` and
            ``{insert_rows}`` are filled with the ``Workload`` counts.
        russian: The Russian label.
    """

    key: str
    english: str
    russian: str
