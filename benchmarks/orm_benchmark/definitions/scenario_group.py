from __future__ import annotations

from dataclasses import dataclass

from orm_benchmark.definitions.scenario import Scenario


@dataclass(frozen=True)
class ScenarioGroup:
    """Scenarios drawn on one chart.

    Attributes:
        key: The chart's name and the anchor of its section.
        english: The English title.
        russian: The Russian title.
        scenarios: The scenarios, in chart order.
    """

    key: str
    english: str
    russian: str
    scenarios: tuple[Scenario, ...]
