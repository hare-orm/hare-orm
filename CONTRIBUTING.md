# Contributing to hare-orm

[Русская версия](CONTRIBUTING.ru.md)

Thanks for helping. Bug reports, documentation fixes, new features and new dialects are all
welcome.

## How a change gets in

- **Branches.** Development happens on `dev`, and every pull request targets it. `main` points at
  the latest release and moves only when a release is cut (see [Releases](#releases)).
- **Discuss first when it's big.** A new public API, a change of existing behavior, or a new
  dialect feature starts as an issue, so the approach is agreed with a maintainer before code is
  written. A bug fix or a documentation fix can go straight to a pull request.
- **Review and approval.** Every pull request is reviewed by a maintainer (GitHub requests the
  review automatically through `.github/CODEOWNERS`). It is merged only when a maintainer has
  approved it, CI is green (the checks and the [test matrix](#test-matrix-and-ci) CI runs for it) and
  every commit is signed off (see [Sign-off](#sign-off)). A maintainer may decline a change that
  doesn't fit the project's direction, and says why.
- **Design rule.** A change in how hare-orm looks or behaves has to be a real improvement. Where
  it is only a matter of taste, hare-orm does what Django's ORM does, so Django users find
  familiar ground.
- **No compatibility shims before 1.0.** When you rename or remove public API, change every use
  and document it under *Breaking changes* in [CHANGELOG.md](CHANGELOG.md). Don't keep the old name
  as an alias (see [Versioning and releases](README.md#versioning-and-releases)).

Maintainer: Vladislav Yaremenko ([@VladislavYar](https://github.com/VladislavYar)).

## Setup

Requires Python 3.12, 3.13 or 3.14 and [Poetry](https://python-poetry.org/).

```bash
make deps
```

(runs `poetry install --extras asyncpg --extras opentelemetry`). This pulls in the
dev/test/contrib/docs dependency groups too — you don't need to install those separately.

Install the git hooks once so style/type issues are caught before they reach CI:

```bash
poetry run pre-commit install
```

The hook runs `make _check` — the same ruff, mypy, bandit, Rust and actionlint checks, with the
same tool versions, as `make check` and CI's check job. The Rust checks need a Rust toolchain, and
actionlint runs in Docker (see [Code style & checks](#code-style--checks)).

## Project layout

One package per area, one concept per module — the name of a module says which class or concern
it holds. The public API is what `hare`, `hare.fields`, `hare.models`, `hare.query.expressions`,
`hare.query.functions` and the documented modules export; everything else can move.

| Package | What it holds |
| --- | --- |
| `hare/` | The public entry points (`__init__.py`), `exceptions.py` and `warnings.py` — nothing else. |
| `hare/core/` | Starting and holding the ORM: `Hare` (`hare.py`), its config (`config/`), the context a process or test runs in (`hare_context.py`), the connections (`connections/`) and the router (`routing/`), the app and model registry (`apps/`: the registry, relation linking, swappable models, model discovery, table name checks), the caches and dropping them (`caching/`), and `Registries` — what dialects, fields and packages register. |
| `hare/models/` | `Model` — its public API — and the classes running its steps: an instance's set-up, connections, copies, saving, checks and dirty fields (`instances/`), writing rows (`write/`: the writer, inserts, updates, rollback restores, captured changes), deletion and soft deletion with their cascades and previews (`deletion/`), building the class and checking its definition (`class_building/`), `MetaInfo` and the metaclass (`model_meta.py`), tenancy (`tenancy/`). |
| `hare/fields/` | `Field` (`field.py`), the value fields by family (`data/`: `numeric`, `text`, `temporal`, `json`, `choices`, `containers` — arrays, maps, tuples —, `network`, boolean, binary and UUID fields), relational fields, their accessors and related managers (`relations/`), generated, encrypted and swappable fields, database defaults (`db_defaults/`), registered lookups and transforms (`registrations/`), validators (`validators/`: limits and formats). |
| `hare/query/` | Building queries: `QuerySet` and the classes checking its arguments (`queryset/`), the statements a queryset runs (`statements/`: building — joins, grouping, ordering, keysets, row locks, annotations, conditions, CTEs; `select/` — values, model rows, set operations; `summary/`; `write/` — update, delete, bulk writes, merge), statement plans and how the parts of a query describe them (`plans/`), the rewrites every entry point applies once (`rewrites/`), expressions (`expressions/`), ORM functions (`functions/`), lookups and filters (`filters/`), managers, scopes, relation loading, lookup paths and the lookup-info API (`lookup_info/`). |
| `hare/sql/` | The SQL builder the ORM renders through: terms, criteria, functions and query builders (`builder/`) — no models, no connections. |
| `hare/ddl/` | Schema objects declared in `Meta`: indexes, constraints, triggers, table options, and the dialect-neutral quoting of names and literals. |
| `hare/dialects/` | One package per database (`sqlite/`, `postgresql/` with `drivers/asyncpg` and `drivers/rust_pg`, `clickhouse/` with `drivers/clickhouse_connect` and `drivers/clickhouse_driver`) on the contract in `base/`: `Dialect` and `Features`, the connection and its client (`connection/`, `client/`), transactions, literals, parameters, clauses, renderers, types, lookups, full-text search (`search/`), schema editors and their parts (`schema/`), migration safety rules. What several of hare's dialects write alike beyond ISO SQL is a class of `base/` too, named after what it holds (`LimitReturningConflictQueryClauses`). The core imports no dialect; a dialect-specific class lives in its dialect. |
| `hare/gis/`, `hare/search/`, `hare/vectors/` | Spatial data, full-text search and vector search — one API on every dialect that has them; each dialect writes the SQL. |
| `hare/transactions/` | `Transactions` (`atomic()`, `on_commit()`), their options, and two-phase-commit transactions across databases. |
| `hare/migrations/` | Migration files and operations (`migration.py`, `operations/`), project state (`state/`), reading migrations (`loading/`), running them (`execution/`), finding the operations of a change (`autodetection/`), building and squashing new migrations (`making/`) and writing them (`writer/`), safety checks (`safety/`), schema drift (`drift/`) and the programmatic API (`api/`). |
| `hare/instrumentation/` | Observers and the events they get, query wrappers and tags, row-change events (`change_events.py`) and the capture of a model's changes (`capture/`). |
| `hare/inspectdb/` | Reading an existing database schema into model definitions. |
| `hare/cli/` | The `hare` command. |
| `hare/contrib/` | Optional integrations: framework plugins, pydantic models, request queries, test helpers, the transactional outbox, notify, taskiq, OpenTelemetry. |
| `hare/time/` | Time zones and the system clock every moment hare stamps is read from. |
| `hare/health/` | Health checks of the connections and the criteria they judge them by. |
| `hare/stubs/` | The `hare stubs` command's stubs of model modules for pyright. |
| `hare/typing_info/` | What the typing tools — the mypy plugin, `hare stubs` — know of the models. |
| `hare/classes/` | Class paths a migration writes and reads, class properties, declared subclasses. |
| `hare/lazy_loading/` | What hare prepares on first use instead of on import: regular expressions, pydantic's classes. |
| `hare/native/` | The parts of hare's compiled extension `rust.native` the Python code speeds itself up with. |
| `hare/numbers/` | Telling a finite number apart from anything else a setting or an argument may be. |

Where new code goes: a constant into the `constants.py` of the package using it, an enum into its
`enums.py`; a helper that belongs to a class is a method of that class, not a function next to it;
a module of its own for a new concept rather than a new class in an unrelated module.

Every process-wide cache is registered in `hare.core.caching.caches.Caches` where it is declared, so a
change to a model or a registry drops it — never `functools.cache`/`lru_cache` or a module-level dict:
a `Cache(max_size)` for values by key (a key holding a model class is kept and dropped with that
model), a `ModelCache()` for one value per model class (`ModelCache(depends_on_other_models=True)`
when a value describes other models too), or `Caches.register(cache)` for anything else with
`forget_model(model)` and `forget_all()`.
`tests/test_model_caches.py` fails on a model-keyed `WeakKeyDictionary` declared in its place.

A new part of a query declares how it meets its statement plan instead of describing it by hand:
an expression or a window function lists how each attribute meets the plan in `plan_parts`
(`hare.query.plans.enums.PlanPartType`), a query class how each setting meets the plan key in
`plan_slots` (`PlanKeyForm`); the description is generated from the declaration when the class is
made. Its build resolves each argument with `ExpressionArguments.get_result()`, which records the
reference of a bound value under the attribute holding it — a plan binds values by where they come
from, not by the order the build records them. A class keeping no plan declares `plannable = False`;
a class doing neither fails when it is defined, and `--verify-plans` fails on an attribute no part
declares. Plan keys are never built by walking a query's parts from outside them.

## Running the test suite

The suite runs in full against four databases. A test that needs something a dialect lacks
(transactions, foreign keys, a PostgreSQL extension) is skipped there by its feature marker, not by
the dialect's name.

```bash
make test            # the whole suite against SQLite, with coverage
make test_sqlite     # the same, coverage report off (faster local loop)
make test_columnar   # the whole suite against the columnar test dialect
```

The columnar test dialect (`tests/dialects/columnar`) is a third-party dialect built only from
hare's public API — no transactions, foreign keys or unique constraints, its own placeholders,
quoting, types and URL scheme (`columnar://`). A change that works on SQLite but relies on
something the dialect contract doesn't promise fails here.

Some tests read no test database at all: the mypy plugin's runs, the stubs, the scans of hare's own
source, the programs run in a process of their own. They are marked `database_independent` and are
a suite of their own, left out of the suite of every database — so they run once, not once more
for SQLite, the columnar dialect and each PostgreSQL driver:

```bash
make test_database_independent
```

Mark a new module of that sort with `pytestmark = pytest.mark.database_independent`. `make test`
and a plain `pytest` run every test, these included.

PostgreSQL tests need a running server. `make test_db_up` builds and starts the test server
(`tests/docker/postgres`: PostGIS, pgvector and the contrib extensions, so no PostgreSQL-only test
skips) on `localhost:5433`, with durability switched off — the suite creates and drops hundreds of
databases, and every `DROP DATABASE` forces a checkpoint:

```bash
make test_db_up                 # once; `make test_db_down` removes it
make test_postgres_asyncpg      # the whole suite via the pure-Python asyncpg driver
make test_postgres_rust         # the whole suite via the Rust driver (needs it built first, below)
```

Don't point the suite at a server holding data you care about. To use another server, override
the connection details on the command line (`HARE_POSTGRES_WORKERS` sets the xdist worker count,
8 by default):

```bash
make test_postgres_asyncpg HARE_POSTGRES_HOST=db.local HARE_POSTGRES_PORT=5432
```

Three more runs need servers of their own, each started in Docker by its `*_up` target and removed
by its `*_down` target:

```bash
make test_db_up test_pgbouncer_up && make test_pgbouncer   # the PostgreSQL suite through PgBouncer (port 6432), both drivers
make deps options="--extras clickhouse --extras clickhouse-driver"   # both ClickHouse drivers, for the next line
make test_clickhouse_up && make test_clickhouse            # the ClickHouse dialect's tests on both drivers, ClickHouse 25.8 on ports 8124 (HTTP) and 9124 (native)
make test_brokers_up && make test_brokers                  # the outbox against Redis (6390), RabbitMQ (5673), Kafka (9095)
```

PgBouncer runs in transaction pooling with prepared statements across transactions (1.21+, the
oldest hare supports); what needs a session of its own (`LISTEN`, a session lock timeout) goes
straight to the server. ClickHouse runs only the dialect's own tests (`tests/dialects/clickhouse`):
the shared test models have keys the database generates, which ClickHouse doesn't. Without their
servers, the broker tests are skipped.

Every `make` target and every matrix run passes `--verify-plans`
(`tests/plan_verification`): each query that runs on a cached plan — a statement plan, a plan by call
signature — is built again without plans, and its SQL and parameters compared with the plan's. A
difference fails the test with both statements and the plan's key, so a plan that forgets part of
its query is caught by the test that ran it. Pass it to a `pytest` of your own too:

```bash
HARE_TEST_DB=sqlite+aiosqlite://:memory: poetry run pytest --verify-plans tests/test_queryset.py
```

`make testall`/`make ci` runs `check` plus `test_database_independent`, `test_sqlite`, `test_columnar` and
`test_postgres_asyncpg` — everything except `test_postgres_rust`, which needs a Rust toolchain.
CI runs the suite on the Python, SQLite and PostgreSQL versions of the
[test matrix](#test-matrix-and-ci), through both PostgreSQL drivers — all of them for a pull request
into `main`, every night, on demand and before a release; the newest of each otherwise.

## Building the Rust extensions

`test_postgres_rust` and any local work on `rust/` need [rustup](https://rustup.rs/) plus
[maturin](https://www.maturin.rs/). `rust-toolchain.toml` pins the Rust version CI uses too - rustup
installs it on the first `cargo` call in the checkout, so a check passing locally passes in CI:

```bash
make build_native           # rust.native for this OS and the current Python, into rust/
make build_native_linux     # the Linux rust.native of Python 3.12-3.14, in Docker
make build_native_windows   # the Windows rust.native of Python 3.12-3.14 (.venv-3.X)
make build_native_macos     # the macOS rust.native of Python 3.12-3.14 (.venv-3.X), universal2
make rust_check             # cargo fmt --check, clippy and the Rust unit tests (part of make check)
```

`rust.native` (`rust/native/`) holds the PostgreSQL driver behind `postgresql://` URLs (`rust.native.pg`)
and the row readers/writers with the field codecs (`rust.native.rows`). It is optional at runtime:
without it, use `postgresql+asyncpg://` URLs, and rows are read and written in pure Python. CI builds
it on every run for Python 3.12, 3.13 and 3.14 on Linux, Windows and macOS (one universal2 file per
Python for Apple silicon and Intel Macs), shares it with every job that needs it, and commits the
nine files (`rust/native.cpython-3XX-x86_64-linux-gnu.so`, `rust/native.cp3XX-win_amd64.pyd` and
`rust/native.cpython-3XX-darwin.so`) back to `dev` and `main` — a checkout on any of the three
systems runs without a Rust toolchain, and you only need to rebuild after changing `rust/` source.

## Test matrix and CI

The test matrix is every combination the suite runs on:

| Axis | Versions |
| --- | --- |
| Python | 3.12, 3.13, 3.14 |
| SQLite | 3.35.5 (the oldest supported), 3.37.2 (Ubuntu 22.04's own; `STRICT` tables from 3.37), 3.38.0 (`->>`), 3.41.0 (`unhex()`) and 3.53.4 (the newest) — plus the newest with SQLite's regular expression functions installed; on Linux, Windows and macOS |
| PostgreSQL | 14, 15, 16, 17, 18 — each through `asyncpg` (`postgresql+asyncpg://`) and through the Rust driver (`postgresql://`); on Linux |
| Columnar test dialect | on Linux |

The SQLite versions are listed in `tests/sqlite_versions/constants.py`. A run on one of them loads
exactly that version, whatever SQLite the Python was built with:

- **Linux**: `libsqlite3.so.0` compiled from the version's amalgamation, found through
  `LD_LIBRARY_PATH`;
- **Windows**: the version's official `sqlite3.dll` next to a copy of the Python's own
  `_sqlite3.pyd`, put first on `PYTHONPATH`;
- **macOS**: `libsqlite3.0.dylib` compiled from the amalgamation, through `DYLD_LIBRARY_PATH`. The
  python.org builds (which `actions/setup-python` installs) have SQLite linked into `_sqlite3`
  statically, so the library path changes nothing there: the `_sqlite3` module is then compiled
  from the CPython sources of the running Python version (downloaded from python.org) against the
  compiled library and put first on `PYTHONPATH`. The same happens on Linux for a Python whose
  `_sqlite3` finds its SQLite through its own `RPATH` (the manylinux builds).

Preparing a version checks with a fresh interpreter that `sqlite3.sqlite_version` is the
requested one and fails otherwise; every test run also gets `HARE_TEST_SQLITE_VERSION` and stops
before its first test when another SQLite is loaded — a run never falls back to the system's
SQLite silently.

### Running the matrix locally

You need Python 3.12, 3.13 and 3.14 (`py -3.X` on Windows, `python3.X` elsewhere), Docker for the
PostgreSQL servers, and on Linux and macOS a C compiler for the SQLite libraries. The rust_pg runs
use the `rust.native` builds in `rust/`: the committed Linux, Windows and macOS files, or
`make build_native_linux`/`make build_native_windows`/`make build_native_macos` after changing
`rust/`.

```bash
make matrix_venvs         # .venv-3.12, .venv-3.13, .venv-3.14 with every dependency group
make sqlite_versions      # every SQLite version, prepared for each of those Pythons
make test_db_matrix_up    # PostgreSQL 14-18: hare-orm-test-db-14 ... -18 on ports 5414-5418
make test_matrix          # every run, then a table: Python x suite -> passed/failed, counts, time
make test_db_matrix_down  # removes the servers
```

Each run writes its pytest output to `.test-matrix/<python>-<suite>.log`; `make test_matrix`
fails when any run failed. The suites are `database-independent`, `sqlite-<version>`,
`sqlite-regexp`, `columnar`, `asyncpg-<postgres>` and `rust_pg-<postgres>`. Run a part of the matrix with the options of
`tests/matrix/matrix_runner.py`, and narrow the preparing targets with their variables:

```bash
make test_matrix MATRIX_OPTIONS="--python 3.13 --suite 'sqlite-*' --suite rust_pg-16"
make test_matrix MATRIX_OPTIONS="--workers auto --print-failed-logs"   # 8 workers by default
make test_matrix MATRIX_OPTIONS="--newest"   # the newest Python, SQLite and PostgreSQL only
make sqlite_versions matrix_sqlite_versions=newest
make matrix_venvs matrix_python_versions=3.13 matrix_interpreter=/usr/bin/python3.13
make sqlite_versions matrix_python_versions=3.13 matrix_sqlite_versions=3.35.5
make test_db_matrix_up matrix_postgres_versions=16
```

A `--suite` is a name or a shell-style pattern; one that matches no suite is an error.

The SpatiaLite tests (`tests/dialects/sqlite/test_spatialite.py`) load the library
`HARE_TEST_SPATIALITE_PATH` names — a name the system's loader finds or a path — and point PROJ at
`HARE_TEST_SPATIALITE_PROJ_DATABASE` when it is set; without the library they skip. On Linux install
`libsqlite3-mod-spatialite` and set `HARE_TEST_SPATIALITE_PATH=mod_spatialite`; on macOS
`brew install libspatialite` and `HARE_TEST_SPATIALITE_PATH=$(brew --prefix)/lib/mod_spatialite`; on
Windows `make spatialite` downloads the official build into `tests/spatialite/` and prints both
variables. CI sets them in the `test-sqlite` jobs.

### Lowest dependency versions

hare's direct dependencies — the `[project]` dependencies of `pyproject.toml` and every extra — are
also tested at the lowest versions their declared bounds allow, on Python 3.12. Their lower
bounds are a promise: raising what hare needs means raising the bound in `pyproject.toml`.

```bash
make test_db_up                 # the test server on 5433
make lowest_dependencies_venv   # .venv-lowest (LOWEST_DEPENDENCIES_VENV=<path> elsewhere)
make test_lowest_dependencies   # the suite on SQLite and on PostgreSQL through both drivers
```

`lowest_dependencies_venv` needs [uv](https://docs.astral.sh/uv/): it resolves the dependencies
with `uv pip compile --resolution lowest-direct` into `.venv-lowest/requirements.txt` and installs
them. A dependency that another one requires in a newer version gets that newer version (Robyn
requires `orjson` 3.11.5 and `uvloop` 0.22, FastAPI `pydantic` 2.7) — the file shows what was installed. The
test tools (`pytest` and its plugins, `pytz`, `ruff` — which formats the migration files the tests read) aren't hare's dependencies and stay at their
`poetry.lock` versions (`tests/lowest_dependencies/locked_tool_requirements.py`).

### CI

CI (`.github/workflows/ci.yml`) runs on every pull request, every push to `dev` and `main`, every
night (on `dev`), on demand (*Run workflow*) and before a release (the `release` workflow calls it).
Which part of the matrix a run tests, its `plan` job decides:

- **the whole matrix** — a pull request into `main`, the nightly run, a run on demand, a release;
- **its newest corner** — a push, a pull request into another branch: Python 3.14, the newest SQLite
  and `sqlite-regexp` on Linux, the columnar dialect, PostgreSQL 18 through both drivers
  (`make test_matrix MATRIX_OPTIONS=--newest`).

Each job runs the Makefile targets above — the test jobs together are `make test_matrix`, each one
part of it:

| Job | What it does | Makefile targets |
| --- | --- | --- |
| `plan` | picks the matrix of the run | — |
| `check` | ruff format check, ruff, mypy and bandit on Python 3.12; cargo fmt, clippy and the Rust unit tests; actionlint | `make deps`, `make check` |
| `build-native-linux` | `rust.native` for Python 3.12, 3.13 and 3.14 on Linux, in the maturin image (manylinux2014) | `make build_native_linux` |
| `build-native` | `rust.native` for Python 3.12, 3.13 and 3.14 on Windows and on macOS (universal2), one job per system | `make deps`, `make build_native_windows`, `make build_native_macos` |
| `test-sqlite` | the tests reading no test database (`database-independent`), every SQLite version and `sqlite-regexp`, one job per OS (Linux, Windows, macOS) and Python; the newest SQLite and `sqlite-regexp` on Linux and Python 3.14 in the newest corner | `make matrix_venvs`, `make sqlite_versions`, `make test_matrix` |
| `test-columnar` | the columnar test dialect, one job per Python (Python 3.14 in the newest corner) | `make matrix_venvs`, `make test_matrix` |
| `test-postgres` | one PostgreSQL version through both drivers, one job per version and Python (PostgreSQL 18 on Python 3.14 in the newest corner) | `make test_db_matrix_up`, `make matrix_venvs`, `make test_matrix` |
| `test-lowest-dependencies` | the lowest dependency versions on Python 3.12 — with the whole matrix | `make test_db_up`, `make lowest_dependencies_venv`, `make test_lowest_dependencies` |
| `test-pgbouncer` | the PostgreSQL suite through PgBouncer, both drivers, on Python 3.14 | `make test_db_up`, `make test_pgbouncer_up`, `make test_pgbouncer` |
| `test-clickhouse` | the ClickHouse dialect's tests against ClickHouse 25.8, on Python 3.14 | `make deps`, `make test_clickhouse_up`, `make test_clickhouse` |
| `test-brokers` | the outbox's wakeups and deliveries against Redis, RabbitMQ and Kafka, on Python 3.14 | `make test_brokers_up`, `make test_brokers` |
| `test-poetry-add` | a new poetry project adding hare-orm from the checkout | `make deps`, `make test_poetry_add` |
| `commit-native-binary` | on a push to `dev`/`main`, once every job of the run passed: commits the nine Linux, Windows and macOS `rust.native` files | — |
| `deploy-docs` | on a push to `dev`: publishes the documentation | `make docs` |

The test jobs run the `rust.native` this commit builds, not the committed files, and upload their
`.test-matrix/` logs as artifacts; a failed run's output is also printed in the job's log.

## Code style & checks

```bash
make style   # auto-format (ruff format + ruff check --fix)
make lint    # auto-format + type-check (mypy) + bandit
make check   # format check + ruff + mypy + bandit + rust_check + actionlint, no auto-fixing — what CI runs
make actionlint                         # the GitHub Actions workflows, in the pinned actionlint image
make actionlint ACTIONLINT=actionlint   # the same with an installed actionlint binary
```

actionlint checks `.github/workflows/*.yml` — syntax, expressions, job dependencies, and the
shell scripts of the steps through shellcheck (the Docker image carries shellcheck; with an
installed binary, shellcheck runs only when it is installed too).

Enforced mechanically (ruff, mypy, bandit, codespell) — don't fight the formatter, let
`make style` settle it. Beyond what the tooling catches, this codebase leans heavily on full,
descriptive names (no abbreviated variables/identifiers) and on detailed, accurate docstrings and
comments that explain *why* a piece of code is the way it is (a non-obvious constraint, a database
quirk) — match that style in new code and comments.

## Docs

If your change affects public behavior, update the relevant page under `docs/` — both the English
`.md` file and its Russian `.ru.md` sibling. Every heading carries an explicit id shared by both
languages, as an inline anchor GitHub reads too (`## <a id="some-heading"></a>Some heading`); the
site makes it the heading's own id. A note or a warning is a GitHub
alert — `> [!NOTE]`, `> [!WARNING]`, `> [!CAUTION]`, its title a bold first line — so a page reads
the same on GitHub and in an editor's preview; the site renders it as an admonition titled by that
line (`mkdocs_hooks/`). Nothing else is indented four spaces outside a list or a fence: plain Markdown
reads it as code. Verify the site still builds before opening a PR:

```bash
make docs    # mkdocs build --strict: fails on a broken link or anchor
```

## Submitting a change

1. Fork the repository and branch off `dev`. Make your change and add or update tests for it.
   `make test_sqlite` and `make test_columnar` must stay green; if the change touches PostgreSQL,
   `make test_postgres_asyncpg` too.
2. Add a line for every user-visible change under `## [Unreleased]` in
   [CHANGELOG.md](CHANGELOG.md), in its group (*Breaking changes*, *Added*, *Changed*, *Fixed*,
   *Removed*, *Security*), and the same line in Russian in [CHANGELOG.ru.md](CHANGELOG.ru.md).
   Internal refactoring and test-only changes need no entry.
3. Sign off every commit (`git commit -s`, see [Sign-off](#sign-off)).
4. Run `make check`, or push and let CI run it: CI runs the same checks, plus the
   [test matrix](#test-matrix-and-ci) — its newest corner for a pull request into `dev`.
5. Open a pull request against `dev`. Describe *why* the change is needed, not just what it does,
   and link the issue it resolves.

Found a security vulnerability instead? See [SECURITY.md](SECURITY.md) — please don't open a
public issue for one.

## Sign-off

hare-orm is MIT-licensed, and a contribution is accepted under that same license. To confirm that
you have the right to submit it on those terms, every commit carries a `Signed-off-by` line with
your name and email, certifying the [Developer Certificate of Origin 1.1](https://developercertificate.org/):

```text
Signed-off-by: Jane Doe <jane@example.com>
```

`git commit -s` adds the line for you. The `dco` workflow checks every commit of a pull request
and fails when a line is missing or names someone other than the commit's author. To sign off
commits you already made:

```bash
git rebase --signoff origin/dev
git push --force-with-lease
```

Code copied from another project is accepted only under a license compatible with MIT, with its
copyright notice kept and credited in [NOTICE.md](NOTICE.md).

## Releases

Maintainers cut a release from `dev`:

1. Choose the version by [the versioning rules](README.md#versioning-and-releases): a patch
   release for fixes only, a minor release when there is anything under *Added* or *Breaking
   changes*.
2. In [CHANGELOG.md](CHANGELOG.md) and [CHANGELOG.ru.md](CHANGELOG.ru.md), rename
   `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`, start a new empty `## [Unreleased]` above it,
   and update the comparison links at the bottom.
3. Set the version with `poetry version X.Y.Z` and in `__version__` of `hare/__init__.py`, and
   commit the changed files as `release: vX.Y.Z`.
4. Fast-forward `main` to that commit and push both branches.
5. Tag the head of `main` and push only that tag:

   ```bash
   git tag -a vX.Y.Z -m "hare-orm X.Y.Z"
   git push origin vX.Y.Z
   ```

The `release` workflow then checks that the tag matches the version in `pyproject.toml`, that
the changelog has a section for it and that the tagged commit is on `main`, and runs the whole
[test matrix](#test-matrix-and-ci) on that commit — nothing is published unless it passes. On each platform's own
runner — Linux x86-64 and ARM64 (manylinux2014 and musllinux 1.2), macOS Intel and Apple Silicon,
Windows x64 and ARM64 — it builds the Rust extension for Python 3.12, 3.13 and 3.14 (maturin
`--interpreter` with the three of them) and a hare-orm wheel per Python carrying it
(`.github/scripts/build_wheel.py`), and installs each wheel into a fresh environment of its own
Python to run hare on SQLite, and on PostgreSQL through the Rust driver on Linux (the musllinux
wheels in the `python:3.X-alpine` images). It also builds the pure wheel for other platforms and the sdist, neither of which carries a
compiled extension. Publishing to PyPI waits for a maintainer's approval of the `pypi` environment;
then the GitHub release is created, using the changelog section as its notes.

To try the release build without publishing, run the `release` workflow by hand (Actions →
release → Run workflow): it builds and checks every wheel and stops there.

A security fix is released as a patch release of the latest minor version, following
[SECURITY.md](SECURITY.md).
