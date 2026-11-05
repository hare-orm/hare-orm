from __future__ import annotations

import math
import statistics
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from orm_benchmark.constants import (
    BASELINE_DISTRIBUTION,
    BENCHMARK_REPORT_VERSION,
    FAMILIES,
    LOAD_CONCURRENCY,
    LOAD_OPERATIONS,
    LOAD_TESTS,
    LOAD_WARM_UP_ROUNDS,
    POOL_MAX_SIZE,
    RUSSIAN_NAME_PARTS,
    SIZES,
)
from orm_benchmark.declarations.databases import DATABASES
from orm_benchmark.definitions.database import Database
from orm_benchmark.definitions.scenario import Scenario
from orm_benchmark.definitions.scenario_group import ScenarioGroup
from orm_benchmark.definitions.target import Target
from orm_benchmark.measuring.workload import Workload


class BenchmarkReport:
    """A versioned snapshot and its numerical comparisons, independent of renderers.

    Loading a report never consults current targets, workload sizes or scenario
    declarations. Those are captured when collecting results, including failed and
    unmeasured cells. A comparison exists only for positive measured values shared
    by both targets; missing baselines cannot produce an artificial ratio of one.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        if data.get("schema_version") != BENCHMARK_REPORT_VERSION:
            raise ValueError("This report has no supported benchmark snapshot; collect a new report before rendering")
        self.data = data
        self.conditions = data["conditions"]
        self.targets = {item["key"]: Target(**item) for item in data["definitions"]["targets"]}
        self.databases = {}
        for item in data["definitions"]["databases"]:
            values = dict(item)
            values["excluded_scenarios"] = frozenset(item["excluded_scenarios"])
            values["groups"] = tuple(
                ScenarioGroup(
                    group["key"],
                    group["english"],
                    group["russian"],
                    tuple(Scenario(**scenario) for scenario in group["scenarios"]),
                )
                for group in item["groups"]
            )
            self.databases[item["key"]] = Database(**values)

    @classmethod
    def collect(
        cls, targets: list[Target], runs: dict[str, list[dict[str, Any]]], size: str, seed: int, release: str | None
    ) -> BenchmarkReport:
        """Capture declarations and aggregate process summaries while retaining every sample."""
        first_run = next(iter(runs.values()))[0]
        databases = [database for database in DATABASES if any(target.database == database.key for target in targets)]
        descriptions = []
        for database in databases:
            description = asdict(database)
            description["excluded_scenarios"] = sorted(database.excluded_scenarios)
            descriptions.append(description)
        counts = Workload.get_counts(SIZES[size])
        data: dict[str, Any] = {
            "schema_version": BENCHMARK_REPORT_VERSION,
            "generated": datetime.now(UTC).strftime("%Y-%m-%d"),
            "size": size,
            "seed": seed,
            "runs": len(next(iter(runs.values()))),
            "environment": first_run["environment"],
            "hare_release": release,
            "definitions": {"databases": descriptions, "targets": [asdict(target) for target in targets]},
            "conditions": {
                "counts": counts,
                "sizes": dict(SIZES),
                "pool_max_size": POOL_MAX_SIZE,
                "load_concurrency": LOAD_CONCURRENCY,
                "load_operations": LOAD_OPERATIONS,
                "load_warmup_rounds": LOAD_WARM_UP_ROUNDS,
                "load_tests": list(LOAD_TESTS),
                "families": list(FAMILIES),
                "baseline_distribution": BASELINE_DISTRIBUTION,
                "russian_name_parts": dict(RUSSIAN_NAME_PARTS),
                "repetition_statistic": "median",
                "process_statistic": "median",
                "cold_start": (
                    "connection initialization and first SELECT 1; imports, model declarations and DDL excluded"
                ),
                "latency": "operation start to completion including pool wait; measured per operation",
            },
            "databases": {},
        }
        for target in targets:
            target_runs = runs[target.key]
            database = next(database for database in databases if database.key == target.database)
            database_data = data["databases"].setdefault(
                database.key,
                {
                    "server": target_runs[0]["server"],
                    "rows": counts["events" if database.key == "clickhouse" else "rows"],
                    "drivers": {},
                    "targets": {},
                },
            )
            database_data["drivers"].update(target_runs[0]["drivers"])
            names = dict.fromkeys(scenario.key for group in database.groups for scenario in group.scenarios)
            for run in target_runs:
                names.update(dict.fromkeys(run["results"]))
            scenarios = {}
            for name in names:
                observations = []
                for run in target_runs:
                    if name in run["results"]:
                        observation = run["results"][name]
                    elif name in database.excluded_scenarios:
                        observation = {"status": "unsupported"}
                    else:
                        observation = {"status": "failed" if run["status"] == "failed" else "not-run"}
                    observations.append(observation)
                statuses = {observation["status"] for observation in observations}
                status = next(iter(statuses)) if len(statuses) == 1 else "failed"
                result = {"status": status, "observations": observations}
                if status == "measured":
                    units = {observation["unit"] for observation in observations}
                    if len(units) != 1:
                        raise ValueError(f"Inconsistent units for {target.key}/{name}: {units}")
                    values = [cls.summarize(observation) for observation in observations]
                    result.update(unit=units.pop(), median=statistics.median(values), runs=values)
                scenarios[name] = result
            database_data["targets"][target.key] = {
                "name": target.name,
                "version": target_runs[0]["version"],
                "python": target_runs[0]["environment"]["python"],
                "scenarios": scenarios,
                "processes": [{key: value for key, value in run.items() if key != "results"} for run in target_runs],
            }
        return cls(data)

    @staticmethod
    def summarize(observation: dict[str, Any]) -> float:
        statistic = statistics.fmean if observation["statistic"] == "mean" else statistics.median
        return float(statistic(observation["samples"]))

    def get_databases(self) -> list[Database]:
        return list(self.databases.values())

    def get_targets(self, database: Database) -> list[Target]:
        return [target for target in self.targets.values() if target.database == database.key]

    @staticmethod
    def get_scenarios(database: Database) -> list[Scenario]:
        return [
            scenario
            for group in database.groups
            for scenario in group.scenarios
            if scenario.key not in database.excluded_scenarios
        ]

    def get_result(self, target: Target, scenario: str) -> dict[str, Any]:
        return self.data["databases"][target.database]["targets"][target.key]["scenarios"].get(
            scenario, {"status": "not-run"}
        )

    def get_median(self, target: Target, scenario: str) -> float | None:
        result = self.get_result(target, scenario)
        return float(result["median"]) if result["status"] == "measured" else None

    def get_latency_scenarios(self, database: Database) -> list[str]:
        return [scenario.key for scenario in self.get_scenarios(database) if scenario.key != "cold_start"]

    def get_ratio(self, target: Target, baseline: Target | None) -> float | None:
        if baseline is None:
            return None
        logarithms = []
        for scenario in self.get_latency_scenarios(self.databases[target.database]):
            value, reference = self.get_median(target, scenario), self.get_median(baseline, scenario)
            if value is not None and reference is not None and value > 0 and reference > 0:
                logarithms.append(math.log(value) - math.log(reference))
        return math.exp(statistics.fmean(logarithms)) if logarithms else None

    def get_geometric_mean_time(self, target: Target) -> float:
        values = [
            value
            for scenario in self.get_latency_scenarios(self.databases[target.database])
            if (value := self.get_median(target, scenario)) is not None and value > 0
        ]
        return statistics.geometric_mean(values) if values else math.inf

    def get_baseline_drivers(self, database: Database) -> list[Target]:
        drivers = [
            target
            for target in self.get_targets(database)
            if target.distribution == self.conditions["baseline_distribution"]
            and math.isfinite(self.get_geometric_mean_time(target))
        ]
        return sorted(drivers, key=self.get_geometric_mean_time)

    def get_baseline(self, database: Database) -> Target | None:
        drivers = self.get_baseline_drivers(database)
        return drivers[0] if drivers else None

    def get_load_tests(self, database: Database) -> list[str]:
        return [
            name
            for name in self.conditions["load_tests"]
            if any(
                self.get_median(target, f"{name}_throughput_ops_per_sec") is not None
                for target in self.get_targets(database)
            )
        ]
