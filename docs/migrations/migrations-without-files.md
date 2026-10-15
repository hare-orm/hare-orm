# Migrations without files

An application that changes a table's schema while it runs — a content type edited in an admin panel —
builds its migrations in memory, keeps them where it likes (as text in its own database, say) and applies
them itself. Everything below is imported from `hare.migrations`: `Migration`, `State`,
`MigrationRunner`, `MigrationRecorder`, `MigrationWriter`, `OperationEffect`, `OperationPlan`.

```python
class Migration:
    def __init__(
        self,
        name: str,
        app_label: str,
        *,
        operations: Iterable[Operation] | None = None,
        dependencies: Iterable[tuple[str, str]] | None = None,
    ) -> None: ...

    @classmethod
    def from_source(cls, source: str, *, name: str, app_label: str) -> Migration: ...

class State:
    @classmethod
    def from_models(
        cls,
        models: Iterable[type[Model]],
        *,
        app_label: str | None = None,
        default_connections: dict[str, str] | None = None,
    ) -> State: ...

    def get_operations(self, new_state: State, app_label: str) -> OperationPlan: ...

class Operation(ABC):
    def get_effect(self, app_label: str, state: State, dialect: Dialect) -> OperationEffect: ...

class MigrationRunner:
    def __init__(
        self,
        connection: DatabaseClient,
        *,
        recorder: MigrationRecorder | None = None,
        lock_timeout: float | None = None,
    ) -> None: ...

    async def ensure_journal(self) -> None: ...

    async def apply(self, migration: Migration, state: State, *, dry_run: bool = False) -> State: ...

    async def unapply(self, migration: Migration, state: State, *, dry_run: bool = False) -> State: ...

    async def record(
        self,
        migration: Migration,
        *,
        applied: bool,
        connection: DatabaseClient | None = None,
    ) -> None: ...

    async def collect_sql(
        self,
        migration: Migration,
        state: State,
        *,
        backward: bool = False,
    ) -> list[str]: ...

    def get_effects(self, migration: Migration, state: State) -> list[OperationEffect]: ...

class MigrationRecorder:
    def __init__(self, connection: DatabaseClient, *, table_name: str = "hare_migrations") -> None: ...
```

- `Migration(...)` builds a migration in code; `operations` and `dependencies` take the place of the
  class attributes a migration file declares.
- `Migration.from_source()` reads a migration from the text of a migration file — what
  `MigrationWriter(name, app_label, operations).as_string()` renders. The text runs as Python, so it must
  come from a place you trust. Text that doesn't run, or declares no `Migration` class, raises
  `hare.migrations.exceptions.MigrationLoadError`.
- `State.from_models()` is the schema state of model classes as they are declared. A model's app is its
  `Meta.app`, else `app_label`; a model with neither raises `ConfigurationError`.
- `old_state.get_operations(new_state, app_label)` returns an
  `OperationPlan(operations, warnings, data_loss_warnings)` — the operations `makemigrations` would write
  for the app, with its warnings.
- `Operation.get_effect()` returns an
  `OperationEffect(operation, reversible, rewrites_table, loses_data, reason)`: whether the operation can
  be unapplied, whether the database rewrites the whole table (the time grows with its rows), whether
  values or rows can be lost, and why. `RemoveField` and `DeleteModel` lose data. `AlterField` loses data
  when it changes a column's type, and rewrites the table when the dialect does that for this change —
  see [`alter_field_rewrites_table`](zero-downtime.md#alter-field-rewrites-table).
- `MigrationRunner` applies one migration on `connection` exactly as `hare migrate` does: an atomic
  migration in one transaction together with its journal record, constraint checks before the commit.
  `lock_timeout` is the [lock timeout](migrations.md#lock-timeout) its statements run under.
    - `ensure_journal()` creates the recorder's table when it doesn't exist.
    - `apply()` returns the state after the migration; `unapply()` takes the state before it was applied
      and returns it. With `dry_run=True` either runs the operations without touching the database or the
      journal.
    - `record()` marks a migration applied or unapplied in the journal. It does nothing without a
      `recorder`, and so do the journal steps of `apply()`/`unapply()`.
    - `collect_sql()` returns the migration's SQL (or its rollback's, with `backward=True`) without
      running it, inside `BEGIN;`/`COMMIT;` for an atomic migration.
    - `get_effects()` returns the `OperationEffect` of each operation, in order.
- `MigrationRecorder` is the journal of applied migrations, in the table `table_name`. Give runtime
  migrations a table of their own, so `hare migrate` never sees them.

A model registered while the application runs (`Hare.register_live_models()`), with its first migration
built from the difference between two states:

```python
from hare import Connections, Hare
from hare.migrations import Migration, MigrationRecorder, MigrationRunner, MigrationWriter, State

connection = Connections.get("default")
runner = MigrationRunner(connection, recorder=MigrationRecorder(connection, table_name="content_migrations"))
await runner.ensure_journal()

old_state = State.from_models([Book])
plan = old_state.get_operations(State.from_models([Book, Notice]), "content")
migration = Migration("0001_notice", "content", operations=plan.operations)

print("\n".join(await runner.collect_sql(migration, old_state)))  # the SQL, nothing run yet
source = MigrationWriter(migration.name, "content", migration.operations).as_string()  # keep it
new_state = await runner.apply(migration, old_state)
Hare.register_live_models([Notice], app_label="content", managed=False)
```

`managed=False` keeps `makemigrations`, `migrate` and drift checks away from the table: its migrations
live only where the application keeps them, and `hare migrate` doesn't know them. The current state of
such a table comes from its own migrations applied in order, not from the registered class. The journal
of these migrations is a table no model backs: a drift check is told of it, or reports it as untracked —
`await detect_drift_for_alias(Hare.apps, apps_config, "default", journal_tables=["content_migrations"])`,
`hare drift --journal-table content_migrations`.
