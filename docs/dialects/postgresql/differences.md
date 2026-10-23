# PostgreSQL: what runs differently

Where SQLite or ClickHouse compute something their own way, hare leaves their result as it is — see
[SQLite: what runs differently](../sqlite/differences.md#results) and
[ClickHouse: what runs differently](../clickhouse/differences.md#results). What differs on
PostgreSQL is what the server itself does — how it ends a transaction after an error, how long a name may
be, what each server version adds. Each row below says what hare does about it.

## <a id="transactions"></a>Transactions

| PostgreSQL | hare |
|---|---|
| A failed statement aborts the transaction | Leaving the block without an exception rolls it back and raises `TransactionManagementError`; a nested `atomic()` around the statement keeps the outer transaction usable — see [A failed statement in a transaction](../../connections/transactions.md#failed-statement). |
| `CREATE INDEX CONCURRENTLY` outside a transaction only | `AddIndex(concurrently=True)` needs `atomic = False` on its migration — see [Indexes](../../migrations/operations.md#index-operations). |
| Waiting for a lock without a limit | `migrate(lock_timeout=...)` bounds how long a migration waits for a table's lock — see [Lock timeout](../../migrations/migrations.md#lock-timeout). |

## <a id="values-and-names"></a>Values and names

| PostgreSQL | hare |
|---|---|
| A session time zone of the server's setting | Both drivers run every session in UTC — see [The session time zone](connecting.md#session-time-zone). |
| Names cut to 63 bytes | A name hare generates is kept within 63 bytes, a longer one shortened with a digest; an explicit name over 63 bytes raises `ConfigurationError` — see [`table`](../../models/meta-options.md#table). |
| `NULL` placement in an ordering | `NULL` sorts as the largest value, last ascending — see [NULL ordering](../../querying/queryset-methods.md#null-ordering). |
| Text order of the database's collation | Text compares and sorts by the collation of the database; `Collate(...)` asks for another — see [Database functions](../../querying/functions.md). |
| Integer and decimal ranges of a column type | The column refuses a value out of its range itself. |

## <a id="server-versions"></a>Server versions

hare reads the server's version when it connects and turns on what the version has; on an older server
the feature is refused — see the [features table](../dialects-and-features.md#database-features).

| Version | Adds |
|---|---|
| 15 | `merge()`; `UniqueConstraint(nulls_distinct=False)` |
| 16 | `explain(generic_plan=True)` |
| 17 | `merge().returning()` and `when_not_matched_by_source()`; `JsonTable`; an `ExclusionConstraint` on a partitioned table; `explain(memory=True)`, `explain(serialize=True)` |
| 18 | A `VIRTUAL` `GeneratedField` (`stored=False`); a `UuidV7()` `db_default`; `without_overlaps=True`; `returning(old=...)` |

What an older server does itself, hare leaves to it:

- PostgreSQL before 18 refuses to cast a JSON `null` of a `JSONField` path to another type
  (`Cast(F("data__n"), IntField())` — *cannot cast jsonb null to type integer*); PostgreSQL 18 gives `NULL`.
- PostgreSQL before 18 doesn't tell a transaction pooler its search path — the `schema` option and a
  schema per tenant behind one are refused (see [Connection poolers](../../connections/connection-poolers.md#what-hare-does)).

## <a id="schema"></a>Schema and migrations

| PostgreSQL | hare |
|---|---|
| Extensions for vectors, geodata, trigrams and some types | `Meta.extensions` declares them, and the migration autodetector writes `CreateExtension` for each — see [Extensions](../../migrations/operations.md#extensions). |
| Partitioned tables, index types | See [Indexes and tables](indexes-and-tables.md). |
| Schemas | `Meta.schema`, a schema per tenant and row level security work as written — see [Schema per tenant](../../soft-delete-versions-tenants/schema-per-tenant.md). |

## <a id="drivers"></a>Drivers

`rust_pg` and `asyncpg` give the same results through hare's API; raw SQL differs in how the two bind and
read values — see [Raw SQL on the two drivers](connecting.md#driver-differences).
