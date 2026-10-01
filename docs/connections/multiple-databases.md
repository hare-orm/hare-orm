# Several databases

## `using=` {: #using-db }

A connection is chosen the same way everywhere - by one name, `using`, which takes a connection
alias (`"replica"`) or a client (a transaction's, one from `autonomous()`, a router, or
`Connections.get(alias)`):

**On a queryset**: `.using(alias_or_client)` — a copy of the queryset bound to that connection.
Every query starts from one, so this covers `create`, `get`, `get_or_create`, `update_or_create`,
`filter`, `bulk_create`, `bulk_update`, `raw`, `count`, ...:
`Book.objects.using("replica").filter(...)`, `Book.objects.using(transaction).create(...)`.

**On an instance**: the `using=` argument of `save`, `delete`, `hard_delete`, `delete_preview`,
`restore` and `refresh_from_db`; of the relation writes (`add`, `remove`, `set`, `clear`, `create`,
...); and of `prefetch_related_objects()`.

**Elsewhere**: `Transactions.atomic(using)`, `on_commit(..., using=)`, `on_rollback(..., using=)`,
`execute_sql(..., using=)`.

!!! note "`select_for_update()` needs an open transaction"
    Unlike the other methods above, actually executing a `select_for_update()` queryset outside a
    `Transactions.atomic()` block raises `QueryError` on a backend that supports row
    locking — the lock would otherwise be acquired and released within the same autocommit
    statement, giving no real protection while looking like it does.

**On a `ManyToManyRelation`**: `create`, `add`, `remove`, `clear`.

```python
async with Transactions.autonomous() as conn:
    job = await Job.objects.using(conn).create(status="running")
    await job.tags.add(tag, using=conn)
```

### Instances remember their connection {: #instances-remember-their-connection }

A model instance remembers the connection alias it was loaded from (including instances built by
`select_related()`/`prefetch_related()`, `stream()`, `union()` and `raw()`) or last saved to
(`save()`, `create()`, `get_or_create()`, `bulk_create()`). Every later operation on it that isn't
given `using=` goes there by default: `save()`, `delete()`, `restore()`, `refresh_from_db()`,
`prefetch_related_objects()`, awaiting a forward relation (`await book.author`), and the relations
of the instance (`author.books.all()`/`.filter()`/`.count()`/`.values()`, M2M `add()`/`remove()`/`clear()`):

```python
book = await Book.objects.all().using("replica").get(title="Notes")
author = await book.author          # read from "replica", not from "default"
books = await author.books.all()    # "replica" too
```

The alias is looked up when the query runs, so inside an open `atomic()` on that same alias
the transaction is used. An explicit `using=`/`.using()` always wins, and so does a router that
has an opinion for the queried model. A related model whose default connection differs from the
instance model's (a cross-database relation), or a relation read from an instance whose model the
router routes, keeps its own connection choice.

## Routers {: #routers }

Write a **plain class** — no base class to inherit, hare dispatches to it duck-typed — with either
or both methods, each returning a **connection alias string** (or `None`):

```python
class ReportingRouter:
    def db_for_read(self, model: type[Model]) -> str | None:
        if model._meta.app == "reporting":
            return "reporting_replica"
        return None

    def db_for_write(self, model: type[Model]) -> str | None: ...
```

Either method may be absent entirely — hare skips a router for an action it doesn't implement,
rather than erroring. The first configured router to return a non-`None` alias wins (Django-style
ordering); if none do, the model's usual default connection applies. A returned alias that doesn't
resolve to a configured connection raises `ConfigurationError` — a router matching but naming a
typo'd/stale alias is treated as a real misconfiguration, not silently ignored.

Reads that decide a write run on the `db_for_write` connection, like Django: the existence check of
`get_or_create()`, the locked read of `update_or_create()` and `select_for_update()`. A read
replica may not have a row the primary already holds (replication lag, or a row created earlier in a
still-open transaction), and a miss there would create a duplicate.

Register via `Hare.init(routers=[...])` — a list of dotted-path strings (`"my_app.routers.
ReportingRouter"`) or classes directly, each instantiated without arguments. Each `HareContext` owns
its own `hare.core.router.ConnectionRouter` — the internal per-context dispatcher that holds
your routers and asks each in turn (`ctx.router`) — so configs from different contexts never mix.
