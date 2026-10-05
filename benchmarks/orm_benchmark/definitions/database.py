from __future__ import annotations

from dataclasses import dataclass

from orm_benchmark.definitions.scenario_group import ScenarioGroup


@dataclass(frozen=True)
class Database:
    """A database the benchmark runs on, with the scenarios its targets run.

    Attributes:
        key: The name on the command line, in results.json and in the docs page's file name.
        title: The name on the pages and charts.
        groups: The scenario groups, in page order.
        excluded_scenarios: Scenarios of the groups the database has no part of SQL for - left out of
            its pages and summary, never run.
        load_english: The English description of its load test, after "The load test: ".
        load_russian: The Russian one.
    """

    key: str
    title: str
    groups: tuple[ScenarioGroup, ...]
    excluded_scenarios: frozenset[str]
    load_english: str
    load_russian: str
