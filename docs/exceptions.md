# Exceptions (`hare.exceptions`)

```
HareError(Exception)
├── ConfigurationError
├── FieldError
├── QueryError(HareError, ValueError)
├── NoValuesFetched
├── IncompleteInstanceError
├── ValidationError(HareError, ValueError)
├── UnSupportedError
├── ObjectLookupError                    — .model: type[Model] | None
│   ├── MultipleObjectsReturned
│   └── DoesNotExist(ObjectLookupError, LookupError)
├── TransactionManagementError
│   ├── DistributedTransactionPartiallyCommittedError — .xid, .coordinator_alias, .pending_participant_aliases
│   └── DistributedTransactionCommitAmbiguousError    — .xid, .coordinator_alias, .participant_aliases
├── DatabaseError                        — .sql, .params
│   ├── OperationalError
│   │   ├── TransactionRetryError
│   │   ├── TooManyParametersError
│   │   └── CascadeDepthLimitError        — caught by delete() itself, never reaches your code from it
│   │       └── SqliteTriggerRecursionLimitError  — hare.dialects.sqlite.exceptions
│   ├── IntegrityError
│   │   ├── StaleObjectError              — .model, .pk, .expected_version
│   │   └── ProtectedError                — .protected_objects: list[Model]
│   └── DBConnectionError(DatabaseError, ConnectionError)
├── NonExistentTimeError(HareError, ValueError)
└── DecryptionError(HareError, ValueError)
```

Every exception hare raises is a `HareError`. The tree separates two types of failure:

- **The call is wrong** - `QueryError`, `FieldError`, `ConfigurationError`, `ValidationError`,
  `UnSupportedError`, `NoValuesFetched`, `IncompleteInstanceError`: raised by hare itself, before
  anything is sent to the database.
- **The database refused** - `DatabaseError` and its subclasses: what the connection's driver
  reported, and the integrity checks hare runs in the database's place (`PROTECT`, `RESTRICT`, the
  optimistic lock).

`QueryError` and `ValidationError` are also a `ValueError`, `DoesNotExist` a `LookupError`,
`DBConnectionError` a `ConnectionError` - a plain `except ValueError:`/`except LookupError:` written
without knowing about hare catches them.

An error a database reported (`IntegrityError`, `OperationalError`, `DBConnectionError`, ...) is raised
from the driver's own exception, on every backend: it is the exception's `__cause__` (and its
`args[0]`), carrying the server's `sqlstate`, `constraint_name`, `table_name`, ... on Postgres.

