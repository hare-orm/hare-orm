# Benchmarks

[Русский](README.ru.md)

`bench.py` compares hare-orm with SQLAlchemy, tortoise-orm, yara-orm, Django and the clickhouse-connect
driver on the same scenarios, on PostgreSQL, SQLite and ClickHouse. The results and charts are on the
[Benchmarks](https://hare-orm.github.io/hare-orm/benchmarks/) pages of the documentation.

## What is measured

| Database | Target | What runs |
|---|---|---|
| PostgreSQL | `hare-rust` | hare-orm on its Rust PostgreSQL driver (`postgresql://`) |
| | `hare-asyncpg` | hare-orm on asyncpg (`postgresql+asyncpg://`) |
| | `sqlalchemy` | SQLAlchemy 2 async ORM on asyncpg; a session runs its statements in a transaction and commits it — SQLAlchemy's default |
| | `sqlalchemy-autocommit` | the same, the engine in `AUTOCOMMIT`: every statement commits on its own, as in hare and Django; the transaction scenarios still run in a real transaction |
| | `tortoise` | tortoise-orm on asyncpg |
| | `yara-orm` | yara-orm |
| | `django` | Django's async ORM on psycopg 3 with its connection pool |
| SQLite | `hare-sqlite` | hare-orm on aiosqlite |
| | `sqlalchemy-sqlite`, `sqlalchemy-autocommit-sqlite` | SQLAlchemy 2 async ORM on aiosqlite, in both modes |
| | `tortoise-sqlite` | tortoise-orm |
| | `yara-orm-sqlite` | yara-orm |
| | `django-sqlite` | Django's async ORM |
| ClickHouse | `hare-clickhouse` | hare-orm on clickhouse-connect — ClickHouse's HTTP interface |
| | `hare-clickhouse-driver` | hare-orm on clickhouse-driver — ClickHouse's native TCP protocol |
| | `clickhouse-connect` | the clickhouse-connect driver alone, SQL text — the floor an ORM over it builds on |
| | `sqlalchemy-clickhouse` | SQLAlchemy 2.0 async ORM with clickhouse-sqlalchemy on the asynch driver, in an environment of its own |
| | `django-clickhouse` | Django's async ORM with django-clickhouse-backend |

<!-- benchmark-scenarios:start -->
Every scenario has the same name and does the same work in each ORM, written the way that ORM's
documentation writes it (`--size small`: a table of 100 widgets). The scenarios, as the charts label
them:

- **PostgreSQL** — a table of 1,000 widgets, each with a gadget, a tag and a JSON document
  - *Reads*: All 1000 rows; 200 × get() by primary key; 200 × first(); A page, LIMIT/OFFSET; order_by() + the first 10; Filter on two fields; icontains search; exists(); values_list(flat=True); distinct() values of a field; only("id", "name"); OR + JOIN + distinct; id IN (200 values); Iterating all rows in chunks; Reading a JSON field
  - *Relations*: select_related, a JOIN; Filter across a relation; prefetch_related; prefetch_related, many-to-many; Filter across many-to-many; annotate(Count) of a relation; Exists() in annotate; N+1: 200 separate queries; 200 × many-to-many add()
  - *Aggregates*: count(); Sum, Avg, Max, Min; Count with a condition; GROUP BY; HAVING on an aggregate; annotate + values; Case / When; Window: Rank() per category
  - *Writes*: bulk_create, 1000 rows; bulk_create, 10000 rows; 200 × create(); update() by a filter; update() with F(): value + 1; 200 × update() by primary key; 200 × get() + save(); bulk_update, 200 rows; Upsert, 200 rows; 200 × get_or_create(); delete() by a filter; 200 × get() + delete(); Writing a JSON field
  - *Transactions, concurrency, start*: 200 × a transaction: get() + save(); 200 × a nested transaction (savepoint); 200 × select_for_update() in a transaction; 200 × get() at once; Start: init and the first connection
  - *Load*: 50 workers run 1,000 operations on one connection pool: 80% `get()`, 15% a filtered
    read, 5% `get()` + `save()`; the write load runs half `get()` + `save()`.
- **SQLite** — a table of 1,000 widgets, each with a gadget, a tag and a JSON document
  - *Reads*: All 1000 rows; 200 × get() by primary key; 200 × first(); A page, LIMIT/OFFSET; order_by() + the first 10; Filter on two fields; icontains search; exists(); values_list(flat=True); distinct() values of a field; only("id", "name"); OR + JOIN + distinct; id IN (200 values); Iterating all rows in chunks; Reading a JSON field
  - *Relations*: select_related, a JOIN; Filter across a relation; prefetch_related; prefetch_related, many-to-many; Filter across many-to-many; annotate(Count) of a relation; Exists() in annotate; N+1: 200 separate queries; 200 × many-to-many add()
  - *Aggregates*: count(); Sum, Avg, Max, Min; Count with a condition; GROUP BY; HAVING on an aggregate; annotate + values; Case / When; Window: Rank() per category
  - *Writes*: bulk_create, 1000 rows; bulk_create, 10000 rows; 200 × create(); update() by a filter; update() with F(): value + 1; 200 × update() by primary key; 200 × get() + save(); bulk_update, 200 rows; Upsert, 200 rows; 200 × get_or_create(); delete() by a filter; 200 × get() + delete(); Writing a JSON field
  - *Transactions, concurrency, start*: 200 × a transaction: get() + save(); 200 × a nested transaction (savepoint); 200 × get() at once; Start: init and the first connection
  - *Load*: 50 workers run 1,000 operations on one connection pool: 80% `get()`, 15% a filtered
    read, 5% `get()` + `save()`; the write load runs half `get()` + `save()`.
- **ClickHouse** — a table of 1,000,000 events, sorted by their key
  - *Reads*: count() with a filter; GROUP BY + Sum, Avg; The top 10 by a sum; Grouped by day; Count of distinct values; A page of values(); 200 × get() by key; 200 × get() at once
  - *Writes and start*: Inserting 100000 rows; update() by a filter (a mutation); delete() by a filter (a mutation); Start: init and the first connection
  - *Load*: 50 workers run 1,000 operations: 80% `get()` by key, 20% a `GROUP BY` of a day.
<!-- benchmark-scenarios:end -->

## How it is measured

<!-- benchmark-method:start -->
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
<!-- benchmark-method:end -->

`all` runs every target once; `--runs N` runs them N times. A result is then the median, and the charts
draw the spread from the best run to the worst.

## Running it

The servers, at 127.0.0.1: PostgreSQL on port 5433 with the user and password `postgres`
(`make test_db_up` starts one) and ClickHouse with the password `clickhouse`, its HTTP interface on 8124
and its native protocol on 9124 (`make test_clickhouse_up`). Each run creates a database of its own
there, or a SQLite file in the temporary directory, and drops it afterwards.

```sh
python -m venv .bench-venv
.bench-venv/bin/pip install -e . -r benchmarks/requirements.txt   # .bench-venv\Scripts\pip on Windows
.bench-venv/bin/python benchmarks/bench.py all
```

The `sqlalchemy-clickhouse` target runs in an environment of its own, `.bench-venv-clickhouse-sqlalchemy`:
clickhouse-sqlalchemy's asynchronous driver runs only on SQLAlchemy 2.0.30 and Python 3.12, while the
other SQLAlchemy targets measure its current version.

```sh
python3.12 -m venv .bench-venv-clickhouse-sqlalchemy               # py -3.12 on Windows
.bench-venv-clickhouse-sqlalchemy/bin/pip install -r benchmarks/requirements-clickhouse-sqlalchemy.txt
```

`all` writes `docs/assets/benchmarks/results.json` — the medians, every run's numbers, the versions and
the machine — draws the charts next to it and writes the pages under `docs/benchmarks/`. The other
commands:

```sh
python benchmarks/bench.py run hare-rust --size small    # one run of one target, printed
python benchmarks/bench.py all --databases sqlite clickhouse --runs 3
python benchmarks/bench.py all --targets hare-rust django
python benchmarks/bench.py charts                        # the charts and pages again, from results.json
```

Not measured here: network latency, several processes or cores, memory use.
