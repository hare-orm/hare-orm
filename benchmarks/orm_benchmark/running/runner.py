from __future__ import annotations

import importlib
import json
import os
import platform
import random
import subprocess
import sys
import tempfile
from dataclasses import asdict
from importlib import metadata
from pathlib import Path
from typing import Any, ClassVar

from orm_benchmark.constants import (
    BENCHMARK_DIRECTORY,
    BENCHMARK_SEED,
    ENVIRONMENT_DIRECTORY_PREFIX,
    REPOSITORY,
    SIZES,
)
from orm_benchmark.definitions.benchmark_report import BenchmarkReport
from orm_benchmark.definitions.target import Target
from orm_benchmark.servers.clickhouse_database import ClickhouseDatabase
from orm_benchmark.servers.postgresql_database import PostgresqlDatabase
from orm_benchmark.servers.sqlite_database import SqliteDatabase


class Runner:
    """Runs the benchmark: one run of one target, or every target several times."""

    #: Each suite's class by ``Target.suite`` - imported only by the run that needs it, as a target's own
    #: environment holds only its own ORM.
    SUITES: ClassVar[dict[str, str]] = {
        "active_record": "orm_benchmark.suites.active_record.active_record_suite:ActiveRecordSuite",
        "django": "orm_benchmark.suites.django_orm.django_suite:DjangoSuite",
        "sqlalchemy": "orm_benchmark.suites.sqlalchemy.sqlalchemy_suite:SqlAlchemySuite",
        "hare_clickhouse": "orm_benchmark.suites.clickhouse.hare_clickhouse_suite:HareClickhouseSuite",
        "sqlalchemy_clickhouse": (
            "orm_benchmark.suites.clickhouse.sqlalchemy_clickhouse_suite:SqlAlchemyClickhouseSuite"
        ),
        "django_clickhouse": "orm_benchmark.suites.clickhouse.django_clickhouse_suite:DjangoClickhouseSuite",
    }
    #: The drivers whose versions a database's runs record.
    DRIVERS: ClassVar[dict[str, tuple[str, ...]]] = {
        "postgresql": ("asyncpg", "psycopg"),
        "sqlite": ("aiosqlite",),
        "clickhouse": ("clickhouse-connect", "asynch", "clickhouse-driver"),
    }

    @classmethod
    def get_environment(cls) -> dict[str, Any]:
        """The machine and Python a run measures on."""
        return {
            "os": f"{platform.system()} {platform.release()}",
            "machine": platform.machine(),
            "processor": cls.get_processor_name(),
            "cpu_count": os.cpu_count(),
            "python": platform.python_version(),
        }

    @staticmethod
    def get_processor_name() -> str:
        """The processor's marketing name ("Intel Core i7-..."), where the system tells it."""
        try:
            if sys.platform == "win32":
                import winreg

                with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
                ) as key:
                    return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
            if sys.platform == "darwin":
                return subprocess.run(
                    ["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, check=True
                ).stdout.strip()
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except (OSError, subprocess.CalledProcessError):
            pass
        return platform.processor() or platform.machine()

    @staticmethod
    def get_version(distribution: str) -> str | None:
        """The installed version of a package, None without it."""
        try:
            return metadata.version(distribution)
        except metadata.PackageNotFoundError:
            return None

    @staticmethod
    def get_database(target: Target) -> PostgresqlDatabase | SqliteDatabase | ClickhouseDatabase:
        """The throwaway database of a target's run."""
        name = f"hare_bench_{target.key.replace('-', '_')}"
        if target.database == "postgresql":
            return PostgresqlDatabase(name)
        if target.database == "sqlite":
            return SqliteDatabase(name)
        return ClickhouseDatabase(name)

    @classmethod
    def get_suite(cls, target: Target, database: Any) -> Any:
        module_name, class_name = cls.SUITES[target.suite].split(":")
        return getattr(importlib.import_module(module_name), class_name)(target, database)

    @classmethod
    async def run_once(cls, target: Target, size: str, seed: int = BENCHMARK_SEED) -> dict[str, Any]:
        """One run of one target.

        Args:
            target: The target.
            size: ``small`` or ``large``.

        Returns:
            The run's results, versions and environment.
        """
        random.seed(seed)
        database = cls.get_database(target)
        server_version = None
        results = {}
        error = None
        try:
            server_version = await database.create()
            suite = cls.get_suite(target, database)
            suite.seed = seed
            measured = await suite.run(SIZES[size])
            results = {
                name: {"status": "unsupported"} if value is None else {"status": "measured", **asdict(value)}
                for name, value in measured.items()
            }
        except Exception as failure:
            error = f"{type(failure).__name__}: {failure}"
        finally:
            if database.created:
                try:
                    await database.drop()
                except Exception as failure:
                    error = f"{error + '; ' if error else ''}cleanup {type(failure).__name__}: {failure}"
        drivers = {name: version for name in cls.DRIVERS[target.database] if (version := cls.get_version(name))}
        return {
            "target": target.key,
            "size": size,
            "seed": seed,
            "status": "failed" if error else "measured",
            "error": error,
            "version": cls.get_version(target.distribution),
            "drivers": drivers,
            "server": server_version,
            "environment": cls.get_environment(),
            "results": results,
        }

    @staticmethod
    def get_interpreter(target: Target) -> str:
        """The Python a target runs on: its own environment's, or this one."""
        if target.environment is None:
            return sys.executable
        directory = REPOSITORY / f"{ENVIRONMENT_DIRECTORY_PREFIX}{target.environment}"
        interpreter = directory / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        if not interpreter.exists():
            raise SystemExit(f"{target.key} runs in {directory} - create it as benchmarks/README.md says")
        return str(interpreter)

    @classmethod
    def runs_here(cls, target: Target) -> bool:
        """Whether this process's Python is the one a target runs on."""
        return Path(cls.get_interpreter(target)).resolve() == Path(sys.executable).resolve()

    @classmethod
    def run_all(cls, targets: list[Target], runs: int, size: str, seed: int = BENCHMARK_SEED) -> dict[str, Any]:
        """Every target ``runs`` times, each run in a process of its own - run 1 of every target, then
        run 2 of every target, and so on, so a slow moment of the machine doesn't land on one target.

        Args:
            targets: The targets.
            runs: How many runs each gets.
            size: ``small`` or ``large``.

        Returns:
            The report: by database, the medians and every run's numbers, with the versions and the
            machine.
        """
        by_target: dict[str, list[dict[str, Any]]] = {target.key: [] for target in targets}
        with tempfile.TemporaryDirectory() as run_directory:
            for run_number in range(1, runs + 1):
                round_order = list(targets)
                random.Random(seed + run_number - 1).shuffle(round_order)
                for target in round_order:
                    output = Path(run_directory) / f"{target.key}-{run_number}.json"
                    print(f"run {run_number}/{runs}: {target.key}", flush=True)
                    process = subprocess.run(
                        [
                            cls.get_interpreter(target),
                            str(BENCHMARK_DIRECTORY / "bench.py"),
                            "run",
                            target.key,
                            "--size",
                            size,
                            "--seed",
                            str(seed + run_number - 1),
                            "--out",
                            str(output),
                        ],
                        check=False,
                    )
                    if not output.is_file():
                        raise RuntimeError(f"{target.key} exited {process.returncode} without a benchmark report")
                    result = json.loads(output.read_text(encoding="utf-8"))
                    if process.returncode and result["status"] != "failed":
                        result.update(status="failed", error=f"Benchmark process exited {process.returncode}")
                        result["results"] = {}
                    by_target[target.key].append(result)
        return BenchmarkReport.collect(targets, by_target, size, seed, cls.get_version("hare-orm")).data
