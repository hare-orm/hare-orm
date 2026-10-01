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
  approved it, CI is green (checks plus the four test suites) and every commit is signed off (see
  [Sign-off](#sign-off)). A maintainer may decline a change that doesn't fit the project's
  direction, and says why.
- **Design rule.** A change in how hare-orm looks or behaves has to be a real improvement. Where
  it is only a matter of taste, hare-orm does what Django's ORM does, so Django users find
  familiar ground.
- **No compatibility shims before 1.0.** When you rename or remove public API, change every use
  and document it under *Breaking changes* in [CHANGELOG.md](CHANGELOG.md). Don't keep the old name
  as an alias (see [Versioning and releases](README.md#versioning-and-releases)).

Maintainer: Vladislav Yaremenko ([@VladislavYar](https://github.com/VladislavYar)).

## Setup

Requires Python 3.14+ and [Poetry](https://python-poetry.org/).

```bash
make deps
```

(runs `poetry install --extras asyncpg --extras opentelemetry`). This pulls in the
dev/test/contrib/docs dependency groups too — you don't need to install those separately.

Install the git hooks once so style/type issues are caught before they reach CI:

```bash
poetry run pre-commit install
```

The hook runs `make _check` - the same ruff, mypy and bandit checks, with the same tool
versions, as `make check` and CI's check job.

## Project layout

One package per area, one concept per module - the name of a module says which class or concern
it holds. The public API is what `hare`, `hare.fields`, `hare.models`, `hare.query.expressions`,
`hare.query.functions` and the documented modules export; everything else can move.

| Package | What it holds |
| --- | --- |
| `hare/` | The public entry points (`__init__.py`), `exceptions.py` and `warnings.py` - nothing else. |
| `hare/core/` | Starting and holding the ORM: `Hare` (`hare.py`), its config (`config.py`), the context a process or test runs in (`context.py`, `context_registry.py`), the connections and the router (`connections.py`, `router.py`), the app and model registry with its relation wiring (`apps.py`), the caches and dropping them (`caches.py`, `cache.py`, `model_cache.py`), and `Registries` - what dialects, fields and packages register. |
| `hare/models/` | `Model` (its mixins in `model/`: persistence, relations, hydration, dirty tracking, the queryset shortcuts), the metaclass and `MetaInfo`, tenancy, deletion cascades (`deletion/`). |
| `hare/fields/` | `Field` (`base.py`), the value fields by family (`data/`: `numeric`, `text`, `boolean`, `binary`, `uuids`, `temporal`, `json`, `choices`), relational fields and their related managers (`relations/`), generated, encrypted and swappable fields, database defaults, validators. |
| `hare/query/` | Building queries: `QuerySet` and every other query (`queryset/` - one module per query: `bulk_create`, `update`, `delete`, `values`, `aggregation`, `set_operations`, ...; the plan keys of model queries in `queryset/plan_keys/`), the statement plans repeated queries run from and how the parts of a query describe them (`plans/`), expressions (`expressions/`: `Q`, `F`, `Case`, subqueries, value references), ORM functions (`functions/`), lookups and filters (`filters/`), the default manager and its scope, relation paths (`lookup_paths.py`) and the lookup-info API (`lookup_info.py`). |
| `hare/sql/` | The SQL builder the ORM renders through: terms, criteria, functions and query builders - no models, no connections. |
| `hare/ddl/` | Schema objects declared in `Meta`: indexes, constraints, triggers, table options, and the dialect-neutral quoting of names and literals. |
| `hare/dialects/` | One package per database (`sqlite/`, `postgresql/` with `drivers/asyncpg` and `drivers/rust_pg`) on the contract in `base/`: `Dialect`, `Driver`, the client (`client.py`), transactions, executors (`executor/`), schema editors (`schema/`), types, operators, renderers. The core imports no dialect; a dialect-specific class lives in its dialect. |
| `hare/transactions/` | `Transactions` (`atomic()`, `atomic()`, `on_commit()`), their options, and two-phase-commit transactions across databases (`distributed.py`). |
| `hare/migrations/` | Migration files and operations (`migration.py`, `operations/`), project state (`state/`), reading migrations (`loading/`: graph, loader, recorder), running them (`execution/`), writing new ones (`autodetection/`, `writer.py`), schema drift (`drift.py`) and the programmatic API (`api/`). |
| `hare/instrumentation/` | Query and transaction hooks, row-change events, query tags. |
| `hare/inspectdb/` | Reading an existing database schema into model definitions. |
| `hare/cli/` | The `hare` command. |
| `hare/contrib/` | Optional integrations: framework plugins, pydantic models, request queries, test helpers, outbox, notify, OpenTelemetry. |
| `hare/utils/` | Time zones and lazily compiled patterns. |

Where new code goes: a constant into the `constants.py` of the package using it, an enum into its
`enums.py`; a helper that belongs to a class is a method of that class, not a function next to it;
a module of its own for a new concept rather than a new class in an unrelated module.

Every process-wide cache is registered in `hare.core.caches.Caches` where it is declared, so a
change to a model or a registry drops it - never `functools.cache`/`lru_cache` or a module-level dict:
a `Cache(max_size)` for values by key (a key holding a model class is kept and dropped with that
model), a `ModelCache()` for one value per model class (`ModelCache(depends_on_other_models=True)`
when a value describes other models too), or `Caches.register(cache)` for anything else with
`forget_model(model)` and `forget_all()`.
`tests/test_model_caches.py` fails on a model-keyed `WeakKeyDictionary` declared in its place.

A new part of a query - an expression, a window function, a query type built into other queries -
describes its statement plan in its own class (`hare.query.plans.description.Plannable`):
`get_plan_description(context)` gives the structure that is part of the plan key and the values the
plan binds, in the order the build records their references; a class keeping no plan declares
`plannable = False`. A class doing neither fails when it is defined. Plan keys are never built by
walking a query's parts from outside them.

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

`make testall`/`make ci` runs `check` plus `test_sqlite`, `test_columnar` and
`test_postgres_asyncpg` — everything except `test_postgres_rust`, which needs a Rust toolchain.
CI runs all four.

## Building the Rust extensions

`test_postgres_rust` and any local work on `rust/` need a Rust toolchain
([rustup](https://rustup.rs/)) plus [maturin](https://www.maturin.rs/):

```bash
make build_native         # rust.native for this OS, into rust/
make build_native_linux   # the Linux rust.native, in Docker (for a Windows or macOS checkout)
make rust_check           # cargo fmt --check, clippy and the Rust unit tests (part of make check)
```

`rust.native` (`rust/native/`) holds the PostgreSQL driver behind `postgresql://` URLs (`rust.native.pg`)
and the row readers/writers with the field codecs (`rust.native.rows`). It is optional at runtime:
without it, use `postgresql+asyncpg://` URLs, and rows are read and written in pure Python. CI builds
it once per push (Linux and Windows), shares it with every job that needs it, and commits it back to
`dev` and `main` — you only need to rebuild after changing `rust/` source.

## Code style & checks

```bash
make style   # auto-format (ruff format + ruff check --fix)
make lint    # auto-format + type-check (mypy) + bandit
make check   # format check + ruff + mypy + bandit, no auto-fixing — what CI runs
```

Enforced mechanically (ruff, mypy, bandit, codespell) — don't fight the formatter, let
`make style` settle it. Beyond what the tooling catches, this codebase leans heavily on full,
descriptive names (no abbreviated variables/identifiers) and on detailed, accurate docstrings and
comments that explain *why* a piece of code is the way it is (a non-obvious constraint, a database
quirk) — match that style in new code and comments.

## Docs

If your change affects public behavior, update the relevant page under `docs/` — both the English
`.md` file and its Russian `.ru.md` sibling. Headings that other pages link to carry an explicit id
(`## Some heading {: #some-heading }`) shared by both languages. Verify the site still builds
before opening a PR:

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
4. Run `make check`, or push and let CI run it: CI runs the same checks, plus the full suite
   against SQLite, the columnar test dialect and PostgreSQL via both drivers, on every pull
   request.
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
the changelog has a section for it and that the tagged commit is on `main`. On each platform's own
runner it builds the Rust extensions and a wheel carrying them - Linux x86-64 and ARM64
(manylinux2014 and musllinux 1.2), macOS Intel and Apple Silicon, Windows x64 and ARM64 - and installs every
wheel into a fresh environment to run hare on SQLite, and on PostgreSQL through the Rust driver on
Linux. It also builds the pure wheel for other platforms and the sdist, neither of which carries a
compiled extension. Publishing to PyPI waits for a maintainer's approval of the `pypi` environment;
then the GitHub release is created, using the changelog section as its notes.

To try the release build without publishing, run the `release` workflow by hand (Actions →
release → Run workflow): it builds and checks every wheel and stops there.

A security fix is released as a patch release of the latest minor version, following
[SECURITY.md](SECURITY.md).