| Exception | Raised when |
|---|---|
| `FieldError` | A problem with a model field: an unknown field or lookup in a query, an invalid field reference. |
| `QueryError` | A call can't be run as given: a wrong argument (`clone()` without a required `pk=`), a combination of query methods SQL can't express, or an operation on an instance in the wrong state (unsaved, of another tenant). Nothing is sent to the database. Also a `ValueError`. |
| `ConfigurationError` | The configuration is invalid: settings, connections, apps, a model's `Meta`, a field's declaration or a manager - found while hare is set up, or the first time it is used. Running a query before `Hare.init()` is one too. |
| `TransactionManagementError` | Misuse of the transaction API. |
| `DistributedTransactionPartiallyCommittedError` | Raised by `Transactions.distributed()`: the distributed transaction's decision was already durably committed (the coordinator's own `COMMIT` succeeded), but one or more participants failed to finish `COMMIT PREPARED` before the call returned — the transaction DID commit, this is not a rollback. Carries `.xid`, `.coordinator_alias`, `.pending_participant_aliases`. Resolve the unresolved participants via `hare distributed-recover` or a later manual retry. |
| `DistributedTransactionCommitAmbiguousError` | Raised by `Transactions.distributed()`: the coordinator's own decision-commit itself failed or its outcome is unknown (e.g. the connection dropped after the server processed `COMMIT` but before the acknowledgement came back) — unlike `DistributedTransactionPartiallyCommittedError`, it isn't known whether the transaction committed at all. Carries `.xid`, `.coordinator_alias`, `.participant_aliases`. Every participant is still holding a prepared transaction; run `hare distributed-recover` to resolve them correctly rather than guessing. |
| `DatabaseError` | Base of everything the database refused; carries `.sql`/`.params` when known (see [below](#sqlerrormixin)). |
| `OperationalError` | The database failed to run a statement. |
| `TransactionRetryError` | The database aborted the statement because of a concurrent transaction, and running the whole transaction again can succeed: a serialization failure under `REPEATABLE READ`/`SERIALIZABLE`, a deadlock the database broke by aborting this side, or a database busy with another writer (SQLite's "database is locked"). The connection's driver decides which errors these are (`Driver.is_retryable`). Inside `atomic()` the transaction is rolled back as the error leaves the block - retry the block as a whole, never one statement of it. |
| `IntegrityError` | A write would break the data's integrity - a constraint the database reported, or a `RESTRICT`/`PROTECT` relation hare checked in the database's place. |
| `StaleObjectError` | `Meta.optimistic_lock_field` detected a concurrent modification — the `UPDATE` touched 0 rows because the version didn't match. Carries `.model`, `.pk`, `.expected_version` (the version the instance was read at). |
| `ProtectedError` | Deleting a row blocked by an `on_delete=PROTECT` relation. Carries `.protected_objects: list[Model]`. |
| `NoValuesFetched` | Reading a relation (or a field `.only()`/`.defer()` left out) that hasn't been loaded - by `select_related()`, `prefetch_related()`, `prefetch_related_objects()` or `await`. |
| `DoesNotExist` | `get()`/`Model[pk]` found no matching row. Carries `.model`. Also a `LookupError`. |
| `MultipleObjectsReturned` | `get()` matched more than one row. Carries `.model`. |
| `IncompleteInstanceError` | Saving a partial (`.only()`/`.defer()`) instance that's missing its pk or the fields being saved - also passing one to `bulk_create()`/`bulk_update()` without every field the call writes. |
| `TooManyParametersError` | A statement binds more parameters than the driver or the database accepts in one statement (SQLite's `SQLITE_LIMIT_VARIABLE_NUMBER`, asyncpg's 32767, the Postgres protocol's 65535 on rust_pg) - the connection stays usable, so this is an `OperationalError`, not a `DBConnectionError`. |
| `CascadeDepthLimitError` | A hard DELETE's native `ON DELETE CASCADE` stopped at the database's recursion limit before finishing (a client whose `Features` set `cascade_depth_limit`; on SQLite it is `SqliteTriggerRecursionLimitError`, past `SQLITE_LIMIT_TRIGGER_DEPTH`). `delete()` runs such a DELETE in a transaction, catches this, rolls back and walks the cascade in Python; only a raw DELETE through `execute()` raises it to you. |
| `DBConnectionError` | Connection-level failure — also a `ConnectionError`, so a plain `except ConnectionError:` catches it too. A statement the driver refuses to send (too many parameters, a message it can't encode) is an `OperationalError` instead - the connection is still fine. |
| `ValidationError` | A field validator failed, or a value can't be converted to its field's type. Also a `ValueError`. |
| `NonExistentTimeError` | A naive datetime falls in a daylight-saving gap - a wall-clock time the zone skips, with no real instant (`Timezone.make_aware()`). Also a `ValueError`. |
| `DecryptionError` | A value read from an `EncryptedTextField`/`EncryptedJSONField` can't be decrypted — it was written with a different key than the one passed to `configure_field_encryption()`, or the column doesn't hold an encrypted token. Also a `ValueError`. |
| `UnSupportedError` | The connection's database, dialect, driver or server version can't do what was asked - a lookup, `select_for_update()`, `stream()`, `distinct(*fields)`, `bulk_create(returning=/use_copy=)`, a transaction or isolation level, two-phase commit, a field type, constraint, index option or trigger type, `.explain(output_format=...)` on SQLite (plain `.explain()` works there too, via `EXPLAIN QUERY PLAN`), or a server older than the dialect runs on. Raised before anything is sent. A mistake in the declaration itself, whatever the database, is a `ConfigurationError`. |

## Migration errors {: #migration-errors }

The migrations raise their own `HareError` subclasses, from `hare.migrations.exceptions`:

```
HareError
└── HareMigrationError
    ├── UnknownMigrationError(HareMigrationError, LookupError)
    ├── MigrationLoadError
    ├── CircularDependencyError
    ├── IncompatibleStateError
    ├── IrreversibleMigrationError
    ├── PartiallyAppliedMigrationError
    ├── FieldNarrowingDataLossError
    ├── ForeignKeyTargetChangeError
    └── InconsistentMigrationStateError(HareMigrationError, RuntimeError)
```

| Exception | When |
|---|---|
| `UnknownMigrationError` | An app label, a migration name or a `migrate` target names nothing that exists - or a migration name matches several migrations. Also a `LookupError`. |
| `MigrationLoadError` | A migration file can't be loaded: it doesn't import, has no `Migration` class, a migration is defined twice, or a dependency names a migration or app that doesn't exist. |
| `CircularDependencyError` | The dependencies of the migrations form a cycle. |
| `IncompatibleStateError` | An operation can't run on the state the migrations before it leave. |
| `IrreversibleMigrationError` | A migration is unapplied while one of its operations can't be reversed. |
| `PartiallyAppliedMigrationError` | A non-atomic migration failed partway through - some of its operations already ran. |
| `FieldNarrowingDataLossError` | An `AlterField` narrowing a column would cut stored values. |
| `ForeignKeyTargetChangeError` | An `AlterField` points a relation at another target column (`to_field`) while rows store key values of the old one. |
| `InconsistentMigrationStateError` | Replaying the migration files leaves a relation pointing at a model that no longer exists. |

A migration operation or a value that can't be declared or written into a migration file (a
`lambda` default, `RemoveIndex()` without a name or fields) raises `ConfigurationError`; `migrate`
targets that contradict each other raise `QueryError`.

## The SQL of a database error {: #sqlerrormixin }

Every `DatabaseError` - `OperationalError`, `IntegrityError`, `DBConnectionError` and their
subclasses - carries the statement that failed:

```python
class DatabaseError(HareError):
    def __init__(self, *args, sql: str | None = None, params: list | None = None) -> None
    sql: str | None
    params: list | None
```

When `sql` is set, `str(exc)` includes it — useful for logging/Sentry without having to dig it out
of the exception by hand. The bind parameters never go into `str(exc)` (nor into the exception event
an OpenTelemetry span records): they can hold passwords, tokens or personal data. They stay on
`exc.params` for debugging - log them only where that data is allowed to go:

```python
try:
    await Model.objects.raw(bad_sql)
except OperationalError as exc:
    logger.error("query failed", extra={"sql": exc.sql})
    logger.debug("query params", extra={"params": exc.params})
```

The message the driver or the server itself gives can still name a value, e.g. asyncpg's
`invalid input for query argument $1: 'value'`. hare converts a value through its field wherever the
field is known - a plain filter/`update()` value, `Value(...)` compared with or written to a
column, a text literal in arithmetic with a numeric column (`F("pin") + "5"`) - so a malformed one
fails as the field's `ValidationError` first (hiding a `sensitive=True` field's value). A literal
the database meets with no field to go by - `annotate(x=Value(...))` compared later, a raw SQL
parameter, a function argument - reaches the driver as is, and its error text can show it.

## Handling patterns {: #handling-patterns }

```python
try:
    await link.delete()
except ProtectedError as exc:
    raise ValidationError(f"{len(exc.protected_objects)} object(s) still reference this row")
```

```python
try:
    await report.save()
except StaleObjectError as exc:
    # someone else modified this row first — refetch and retry, or surface a conflict to the user
    logger.warning("stale write on %s pk=%s, expected version %s", exc.model.__name__, exc.pk, exc.expected_version)
    ...
```

```python
widget = await Widget.objects.get_or_none(pk=widget_id)
if widget is None:
    raise NotFound()
```
