from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.matrix.paths import REPOSITORY_DIRECTORY
from tests.utils.manual_clock import ManualClock

pytestmark = pytest.mark.database_independent


@pytest.fixture(autouse=True)
def benchmark_imports(monkeypatch):
    monkeypatch.syspath_prepend(str(REPOSITORY_DIRECTORY / "benchmarks"))


@pytest.mark.asyncio
async def test_benchmark_retains_samples_and_excludes_preparation_and_warmup(monkeypatch):
    from orm_benchmark.measuring import benchmark_scenario_run

    clock = ManualClock()
    monkeypatch.setattr(benchmark_scenario_run, "time", SimpleNamespace(perf_counter=clock.perf_counter))
    calls = []

    async def prepare():
        calls.append("prepare")
        clock.advance(10)

    async def operation():
        calls.append("operation")
        clock.advance(0.125)

    result = await benchmark_scenario_run.BenchmarkScenarioRun.measure(operation, 3, prepare, warmup=2)
    assert result.samples == (125, 125, 125)
    assert result.warmup_operations == 2
    assert calls == ["prepare", "operation"] * 5


@pytest.mark.asyncio
async def test_load_measures_each_operation_and_drains_workers_on_failure(monkeypatch):
    from orm_benchmark.measuring import load_test

    clock = ManualClock()
    monkeypatch.setattr(load_test, "time", SimpleNamespace(perf_counter=clock.perf_counter))
    monkeypatch.setattr(load_test, "LOAD_WARM_UP_ROUNDS", 0)

    async def operation(index):
        clock.advance(0.25)

    result = await load_test.LoadTest.run("load", operation, 1, 4)
    assert result["load_avg_latency_ms"].samples == (250, 250, 250, 250)
    assert result["load_total_ms"].samples == (1000,)
    started, drained = asyncio.Event(), asyncio.Event()

    async def failure(index):
        if index == 0:
            await started.wait()
            raise ValueError("operation failed")
        try:
            started.set()
            await asyncio.Future()
        finally:
            drained.set()

    with pytest.raises(ExceptionGroup, match="unhandled errors"):
        await load_test.LoadTest.run("load", failure, 2, 2)
    assert drained.is_set()


@pytest.fixture
def benchmark_report():
    from orm_benchmark.declarations.targets import TARGETS
    from orm_benchmark.definitions.benchmark_report import BenchmarkReport

    target = next(target for target in TARGETS if target.database == "sqlite" and target.distribution != "hare-orm")
    run = {
        "status": "measured",
        "size": "small",
        "seed": 42,
        "version": "test",
        "server": "test",
        "drivers": {},
        "environment": {"python": "test", "processor": "test", "machine": "test", "os": "test", "cpu_count": 1},
        "results": {
            "fetch_all": {
                "status": "measured",
                "samples": [1, 3, 20],
                "unit": "ms",
                "statistic": "median",
                "warmup_operations": 0,
                "conditions": {},
            },
            "bulk_create": {"status": "unsupported"},
            "get_by_pk": {"status": "failed"},
        },
    }
    return BenchmarkReport.collect([target], {target.key: [run]}, "small", 42, "test")


def test_report_is_a_snapshot_and_missing_baseline_has_no_ratio(benchmark_report, monkeypatch):
    from orm_benchmark.definitions import benchmark_report as module
    from orm_benchmark.output.chart_writer import ChartWriter

    data = json.loads(json.dumps(benchmark_report.data))
    before = ChartWriter.render_all(data)
    monkeypatch.setattr(module, "DATABASES", ())
    monkeypatch.setattr(module, "SIZES", {"small": 99999})
    restored = module.BenchmarkReport(data)
    target = next(iter(restored.targets.values()))
    database = restored.get_databases()[0]
    assert restored.get_median(target, "fetch_all") == 3
    assert restored.get_result(target, "fetch_all")["observations"][0]["samples"] == [1, 3, 20]
    assert restored.get_result(target, "bulk_create")["status"] == "unsupported"
    assert restored.get_result(target, "get_by_pk")["status"] == "failed"
    assert restored.get_result(target, "first_row")["status"] == "not-run"
    assert restored.get_baseline(database) is None
    assert restored.get_ratio(target, None) is None
    assert ChartWriter.render_all(data) == before


def test_staging_failure_preserves_all_published_artifacts(tmp_path, monkeypatch):
    from orm_benchmark.output.benchmark_artifacts import BenchmarkArtifacts

    first, second = tmp_path / "first.svg", tmp_path / "second.md"
    first.write_text("previous chart")
    second.write_text("previous page")
    write_text = Path.write_text

    def fail_second(path, content, **options):
        if content == "new page":
            raise OSError("staging failed")
        return write_text(path, content, **options)

    monkeypatch.setattr(Path, "write_text", fail_second)
    with pytest.raises(OSError, match="staging failed"):
        BenchmarkArtifacts.publish({first: "new chart", second: "new page"})
    assert first.read_text() == "previous chart"
    assert second.read_text() == "previous page"
