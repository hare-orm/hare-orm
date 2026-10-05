# Making and applying migrations

The `hare` commands that make and apply migrations, called from Python; the lock timeout a
migration runs under; squashing many migrations into one; the `Migration` class a migration file
declares; and how a migration writes enums built at run time and swappable models.

## <a id="migrations-api"></a>The commands as functions (`hare.migrations.api`)

Every migration command is a function — the CLI only parses arguments, calls it and prints the
result. Call them from a deploy script, a test or a management endpoint instead of shelling out:

```python
from hare.migrations.api import checkmigrations, makemigrations, migrate, plan, sqlmigrate, squashmigrations

await makemigrations(*, config, app_labels=None, empty=False, merge=False, name=None) -> MigrationChanges
await squashmigrations(*, config, app_label, end_name, start_name=None, squashed_name=None) -> SquashedMigration
await migrate(*, config, app_labels=None, target=None, fake=False, dry_run=False, reporter=None, progress=None, lock_timeout=None) -> None
await plan(*, config, app_labels=None, target=None) -> list[str]
await sqlmigrate(*, config, app_label, migration_name, backward=False) -> list[str]
await checkmigrations(*, config, app_labels=None) -> list[MigrationRisk]
```

`config` is anything [`Hare.init()`](../connections/configuration.md#hare-init) takes: a
`HareConfig`, a dict, the path of a `.json`/`.yml` file, or `"module.VARIABLE"`.

- `makemigrations()` returns the migrations that take each app's history to its models — **not
  written yet**: `MigrationChanges.writers` holds one `MigrationWriter` per new migration (empty
  when nothing changed), `warnings` the possible renames it didn't recognize, `data_loss_warnings`
  the changes that lose stored values, `safety_risks` the `MigrationRisk`s of the new migrations
  (found without reading the database, so every existing table counts as large — see
  [Checking migrations](zero-downtime.md#checking-migrations)). Write each with `writer.write()` (it returns the file's
  path); `writer.as_string()` is the file's text, `writer.name`/`writer.app_label` identify it.
  An app without a migrations package gets one created. `empty=True` makes one empty migration per
  app and `merge=True` one migration per app joining its forked history — both need `app_labels`
  and can't be combined. `ConfigurationError` for an unknown app, for a forked history without
  `merge`, and for `merge` of a history with nothing to merge.
- `squashmigrations()` squashes a run of an app's migrations — from `start_name` (the app's first
  without it) to `end_name`, each a name or a unique prefix of one — into one, see
  [Squashing migrations](#squashing-migrations). It returns a `SquashedMigration`: `writer` (the
  migration, not written yet; `None` when the run holds one migration), `replaced_names`,
  `squashed_operation_count` (the operations before they were shortened) and
  `elided_operation_count` (the `elidable` `RunPython`/`RunSQL` left out). `ConfigurationError` for
  an unknown app or migration, a start after the end, a prefix naming several migrations, and a name
  one of the app's migrations already has.
- `migrate()` applies or unapplies migrations to reach `target` — `"app_label"` (that app's latest),
  `"app_label.migration_name"`, or `"app_label.zero"` (unapply everything); without it every app
  goes to its latest. `reporter(connection_alias, plan_steps, fake, dry_run)` is called with each
  connection's plan before it runs, `progress(event, app_label, migration_name)` around each
  migration; either may be an `async def`.
- `plan()` returns (and prints) the ordered steps `migrate()` would take; `sqlmigrate()` returns
  the SQL of one migration (`backward=True` — of unapplying it) without running it.
- `checkmigrations()` returns the risks of the migrations `migrate()` would apply — see
  [Checking migrations](zero-downtime.md#checking-migrations).

```python
changes = await makemigrations(config="settings.HARE_ORM", app_labels=["models"])
for writer in changes.writers:
    print("writing", writer.write())
await migrate(config="settings.HARE_ORM")
```

`makemigrations()` and `squashmigrations()` bind the models in a context of their own and never
connect to a database. `migrate()`, `plan()` and `sqlmigrate()` call `Hare.init()` themselves and
leave the context initialized — call `await Hare.close_connections()` when done.

## <a id="lock-timeout"></a>Lock timeout

```python
await migrate(config="settings.HARE_ORM", lock_timeout=5)   # or "migrations": {"lock_timeout": 5}
```

```text
hare migrate --lock-timeout 5
```

A migration altering a busy table waits for the lock every open transaction on it holds — and while
it waits, every later query of the table queues behind it. With `lock_timeout` (seconds, `0.001` up
to `2147483.647`) a statement of the migration that waits longer for a lock another session holds
fails: the migration is rolled back, isn't recorded, and `migrate` raises the database's error
(`OperationalError` — "canceling statement due to lock timeout" on PostgreSQL, "database is locked"
on SQLite); retry it once the table is quieter. `migrate(lock_timeout=...)` and `--lock-timeout`
take precedence over the config's `migrations.lock_timeout`; a wrong type or out-of-range value
raises `ConfigurationError`. ClickHouse has no lock timeout for DDL: a migration run with one there
raises `UnSupportedError`.

| Migration | PostgreSQL | SQLite |
|---|---|---|
| Atomic | `SET LOCAL lock_timeout` in its transaction | the connection's busy timeout for its transaction, restored after it |
| Non-atomic | a connection of its own with `SET lock_timeout` for the whole migration (a pooled connection would lose the setting), every query of the connection's name going to it; closed afterwards | the connection's busy timeout for the whole migration, restored after it |

## <a id="squashing-migrations"></a>Squashing migrations

```text
hare squashmigrations app 0012                                      # 0001 to 0012
hare squashmigrations app 0005 0012 --squashed-name accounts_rework  # 0005 to 0012
```

The squashed migration holds the operations of the migrations it replaces, in order, shortened:
a model created and changed later is created changed, a field added and removed cancels out, a
model created and deleted too, renames in a row become one rename, later options of a model take
the place of earlier ones. An operation is moved across another only when the two touch no common
model — a `RunPython`/`RunSQL`, which may touch any, is never crossed. A `RunPython`/`RunSQL` is
kept, unless it's declared `elidable=True`; a `RunPython` function defined in a replaced migration
file is copied into the new file with the imports of its module it uses (a function using other
names of its module — a constant, a helper — is refused: move them into the function or an
importable module). The new migration is named after the first one it replaces
(`0005_accounts_rework`; `0001_squashed_0012_<name>` without `--squashed-name`), depends on what
the replaced ones depend on outside the run, and is `atomic = False` when any of them is. Squashing
a run holding a squashed migration replaces that one's originals.

`replaces` decides how a database is migrated while the replaced files are still on disk:

| The replaced migrations applied to the database | `migrate` |
|---|---|
| None | Runs the squashed migration and records it with every replaced one. |
| All | Runs nothing and records the squashed migration. |
| Some | Runs the rest of the replaced migrations one by one, then records the squashed migration. |

Unapplying the squashed migration unapplies and unrecords them all. A migration depending on a
replaced one — of the same app or another — depends on the squashed one instead. Once every
database has applied all of the replaced migrations, delete their files (and then the
`replaces` of the squashed migration); a database that applied only some of them while their files
are gone raises `MigrationLoadError` naming the missing ones.

## <a id="migration"></a>`Migration`

```python
class Migration:
    operations: list[Operation] = []
    dependencies: list[tuple[str, str]] = []
    run_before: list[tuple[str, str]] = []
    replaces: list[tuple[str, str]] = []
    initial: bool | None = None
    atomic: bool = True
```

With `atomic = True` (the default) the whole migration, including its entry in the migrations
table, runs in one transaction when the backend supports transactional DDL. With `atomic = False`
operations run one by one outside a transaction, except an operation declared with `atomic=True`
(`RunSQL`/`RunPython`), which runs and rolls back as a unit in its own transaction.

SQLite applies most column changes by rebuilding the table: a copy is created from the model,
filled from the old table and renamed into its place. The copy's `AUTOINCREMENT` counter goes on
from the old table's, so the id of a deleted newest row is never issued again; a view naming the
table keeps working; every trigger and index is re-created, a partial `UniqueConstraint(condition=
...)` included. When the change alters a field's type, each stored value is converted the way
PostgreSQL's `USING column::type` does (hare runs every PostgreSQL session in UTC, so there too): a
datetime to its date (of the UTC instant for an aware one), a date to its midnight (as UTC under
`use_timezone=True`), a number to a boolean (non-zero is true), the
`t`/`true`/`yes`/`on`/`1` spellings (and their negatives) to a boolean, a boolean to `true`/`false`
text, a fractional number to the nearest integer, an integer or float to decimal text with the
field's decimal places. A value the conversion can't read is copied unchanged.

SQLite runs every migration with `PRAGMA foreign_keys = OFF` (its table rebuilds need it). Before an
atomic migration commits, and after each operation of a non-atomic one, `PRAGMA foreign_key_check`
verifies that no row points at a missing parent; a violation raises `hare.exceptions.IntegrityError`
listing the offending rows and rolls the migration (or the atomic operation) back. A connection
configured with `foreign_keys` off is not checked.

When `migrate` is given several targets, each one is planned against the state the earlier ones
leave behind; targets that would both apply and unapply the same migration raise `QueryError`.

Migration files are re-read on every load: a file edited after it was first imported is picked up
by the next `migrate`/`plan` in the same process.

## <a id="runtime-enums"></a>Enums built while the application runs

A `CharEnumField`/`IntEnumField` may take an enum built while the application runs — from choices
set in an admin panel:

```python
Status = StrEnum("Status", {"NEW": "new", "DONE": "done"})
status = fields.CharEnumField(Status, default=Status.NEW)
```

Such an enum has no module attribute to import it from, so a migration declares it at its top in
the same functional form, on the same base (`StrEnum`, `IntEnum`, `IntFlag`, `Enum`, a data type
mixed in with `type=`), its members in their order — once, however many fields, defaults and
constraint conditions use it:

```python
from enum import StrEnum

Status = StrEnum('Status', {'NEW': 'new', 'DONE': 'done'})

class Migration(migrations.Migration):
    operations = [
        ops.AddField(model_name='Ticket', name='status',
                     field=fields.CharEnumField(default=Status.NEW, enum_type=Status, max_length=4)),
    ]
```

An enum is imported instead when its `__module__` and `__qualname__` lead to the class itself, or
it declares `migration_import_path`. Two different enums of one name are declared as `Status` and
`Status2`. The autodetector compares an enum by what it holds — its bases, name and members in
order — not by the class: the same enum built again after a restart changes nothing, a member
added, removed, renamed, revalued or moved is an `AlterField`.

## <a id="swappable-models"></a>Swappable models in migrations

A package writes its migrations once; a project points them at its own model through a
[swappable setting](../models/relations.md#swappable-models). `makemigrations` writes a relation
declared with `swappable("USER_MODEL")` as that call, and its dependency as
`migrations.swappable_dependency("USER_MODEL")` instead of a migration of the app the setting points
into now:

```python
from hare import fields, migrations
from hare.migrations import operations as ops
from hare.models import swappable


class Migration(migrations.Migration):
    dependencies = [migrations.swappable_dependency("USER_MODEL")]

    operations = [
        ops.CreateModel(
            name="Consent",
            fields=[
                ("id", fields.IntField(primary_key=True)),
                ("user", fields.ForeignKeyField(swappable("USER_MODEL"), related_name="consents", null=True)),
            ],
        ),
    ]
```

- `swappable_dependency("USER_MODEL")` is `(app_label, "__first__")` — the first migration of the
  app the setting points into (none for the migration's own app). It's evaluated when the file is
  imported, so Hare must be initialized before migrations load; otherwise loading fails with
  `MigrationLoadError` saying to call `Hare.init()` first.
  `swappable_dependency` is the class `SwappableDependency` (`hare.migrations`): that tuple, keeping
  the setting it came from in `.setting`.
- The `CreateModel` of a model declaring `Meta.swappable` records it in `options`. While the model is
  swapped, every operation on its table (`CreateModel`, `AddField`, `AddIndex`, ... and their
  rollback) only changes the migration state — `migrate` creates no table for it, and
  `migrate <app> zero` works the same with or without the setting. `sqlmigrate` and `plan` show the
  foreign key to the table of the model actually used.
- Pointing the setting at another model changes nothing in the package's migrations: the field
  compares as the same reference. The project's own model gets an ordinary `CreateModel` in the
  project's app.
- `makemigrations` of a project whose app the setting points into has no migration yet still works:
  the package's `swappable_dependency` on it is left out until that app's first migration exists.
  A project model pointing back at a package model forms a cycle with it; `makemigrations` splits it
  into two migrations the way it does for any two apps migrated together.
- `makemigrations` and `squashmigrations` sort the imports of and format the files they write with
  ruff when it's installed, so a written migration passes `ruff check`/`ruff format --check`.

Choose the setting before the package's tables are created. `migrate` refuses to run while a table
an applied migration created has a `swappable()` foreign key referencing another table than the
model its setting points at now, and `hare drift` reports the column with the setting's name. To
move an existing project to another model, write a migration of your own: create the new model's
table, copy the rows, repoint the foreign keys of the package's tables (`RunSQL`), and only then
change the setting.
