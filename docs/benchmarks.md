<!-- Written by benchmarks/bench.py (`all` or `charts`): edit the text there, not here. -->

# Benchmarks

hare-orm against SQLAlchemy, tortoise-orm, yara-orm and Django on the same 32 scenarios — reads,
relations, aggregates, writes, transactions and a cold start — plus a concurrent load test, against
one PostgreSQL server. A shorter bar is faster, except in the load test, which counts operations per
second.

## Summary

How many times slower than hare-orm on asyncpg (its faster driver in this run) each ORM is, on
average. The average is the ratio of geometric means of the times over every scenario except the
cold start. hare-orm on the Rust driver is compared with it too.

![Times slower than hare-orm, on average](assets/benchmarks/summary-slower-en-light.svg#gh-light-mode-only)
![Times slower than hare-orm, on average](assets/benchmarks/summary-slower-en-dark.svg#gh-dark-mode-only)

The load test: 50 workers run 1000 operations on one connection pool: 80% `get()`, 15% a filtered
read, 5% `get()` + `save()`. Operations per second:

![Operations per second under load](assets/benchmarks/summary-load-en-light.svg#gh-light-mode-only)
![Operations per second under load](assets/benchmarks/summary-load-en-dark.svg#gh-dark-mode-only)

## By scenario

Each scenario is drawn on its own scale: compare the bars within one scenario, not across scenarios.
The fastest time is in bold. A bar is the median of 5 runs, and the thin line over it spans the best
run to the worst.

### Reads

![Reads](assets/benchmarks/reads-en-light.svg#gh-light-mode-only)
![Reads](assets/benchmarks/reads-en-dark.svg#gh-dark-mode-only)

### Relations

![Relations](assets/benchmarks/relations-en-light.svg#gh-light-mode-only)
![Relations](assets/benchmarks/relations-en-dark.svg#gh-dark-mode-only)

### Aggregates

![Aggregates](assets/benchmarks/aggregates-en-light.svg#gh-light-mode-only)
![Aggregates](assets/benchmarks/aggregates-en-dark.svg#gh-dark-mode-only)

### Writes

![Writes](assets/benchmarks/writes-en-light.svg#gh-light-mode-only)
![Writes](assets/benchmarks/writes-en-dark.svg#gh-dark-mode-only)

### Transactions, concurrency, start

![Transactions, concurrency, start](assets/benchmarks/transactions-en-light.svg#gh-light-mode-only)
![Transactions, concurrency, start](assets/benchmarks/transactions-en-dark.svg#gh-dark-mode-only)

## Where and how it was measured

| | |
|---|---|
| Date | 2026-10-03 |
| Machine | Windows 11, 13th Gen Intel(R) Core(TM) i5-13400, 16 CPU |
| Python | 3.14.3 |
| PostgreSQL | 18.6 (Debian 18.6-1.pgdg13+2) |
| asyncpg | 0.31.0 |
| hare-orm | 0.9.0 |
| SQLAlchemy | 2.1.1 |
| tortoise-orm | 1.1.8 |
| yara-orm | 1.17.0 |
| Django | 6.1.1 |
| hare-orm commit | `70ed3cd` |
| Runs | 5, median |
| Rows in the table | 1000 |

- Every ORM gets a pool of 50 connections, all opened before anything is timed.
- Within a run, a scenario is repeated 3–5 times and its fastest repetition counts; the deletes and
  the many-to-many `add()` change the data for good and run once.
- Each ORM runs in a process of its own. The runs go round after round, the ORMs in a shuffled order
  each round; a scenario's result is the median over the runs.
- SQLAlchemy runs twice: with a transaction around each session (its default) and with the engine in
  autocommit mode, where every statement commits on its own, as in hare and Django.
- Every scenario is written the way that ORM's documentation writes it.

The benchmark is
[`benchmarks/bench.py`](https://github.com/hare-orm/hare-orm/blob/dev/benchmarks/bench.py) in the
repository; its [README](https://github.com/hare-orm/hare-orm/blob/dev/benchmarks/README.md) lists
every scenario and how to run it on your own machine.
