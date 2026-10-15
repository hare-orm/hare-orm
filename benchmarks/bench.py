"""hare-orm's comparative benchmark: the same scenarios on hare-orm and other ORMs, on PostgreSQL, SQLite
and ClickHouse - reads, relations, aggregates, writes, transactions, a cold start and concurrent load.

Commands:
    python benchmarks/bench.py all                every target once, then the report (--runs N: N each)
    python benchmarks/bench.py run hare-rust      one run of one target, printed
    python benchmarks/bench.py charts             the charts and pages again, from results.json

``all`` runs every target in a process of its own, run after run in turn, writes the medians and every
run's numbers to docs/assets/benchmarks/results.json, draws the charts and rewrites every text about
the benchmark from them: the docs' benchmark pages, the Benchmarks section of both READMEs and the
scenario and method lists of benchmarks/README.md. ``charts`` redoes all of that from results.json
without measuring. See benchmarks/README.md for the setup.
"""

from __future__ import annotations

import sys
from pathlib import Path

# The benchmark's own package sits next to this script, outside hare-orm's.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orm_benchmark.commands.benchmark_commands import BenchmarkCommands  # noqa: E402

if __name__ == "__main__":
    BenchmarkCommands.main()
