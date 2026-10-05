<!-- Written by benchmarks/bench.py (`all` or `charts`): edit the text there, not here. -->

# Benchmarks

hare-orm against SQLAlchemy, tortoise-orm, yara-orm, Django and clickhouse-connect on PostgreSQL,
SQLite and ClickHouse: the same scenarios in every ORM, written the way its documentation writes
them, plus a concurrent load test. Each database has a page of its own with every scenario.

## <a id="summary"></a>Summary

How many times slower than hare-orm each ORM is on each database, on average: the ratio of geometric
means of the times over every scenario both run, except the cold start. hare-orm stands for its
fastest driver of the database; a dash marks a database the ORM doesn't run on. The last row is
hare-orm's operations per second in the load test, measured warm: every pool connection has run
every operation of the load before the timing starts.

![Times slower than hare-orm, by database](../assets/benchmarks/scoreboard-en-light.svg#gh-light-mode-only)
![Times slower than hare-orm, by database](../assets/benchmarks/scoreboard-en-dark.svg#gh-dark-mode-only)

## <a id="by-database"></a>By database

- [PostgreSQL](postgresql.md) — 50 scenarios on a table of 1,000 widgets, each with a gadget, a tag
  and a JSON document.
- [SQLite](sqlite.md) — 49 scenarios on a table of 1,000 widgets, each with a gadget, a tag and a
  JSON document.
- [ClickHouse](clickhouse.md) — 12 scenarios on a table of 1,000,000 events, sorted by their key.

## <a id="how-it-was-measured"></a>How it was measured

| | |
|---|---|
| Date | 2026-10-09 |
| Machine | Windows 11, 13th Gen Intel(R) Core(TM) i5-13400, 16 CPU |
| Python | 3.14.3 |
| hare-orm commit | `a4013db4` |
| Runs | 3, median |

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
