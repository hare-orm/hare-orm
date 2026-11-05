<!-- Written by benchmarks/bench.py (`all` or `charts`): edit the text there, not here. -->

# SQLite

The charts and numbers below are published 0.12.0 measurements from 2026-10-30. The current-methodology
section describes the updated runner; it does not establish that these historical measurements followed
the new rules. This page does not yet contain 0.13.0 results.

hare-orm against SQLAlchemy, tortoise-orm, yara-orm and Django on SQLite: 49 scenarios on a table of
1,000 widgets, each with a gadget, a tag and a JSON document, and a concurrent load test. A shorter
bar is faster, except in the load test, which counts operations per second.

## <a id="summary"></a>Summary

How many times slower than hare-orm on aiosqlite each library is, on average: the ratio of geometric
means of the times over every scenario both run, except the cold start.

![Times slower than hare-orm, on average](../assets/benchmarks/sqlite-summary-slower-en-light.svg#gh-light-mode-only)
![Times slower than hare-orm, on average](../assets/benchmarks/sqlite-summary-slower-en-dark.svg#gh-dark-mode-only)

The load test: 50 workers run 1,000 operations on one connection pool: 80% `get()`, 15% a
filtered read, 5% `get()` + `save()`; the write load runs half `get()` + `save()`. Measured
after an untimed concurrent warmup of every operation in the load.

Operations per second:

![Operations per second](../assets/benchmarks/sqlite-summary-load-test-en-light.svg#gh-light-mode-only)
![Operations per second](../assets/benchmarks/sqlite-summary-load-test-en-dark.svg#gh-dark-mode-only)

Operations per second, the write load:

![Operations per second, the write load](../assets/benchmarks/sqlite-summary-write-load-test-en-light.svg#gh-light-mode-only)
![Operations per second, the write load](../assets/benchmarks/sqlite-summary-write-load-test-en-dark.svg#gh-dark-mode-only)

## <a id="by-scenario"></a>By scenario

Each scenario is drawn on its own scale: compare the bars within one scenario, not across scenarios.
The fastest time is in bold. A bar is the median of 5 runs, and the thin line over it spans the best
run to the worst.

### <a id="reads"></a>Reads

![Reads](../assets/benchmarks/sqlite-reads-en-light.svg#gh-light-mode-only)
![Reads](../assets/benchmarks/sqlite-reads-en-dark.svg#gh-dark-mode-only)

### <a id="relations"></a>Relations

![Relations](../assets/benchmarks/sqlite-relations-en-light.svg#gh-light-mode-only)
![Relations](../assets/benchmarks/sqlite-relations-en-dark.svg#gh-dark-mode-only)

### <a id="aggregates"></a>Aggregates

![Aggregates](../assets/benchmarks/sqlite-aggregates-en-light.svg#gh-light-mode-only)
![Aggregates](../assets/benchmarks/sqlite-aggregates-en-dark.svg#gh-dark-mode-only)

### <a id="writes"></a>Writes

![Writes](../assets/benchmarks/sqlite-writes-en-light.svg#gh-light-mode-only)
![Writes](../assets/benchmarks/sqlite-writes-en-dark.svg#gh-dark-mode-only)

### <a id="transactions"></a>Transactions, concurrency, start

![Transactions, concurrency, start](../assets/benchmarks/sqlite-transactions-en-light.svg#gh-light-mode-only)
![Transactions, concurrency, start](../assets/benchmarks/sqlite-transactions-en-dark.svg#gh-dark-mode-only)

## <a id="where-and-how-it-was-measured"></a>Where and how it was measured

| | |
|---|---|
| Date | 2026-10-30 |
| Machine | Windows 11, 13th Gen Intel(R) Core(TM) i5-13400, 16 CPU |
| Python | 3.14.3 |
| hare-orm release | 0.12.0 |
| Runs | 5, median |
| SQLite | 3.50.4 |
| aiosqlite | 0.22.1 |
| hare-orm | 0.12.0 |
| SQLAlchemy | 2.1.1 |
| tortoise-orm | 1.1.8 |
| yara-orm | 1.17.0 |
| Django | 6.1.1 |
| Rows in the table | 1,000 |

In the historical run, an ordinary scenario used the minimum of 3–5 repetitions within each run;
the chart reports the median of five runs. Individual repetition samples were not retained. Load
latency was estimated as elapsed time × concurrency / operation count, without timing each operation.
Destructive scenarios, cold start and load used separate measurement paths.

## <a id="current-methodology"></a>Current runner methodology

- On PostgreSQL every ORM has a pool limit of 50 connections. The driver determines how many
  connections open; cold start is measured before warmup.
- On PostgreSQL every table gets a `VACUUM (FULL, ANALYZE)`, untimed, before each group of scenarios
  and each load test, and autovacuum is off for them: every group starts from compact tables.
  Otherwise autovacuum clears the dead rows the writes leave at a random moment of the run, and
  whether it has come by decides the query plan — with a parallel scan or without.
- Before a load test, concurrent workers run every operation of the load, untimed. This warms the
  operations without guaranteeing preparation on each physical connection.
- Most scenarios repeat 3–5 times; their median counts and all samples are retained. Before
  each bulk insert or `get_or_create()` repetition, that scenario's rows are cleared outside
  the timer. Sequential inserts accumulate rows until the next group; updates reuse the
  changed rows. Deletes, many-to-many `add()` and the cold start run once.
- Each ORM runs in a process of its own. The runs go round after round, the ORMs in a shuffled order
  each round; a scenario's result is the median over the runs.
- On Windows a run's process turns off its power throttling (EcoQoS): the scheduler otherwise may
  move a process without a window onto a hybrid processor's efficiency cores, where it runs at half
  the speed.
- SQLAlchemy runs twice on PostgreSQL and SQLite: with normal transactional sessions whose write
  scenarios commit explicitly, and in autocommit mode with each statement committed individually.
  Transaction and streaming scenarios use transactional sessions.
- Unsupported scenarios, failures and missing measurements have distinct labels; comparisons use
  only measured scenarios shared by both ORMs.
- The start is the library's init and its first query, timed with the library already imported; it
  excludes table creation and is left out of the average.
- JSON retains raw samples, the seed, conditions and versions. Load latency measures each operation
  from start to completion, including its wait for a connection.
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
[`benchmarks/bench.py`](https://github.com/hare-orm/hare-orm/blob/main/benchmarks/bench.py) in the
repository; its [README](https://github.com/hare-orm/hare-orm/blob/main/benchmarks/README.md) lists
every scenario and how to run it on your own machine.
