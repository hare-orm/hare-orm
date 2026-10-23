# Writing a dialect: guarantees, testing and observability

Steps 11 to 13 of [Writing a dialect](index.md): what hare does for a database that guarantees less,
testing a dialect, and observing it.

## <a id="without-guarantees"></a>11. What hare does for a database without guarantees

| The database lacks | hare |
|---|---|
| Transactions | Rejects `atomic()`; runs its own multi-statement writes statement by statement. |
| Savepoints | Joins a nested `atomic()` to its transaction, which rolls back as a whole if the nested block fails. |
| Generated keys | Refuses a model whose primary key the database generates when it is bound — or takes its keys from a series before the insert (`takes_keys_before_insert`). |
| `UPDATE` / `DELETE` of stored rows | Refuses each before it is sent; checks a whole cascade before its first write, so a database without transactions is never left with half of one. |
| Correlated subqueries | Writes an `EXISTS` correlated by equal columns as a membership test; refuses any other correlated subquery before it is sent. |
| Foreign keys | Runs `on_delete` and `PROTECT` in Python; refuses a relation with `db_constraint=True` when its model is bound. |
| Unique constraints | Refuses a model declaring a uniqueness when it is bound, and a migration declaring one before its first statement. |
| `ON CONFLICT` | Refuses `bulk_create()`'s conflict options before any SQL is sent (`supports_insert_conflicts`). |
| `RETURNING` | An `UPDATE` doesn't read changed generated columns back; the values the database gives a written row stay unread on the instance (`refresh_from_db()` reads them); `returning()` is refused. |
| A count of written rows | `update()`, `delete()` and `bulk_update()` return `None`; refuses a model with `Meta.optimistic_lock_field` when it is bound (`reports_written_row_count`). |
| Row locks | Rejects `select_for_update()`. |
| Deferrable constraints | `TableClearing.defer_cascade_foreign_keys()` raises `UnSupportedError` — a hard delete through a `PROTECT` guard in the same cascade can't be deferred. |
| A primary key | Supports keyless models as above. |

`Features.checks_foreign_keys_per_cascade_step`, `checks_restrict_at_statement_end` and
`cascade_depth_limit` describe how a native cascade behaves, so hare knows when to defer or run it
itself. A client whose features set `cascade_depth_limit` raises `CascadeDepthLimitError` from
`hare.exceptions` — or a subclass of its own, as SQLite's `SqliteTriggerRecursionLimitError` — when the
database stops a cascade at that depth; `delete()` catches it, rolls the attempt back and walks the
cascade in Python.

## <a id="testing"></a>12. Testing

hare's own suite runs against a dialect: install the package (its entry point registers the driver) and
point `HARE_TEST_DB` at it (`colstore+colstore-client://.../test_{}`, the `{}` filled per run) — every
test the dialect's features allow runs. A test of something a database may lack is marked with the
features it needs:

```python
from hare.contrib.test import requires_features

@requires_features(supports_transactions=True)
async def test_rollback(db): ...

@requires_features(supports_foreign_keys=True)
async def test_cascade_in_the_database(db): ...
```

`tests/dialects/test_dialect_contract.py` checks every registered dialect and driver: names, schemes,
features, every part built for its own dialect, the registries, the schema editor, the introspector and
deterministic SQL. A test database is reset between tests with `TableClearing.clear_tables()`.

## <a id="observability"></a>13. Observability and speed

`otel_system_name` is the OpenTelemetry `db.system.name` value (`postgresql`, `clickhouse`, or
`other_sql` for a database OpenTelemetry names none for). The Rust row readers depend on the client's
`native_python_types`, not on the dialect's name. Every statement the client runs reaches
[`Observers`](../../observability/observers.md) — `QueryExecuted`, query wrappers, slow-query logging —
through the decorator described in [the client](driver-and-client.md#client); a client that skips it is
invisible to `capture_queries`, the N+1 detector and OpenTelemetry.
