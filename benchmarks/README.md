# Benchmarks

[Русский](README.ru.md)

`bench.py` compares hare-orm with SQLAlchemy, tortoise-orm, yara-orm and Django on the same
scenarios against one PostgreSQL server. The results and charts are on the
[Benchmarks](https://hare-orm.github.io/hare-orm/benchmarks/) page of the documentation.

## What is measured

| Target | What runs |
|---|---|
| `hare-rust` | hare-orm on its Rust PostgreSQL driver (`postgresql://`) |
| `hare-asyncpg` | hare-orm on asyncpg (`postgresql+asyncpg://`) |
| `sqlalchemy` | SQLAlchemy 2 async ORM on asyncpg; a session runs its statements in a transaction and commits it - SQLAlchemy's default |
| `sqlalchemy-autocommit` | the same, the engine in `AUTOCOMMIT`: every statement commits on its own, as in hare and Django; the transaction scenario still runs in a real transaction |
| `tortoise` | tortoise-orm on asyncpg |
| `yara-orm` | yara-orm |
| `django` | Django's async ORM on psycopg 3 with its connection pool |

<!-- benchmark-scenarios:start -->
Every scenario has the same name and does the same work in each ORM, written the way that ORM's
documentation writes it. The table has 1000 widgets (`--size small`: 100), each with a gadget (a
foreign key), a tag (many-to-many) and a JSON document. The scenarios, as the charts label them:

- **Reads**
  - All 1000 rows
  - 200 × get() by primary key
  - A page, LIMIT/OFFSET
  - Filter on two fields
  - exists()
  - values_list(flat=True)
  - only("id", "name")
  - OR + JOIN + distinct
  - id IN (200 values)
  - Reading a JSON field
- **Relations**
  - select_related, a JOIN
  - prefetch_related
  - N+1: 200 separate queries
  - 200 × many-to-many add()
- **Aggregates**
  - count()
  - Sum, Avg, Max, Min
  - GROUP BY
  - annotate + values
  - Case / When
- **Writes**
  - bulk_create, 1000 rows
  - 200 × create()
  - update() by a filter
  - 200 × get() + save()
  - bulk_update, 200 rows
  - Upsert, 200 rows
  - 200 × get_or_create()
  - delete() by a filter
  - 200 × get() + delete()
  - Writing a JSON field
- **Transactions, concurrency, start**
  - 200 × a transaction: get() + save()
  - 200 × get() at once
  - Start: init and the first connection
- **Load** — 50 workers run 1000 operations on one connection pool: 80% `get()`, 15% a filtered
  read, 5% `get()` + `save()`.
<!-- benchmark-scenarios:end -->

## How it is measured

<!-- benchmark-method:start -->
- Every ORM gets a pool of 50 connections, all opened before anything is timed.
- Within a run, a scenario is repeated 3–5 times and its fastest repetition counts; the deletes and
  the many-to-many `add()` change the data for good and run once.
- Each ORM runs in a process of its own. The runs go round after round, the ORMs in a shuffled order
  each round; a scenario's result is the median over the runs.
- SQLAlchemy runs twice: with a transaction around each session (its default) and with the engine in
  autocommit mode, where every statement commits on its own, as in hare and Django.
- Every scenario is written the way that ORM's documentation writes it.
<!-- benchmark-method:end -->

`all` runs every ORM once; `--runs N` runs them N times. A result is then the median, and the
charts draw the spread from the best run to the worst.

## Running it

A PostgreSQL server at `127.0.0.1:5433` with the user and password `postgres` (`make test_db_up`
starts one); each run creates and drops a database of its own there.

```sh
python -m venv .bench-venv
.bench-venv/bin/pip install -e . -r benchmarks/requirements.txt   # .bench-venv\Scripts\pip on Windows
.bench-venv/bin/python benchmarks/bench.py all
```

`all` writes `docs/assets/benchmarks/results.json` - the medians, every run's numbers, the versions
and the machine - and draws the charts next to it. The other commands:

```sh
python benchmarks/bench.py run hare-rust       # one run of one ORM, printed
python benchmarks/bench.py all --targets hare-rust django --runs 3
python benchmarks/bench.py charts              # the charts again, from results.json
```

`--port` points at another server. Not measured here: network latency, several processes or
cores, memory use.
