<!-- Written by benchmarks/bench.py (`all` or `charts`): edit the text there, not here. -->

# ClickHouse

hare-orm against clickhouse-connect, SQLAlchemy and Django on ClickHouse: 12 scenarios on a table of
1,000,000 events, sorted by their key, and a concurrent load test. A shorter bar is faster, except
in the load test, which counts operations per second.

## <a id="summary"></a>Summary

How many times slower than hare-orm on clickhouse-driver over the native TCP protocol (its faster
driver in this run) each library is, on average: the ratio of geometric means of the times over
every scenario both run, except the cold start.

![Times slower than hare-orm, on average](../assets/benchmarks/clickhouse-summary-slower-en-light.svg#gh-light-mode-only)
![Times slower than hare-orm, on average](../assets/benchmarks/clickhouse-summary-slower-en-dark.svg#gh-dark-mode-only)

The load test: 50 workers run 1,000 operations: 80% `get()` by key, 20% a `GROUP BY` of a day.
Measured warm: every pool connection runs every operation of the load before the timing starts, as
in an application that has been running.

Operations per second:

![Operations per second](../assets/benchmarks/clickhouse-summary-load-test-en-light.svg#gh-light-mode-only)
![Operations per second](../assets/benchmarks/clickhouse-summary-load-test-en-dark.svg#gh-dark-mode-only)

## <a id="by-scenario"></a>By scenario

Each scenario is drawn on its own scale: compare the bars within one scenario, not across scenarios.
The fastest time is in bold. A bar is the median of 3 runs, and the thin line over it spans the best
run to the worst.

### <a id="reads"></a>Reads

![Reads](../assets/benchmarks/clickhouse-reads-en-light.svg#gh-light-mode-only)
![Reads](../assets/benchmarks/clickhouse-reads-en-dark.svg#gh-dark-mode-only)

### <a id="writes"></a>Writes and start

![Writes and start](../assets/benchmarks/clickhouse-writes-en-light.svg#gh-light-mode-only)
![Writes and start](../assets/benchmarks/clickhouse-writes-en-dark.svg#gh-dark-mode-only)

## <a id="where-and-how-it-was-measured"></a>Where and how it was measured

| | |
|---|---|
| Date | 2026-10-09 |
| Machine | Windows 11, 13th Gen Intel(R) Core(TM) i5-13400, 16 CPU |
| Python | 3.14.3 |
| hare-orm commit | `a4013db4` |
| Runs | 3, median |
| ClickHouse | 25.8.33.6 |
| asynch | 0.2.5 |
| clickhouse-connect | 1.10.0 |
| clickhouse-driver | 0.2.11 |
| hare-orm | 0.9.0 |
| clickhouse-connect | 1.10.0 |
| clickhouse-sqlalchemy | 0.3.2, Python 3.12.3 |
| django-clickhouse-backend | 2.0 |
| Rows in the table | 1,000,000 |

- On PostgreSQL every ORM gets a pool of 50 connections, all opened before anything is timed.
- Before a load test, every connection of the pool runs every operation of the load, untimed: the
  test measures an application that has been running, not one just started — each connection is open
  and has prepared its statements.
- Within a run, a scenario is repeated 3–5 times and its fastest repetition counts; the rows a write
  scenario puts back are cleared before each repetition, untimed. The deletes and the many-to-many
  `add()` change the data for good and run once.
- Each ORM runs in a process of its own. The runs go round after round, the ORMs in a shuffled order
  each round; a scenario's result is the median over the runs.
- SQLAlchemy runs twice on PostgreSQL and SQLite: with a transaction around each session (its
  default) and with the engine in autocommit mode, where every statement commits on its own, as in
  hare and Django.
- A scenario an ORM has no way to write is marked “not in this ORM” on its chart and left out of its
  average.
- The start is the library's init and its first query, timed with the library already imported; it
  is left out of the average.
- On SQLite every ORM runs on one file the way it does by default; SQLAlchemy's connections wait up
  to 30 s for the file's write lock instead of the default 5 s — its pool of five otherwise stops
  the write load at “database is locked”. `select_for_update()` isn't measured on SQLite, which has
  no row locks.
- The server fills the ClickHouse table before the scenarios; a mutation (`update()`, `delete()`)
  finishes before its call returns in every library. SQLAlchemy runs through clickhouse-sqlalchemy
  0.3.2 with the asynch 0.2.5 driver on SQLAlchemy 2.0.30 and Python 3.12 — its asynchronous driver
  doesn't run on newer versions.
- Every scenario is written the way that ORM's documentation writes it.

The benchmark is
[`benchmarks/bench.py`](https://github.com/hare-orm/hare-orm/blob/dev/benchmarks/bench.py) in the
repository; its [README](https://github.com/hare-orm/hare-orm/blob/dev/benchmarks/README.md) lists
every scenario and how to run it on your own machine.
