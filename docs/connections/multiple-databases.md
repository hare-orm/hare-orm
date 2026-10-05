# Several databases

An application with several connections picks one per query: by `using=` at the call, or by a
router that decides for every query of a model, reads and writes apart.

## <a id="using-db"></a>`using=`

A connection is chosen the same way everywhere — by one name, `using`, which takes a connection
alias (`"replica"`) or a client (a transaction's, one from `autonomous()`, a router, or
`Connections.get(connection_alias)`):

**On a queryset**: `.using(alias_or_client)` — a copy of the queryset bound to that connection.
Every query starts from one, so this covers `create`, `get`, `get_or_create`, `update_or_create`,
`filter`, `bulk_create`, `bulk_update`, `raw`, `count`, ...:
`Book.objects.using("replica").filter(...)`, `Book.objects.using(transaction).create(...)`.

**On an instance**: the `using=` argument of `save`, `delete`, `hard_delete`, `delete_preview`,
`restore` and `refresh_from_db`; of the relation writes (`add`, `remove`, `set`, `clear`, `create`,
...); and of `prefetch_related_objects()`.

**Elsewhere**: `Transactions.atomic(using)`, `on_commit(..., using=)`, `on_rollback(..., using=)`,
`execute_sql(..., using=)`.

> [!NOTE]
> **`select_for_update()` needs an open transaction**
>
> Unlike the other methods above, actually executing a `select_for_update()` queryset outside a
> `Transactions.atomic()` block raises `QueryError` on a backend that supports row
> locking — the lock would otherwise be acquired and released within the same autocommit
> statement, giving no real protection while looking like it does.

**On a `ManyToManyRelation`**: `create`, `add`, `remove`, `clear`.

```python
async with Transactions.autonomous() as conn:
    job = await Job.objects.using(conn).create(status="running")
    await job.tags.add(tag, using=conn)
```

### <a id="instances-remember-their-connection"></a>Instances remember their connection

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

## <a id="routers"></a>Routers

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
its own `hare.core.routing.connection_router.ConnectionRouter` — the internal per-context dispatcher that holds
your routers and asks each in turn (`context.router`) — so configs from different contexts never mix.

### <a id="read-your-writes"></a>Reading your own writes

A replica gets a write later than the primary — a request that creates an order and reads it back
at once may not find it there. So once an asyncio task (a request) has written through a connection,
every read of a model `db_for_write` sends to that connection goes to it rather than to the
connection `db_for_read` returns, for the rest of the task. Another task — another request — keeps
reading from the replica. A task started from the writing one sees its writes too.

A write is what changes rows: `save()`, `delete()` and `restore()` of an instance, a queryset's
`create()`, `update()`, `delete()`, `bulk_create()` and `bulk_update()`, a many-to-many `add()`,
`remove()`, `clear()` and `set()` — through the connection the router chose or `.using()` named.
Asking which connection a model is written through (`Model.get_connection(for_write=True)`)
writes nothing and changes no read.

The config's `read_your_writes_seconds` — a number above 0 and up to 3600 — ends that this long
after the task's last write, when the replica has caught up:

```python
await Hare.init(config={..., "routers": ["myapp.routers.PrimaryReplicaRouter"], "read_your_writes_seconds": 5})
```

`Routing.using_primary()` (a `with` or an `async with` block) sends every read of its block to the connection the
model is written through, whether the task wrote or not — for a check right after a write made elsewhere:

```python
from hare.core.routing import Routing

async with Routing.using_primary():
    order = await Order.objects.get(pk=order_id)
```

`Routing.forget_writes()` lets the current task read from the replicas again — for a task that
serves several requests one after another. A queryset's `.using()` and a transaction choose their
connection themselves and aren't affected. Without routers nothing of this runs.
