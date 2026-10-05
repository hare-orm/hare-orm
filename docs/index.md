# hare-orm

An async Python ORM for PostgreSQL, SQLite and ClickHouse, built with relations in mind. Models are plain
classes, queries are built lazily through a `QuerySet`, and migrations are generated from the changes to
the models. Coming from Django? See [Coming from Django](getting-started/coming-from-django.md).

- **Fast.** A Rust PostgreSQL driver decodes rows and converts values outside Python, and every query
  shape is built once: a repeated query only binds its new values — see
  [Benchmarks](benchmarks/index.md).
- **Relations done right.** Composite primary keys everywhere — as foreign-key targets too — generic
  relations made of real foreign keys, `select_related()`/`prefetch_related()` with filters, recursive
  queries over a tree, and an error instead of a silently inflated aggregate — see
  [Models and relations](#models-and-relations).
- **Your schema, declared.** Constraints, partial and expression indexes, triggers, views, materialized
  views, functions, sequences, row level security policies, grants and partitioning live in the model's
  `Meta` and are created and changed by migrations — see [The schema in `Meta`](#schema-in-meta).
- **Application patterns built in.** Soft delete, versioned rows, multi-tenancy (by column, by schema or
  by row level security), optimistic locking, dirty-field tracking and a transactional outbox are `Meta`
  options — see [Behavior declared in `Meta`](#behavior-in-meta).
- **Migrations you can trust.** Detected from the models, checked for operations that lock a busy table,
  compared with the live database (`hare drift`), and generated from an existing database
  (`hare inspectdb`) — see [Migrations](#migrations).
- **Every database on its own terms.** SQLite, PostgreSQL and ClickHouse are dialects built on the public
  API a third-party package uses; PostgreSQL's types, indexes, PostGIS, pgvector, full-text search and
  `LISTEN`/`NOTIFY` come with it, and a database lacking a feature raises a clear error instead of
  returning a wrong result — see [Databases](#databases).
- **Ready for production.** Observers, OpenTelemetry, SQL comment tags, an N+1 detector, pool metrics,
  distributed transactions, read replicas and routers, PgBouncer support, Pydantic schemas and Litestar,
  FastAPI and Robyn integrations — see [Observability](#observability).
- **Checked before it runs.** A mypy plugin rejects a misspelled filter and types the rows of `values()`;
  a pytest plugin, factories and query-count assertions cover the tests — see
  [Testing and type checking](#testing-and-type-checking).

## <a id="quick-start"></a>Quick start

Install the package, declare two models and query them:

```bash
pip install hare-orm
```

```python
# library/models.py
from hare import fields
from hare.models import Model


class Author(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=120)


class Book(Model):
    id = fields.IntField(primary_key=True)
    author = fields.ForeignKeyField("models.Author", related_name="books")
    title = fields.CharField(max_length=300)
    rating = fields.IntField(default=0)
```

```python
# main.py
import asyncio

from hare import Hare, HareConfig
from library.models import Author, Book


async def main() -> None:
    await Hare.init(HareConfig.from_db_url("sqlite+aiosqlite://db.sqlite3", {"models": ["library.models"]}))
    await Hare.generate_schemas()

    author = await Author.objects.create(name="Ursula K. Le Guin")
    await Book.objects.create(author=author, title="The Left Hand of Darkness", rating=5)

    async for book in Book.objects.filter(author__name__icontains="le guin").select_related("author"):
        print(book.title, "—", book.author.name)

    await Hare.close_connections()


asyncio.run(main())
```

A real project keeps its schema in migrations instead of `generate_schemas()`: `hare makemigrations`,
then `hare migrate`. See [Your first model](getting-started/first-model.md).

## <a id="models-and-relations"></a>Models and relations

A model is a class of fields, and a relation is a field too — see [Field types](models/field-types.md)
and [Relations](models/relations.md).

```python
from hare import fields
from hare.ddl import RawSQLTerm
from hare.models import Model


class Author(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=120)


class Shelf(Model):
    store = fields.CharField(max_length=20)
    number = fields.IntField()
    pk = fields.CompositePrimaryKey("store", "number")      # a composite primary key


class Tag(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50, unique=True)


class Book(Model):
    id = fields.IntField(primary_key=True)
    isbn = fields.CharField(max_length=13, unique=True)
    title = fields.CharField(max_length=300)
    author = fields.ForeignKeyField("models.Author", related_name="books")
    shelf = fields.ForeignKeyField("models.Shelf", related_name="books", null=True)  # both key columns
    tags = fields.ManyToManyField("models.Tag", related_name="books")
    rating = fields.IntField(default=0)
    published_at = fields.DateField(null=True)
    price = fields.DecimalField(max_digits=10, decimal_places=2)
    quantity = fields.IntField(default=0)
    stock_value = fields.GeneratedField(                     # computed by the database
        RawSQLTerm("price * quantity"), output_field=fields.DecimalField(max_digits=12, decimal_places=2)
    )
    supplier_token = fields.EncryptedTextField(null=True)   # encrypted at rest, hidden in logs


class Category(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=100)
    parent = fields.ForeignKeyField("models.Category", related_name="children", null=True)


class Comment(Model):
    id = fields.IntField(primary_key=True)
    target = fields.GenericForeignKeyField({"book": Book, "shelf": Shelf}, related_name="comments")
    text = fields.TextField()
```

- **Composite primary keys** work everywhere a single-column key does: as a foreign-key target with a
  real table-level `FOREIGN KEY`, in joins, cascades, `prefetch_related()`, `__in` filters and cursors. A
  table may also have [no primary key at all](models/meta-options.md#primary_key).
- **`GenericForeignKeyField`** is an exclusive arc of real foreign keys — a column, a foreign key and an
  index per target, with a `CHECK` that exactly one is set — so the database keeps it consistent.
- Every `on_delete` — `CASCADE`, `RESTRICT`, `PROTECT`, `SET_NULL`, `SET_DEFAULT` — also runs for the
  rows a `QuerySet.delete()` removes, and `delete_preview()` shows what deleting a row would reach.
- Fields cover JSON (with path lookups), enums, decimals, UUIDs, binary data, aware datetimes, arrays,
  ranges, geometry and vectors; [custom fields](extending/custom-fields.md) plug into the same machinery.

## <a id="queries"></a>Queries

A `QuerySet` builds a query lazily and runs it when awaited — see
[QuerySet methods](querying/queryset-methods.md).

```python
from hare import Transactions
from hare.query.expressions import Case, Exists, F, OuterReference, Q, Subquery, Value, When, Window
from hare.query.functions import Avg, Count, Sum
from hare.query.functions.window import Lag, Rank

# Filters across relations, Q objects and F expressions
await Book.objects.filter(Q(title__icontains="dark") | Q(rating__gte=4), shelf__store="north")
await Book.objects.filter(quantity=0).update(quantity=F("quantity") + 10, rating=F("rating") - 1)

# Aggregates, grouped by what you select
await Author.objects.annotate(book_count=Count("books"), avg_rating=Avg("books__rating")).filter(book_count__gte=3)
await Book.objects.values("author__name").annotate(total=Sum("quantity")).order_by("-total")

# Subqueries, Exists and conditional expressions
latest = Book.objects.filter(author=OuterReference("pk")).order_by("-published_at").values("title")[:1]
await Author.objects.annotate(
    latest_title=Subquery(latest),
    has_bestseller=Exists(Book.objects.filter(author=OuterReference("pk"), rating=5)),
)
await Book.objects.annotate(level=Case(When(rating__gte=4, then=Value("good")), default=Value("other")))

# Window functions
await Book.objects.annotate(
    rank_in_author=Window(Rank(), partition_by=["author_id"], order_by=["-rating"]),
    previous_price=Window(Lag("price"), partition_by=["author_id"], order_by=["published_at"]),
)

# A category and every descendant, in one WITH RECURSIVE query
await Category.objects.filter(pk=root.pk).with_recursive("children", max_depth=5)

# Relations loaded with the rows, keyset pagination, a locked row
await Book.objects.select_related("author").prefetch_related("tags")
newest = Book.objects.order_by("-published_at", "id")
next_page = await newest.after_cursor(*newest.cursor_values(last_book)).limit(20)
async with Transactions.atomic():
    book = await Book.objects.select_for_update(skip_locked=True).get(id=book_id)

# Upsert in one statement
await Book.objects.bulk_create(books, on_conflict=["isbn"], update_fields=["price", "quantity"])
```

- **Typed rows.** `values()` and `values_list()` return dicts and tuples, with paths through relations,
  JSON keys and date parts (`"published_at__year"`).
- **Safe aggregates.** An aggregate next to a to-many join that would count a row twice raises
  `QueryError` instead of returning an inflated number; grouping sets and subtotals are supported.
- **Set operations** (`union()`, `intersection()`, `difference()`), `FilteredRelation`, `Lateral`,
  `JsonTable`, CTEs (`with_cte()`), `distinct(<fields>)`, random sampling and `iterator()`/`stream()`
  over large results.
- **[The query plan cache](querying/query-plan-cache.md)** keeps the SQL of every query shape: the second
  `filter(title__icontains=...)` with another value skips building the query and only binds the value.
- **[Raw SQL](querying/raw-sql.md)** when you need it: `RawSQL(...)` inside a query, `raw()` returning
  model instances, `execute_sql()`.

## <a id="behavior-in-meta"></a>Behavior declared in `Meta`

Tenancy, soft delete, optimistic locking, dirty-field tracking and change capture are options of `Meta` —
see [Meta options](models/meta-options.md).

```python
from hare.contrib.outbox import ChangeCapture, OutboxEvent
from hare.contrib.versioning import VersionedModel
from hare.models.tenancy.tenancy import Tenancy


class LoanEvent(OutboxEvent):                  # the outbox table, relayed to Kafka, RabbitMQ, Redis...
    pass


class Loan(Model):
    id = fields.IntField(primary_key=True)
    library_id = fields.IntField(db_index=True)
    status = fields.CharField(max_length=20)
    version = fields.IntField(default=1)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        tenant_field = "library_id"          # every query scoped to the active tenant
        soft_delete_field = "deleted_at"     # delete() marks the row; queries hide it
        optimistic_lock_field = "version"    # save() of a stale copy raises StaleObjectError
        track_dirty_fields = True            # get_dirty_fields(); save() writes only the changes
        change_capture = ChangeCapture(LoanEvent)  # every write also lands in the outbox


with Tenancy.scope(library.id):
    overdue = await Loan.objects.filter(status="overdue")    # WHERE library_id = ... AND deleted_at IS NULL
    await overdue[0].delete()                                # soft delete
    restored = await Loan.objects.only_deleted().get(id=loan_id)
    await restored.restore()


class CatalogRecord(VersionedModel):                # a new row per version, (id, version) as the key
    title = fields.CharField(max_length=200)


current = await CatalogRecord.get_last_version_or_exception(id=record_id)
draft = await current.create_new_version(title="Second draft")
```

- **[Multi-tenancy](soft-delete-versions-tenants/multi-tenancy.md)** by a column, by a [schema per
  tenant](soft-delete-versions-tenants/schema-per-tenant.md), or enforced by the database itself through
  row level security — the scope also reaches joined models and fills the tenant of a new row.
- **[Soft delete](soft-delete-versions-tenants/soft-delete.md)** cascades to the related rows that
  soft-delete too, and `restore()` brings them back.
- **[The transactional outbox](integrations/outbox.md)** writes an event in the transaction of the change
  it reports and delivers it at least once, in order per row, to Kafka, RabbitMQ, Redis, a webhook or
  taskiq.

## <a id="schema-in-meta"></a>The schema in `Meta`

Constraints, indexes, triggers, views, functions and row level security are declared in `Meta` too, and
migrations create them — see [Constraints and triggers](models/constraints-and-triggers.md) and
[Views, functions, sequences and access](models/schema-objects.md).

```python
from hare.ddl import (
    CheckConstraint, DatabaseFunction, MaterializedView, PartialIndex, Policy, RawSQLTerm,
    RowLevelSecurity, TenantCondition, Trigger, TriggerEvent, TriggerTiming, UniqueConstraint, View,
)
from hare.dialects.postgresql.indexes import GinIndex
from hare.query.expressions import Q


class Invoice(Model):
    id = fields.IntField(primary_key=True)
    tenant_id = fields.IntField()
    number = fields.CharField(max_length=20)
    amount = fields.DecimalField(max_digits=12, decimal_places=2)
    status = fields.CharField(max_length=10)
    tags = fields.JSONField(default=list)

    class Meta:
        tenant_field = "tenant_id"
        constraints = (
            CheckConstraint(name="invoice_amount_positive", check=Q(amount__gt=0)),
            UniqueConstraint(fields=("tenant_id", "number"), condition=Q(status="open")),
        )
        indexes = (PartialIndex(fields=("tenant_id",), condition=Q(status="open")), GinIndex(fields=("tags",)))
        triggers = (
            Trigger(
                name="invoice_no_negative",
                on=TriggerEvent.UPDATE,
                timing=TriggerTiming.BEFORE,
                when=Q(amount__lt=0),
                body=RawSQLTerm("RAISE EXCEPTION 'negative amount'; RETURN NEW;"),
            ),
        )
        views = [View("open_invoices", query=lambda: Invoice.objects.all_tenants().filter(status="open").values("id"))]
        materialized_views = [
            MaterializedView(
                "invoice_totals",
                query=RawSQLTerm("SELECT tenant_id, sum(amount) AS total FROM invoice GROUP BY tenant_id"),
                unique_columns=("tenant_id",),
            )
        ]
        functions = [
            DatabaseFunction(
                "current_tenant",
                returns="integer",
                body=RawSQLTerm("SELECT current_setting('app.tenant')::integer"),
                language="sql",
            )
        ]
        row_level_security = RowLevelSecurity.FORCED
        policies = [Policy(name="invoice_tenant", using=TenantCondition())]
```

Every object here is created by `migrate`, changed when its declaration changes and dropped when it is
removed; `hare drift` reports the tables, columns, indexes, constraints and triggers the database no
longer matches the models in. Conditions are `Q` objects over the model's own fields, so a renamed field
is renamed inside them too; raw SQL is always an explicit `RawSQLTerm(...)`. PostgreSQL tables can also
be [partitioned](models/meta-options.md#table_options) by hash, list or range, with
`ExclusionConstraint`, deferrable constraints, sequences and grants alongside.

## <a id="migrations"></a>Migrations

The `hare` command detects the changes of the models, applies them and checks them against a database in
use — see [The `hare` command](migrations/cli.md).

```bash
hare makemigrations          # detect the changes to your models
hare migrate                 # apply them
hare checkmigrations         # flag operations that would lock or rewrite a large table
hare drift library              # compare the models of an app with the live database
hare inspectdb > models.py   # generate models from an existing database
hare squashmigrations library 0010
```

```text
WARNING: risky on a database in use if the tables are large - `hare checkmigrations` checks against the database:
  app.0007_remove_book_pages: Remove field pages from Book [remove_field]
    The column of Book.pages is dropped while the code still running reads it.
    Safely: First remove the field from the models only - SeparateDatabaseAndState(state_operations=[RemoveField(model_name='Book', name='pages')]) - and drop the column with RunSQL in a later migration, once no running code reads it.
```

- **[Zero-downtime rules](migrations/zero-downtime.md)** know which operations rewrite a table, take a
  long lock or break the code still running, and say how to do each one safely —
  `AddIndex(..., concurrently=True)`, `AddConstraint(..., not_valid=True)` with `ValidateConstraint`,
  `BackfillColumn` in batches and `AlterColumnNotNullSafe` are built in.
- Renames, data migrations (`RunPython`, `RunSQL`), merges of forked histories, migrations across apps
  and connections, [migrations without files](migrations/migrations-without-files.md) for
  runtime-registered models, and a `hare` command with `shell`, `dbshell`, `sqlmigrate` and `stubs`.

## <a id="transactions-and-connections"></a>Transactions and connections

`Transactions` opens transactions, savepoints, independent and distributed transactions — see
[Transactions](connections/transactions.md).

```python
from hare import Transactions

async with Transactions.atomic(lock_timeout=2.0):
    await loan.save()
    Transactions.on_commit(lambda: send_due_date_reminder(loan.id))      # runs once the transaction commits

async for attempt in Transactions.atomic(isolation="serializable", retries=3):
    async with attempt:                                             # run again after a serialization failure
        await lend(book_id, reader_id)

async with Transactions.autonomous() as connection:                     # commits even if the caller rolls back
    await CatalogImport.objects.using(connection).create(status="started")

async with Transactions.distributed(coordinator="library", participants=["billing"]) as distributed:
    await Loan.objects.using(distributed.coordinator).create(...)       # two-phase commit
    await Deposit.objects.using(distributed["billing"]).create(...)

book = await Book.objects.using("replica").get(id=book_id)              # its relations read from "replica" too
```

Savepoints nest, `statement_timeout`/`lock_timeout`/`read_only` apply per transaction, a
[router](connections/multiple-databases.md) sends reads and writes to their databases, and
[PgBouncer](connections/connection-poolers.md) 1.21+ keeps prepared statements working behind it.

## <a id="databases"></a>Databases

SQLite, PostgreSQL and ClickHouse are dialects of one API; what each database has is in
[Dialects and features](dialects/dialects-and-features.md).

| | SQLite | PostgreSQL | ClickHouse |
|---|---|---|---|
| Driver | `aiosqlite` | `rust_pg` (the default) or `asyncpg` | `clickhouse-connect` (HTTP) or `clickhouse-driver` (native TCP) |
| Transactions | ✓ | ✓ | with `transactions=true` on a server with ClickHouse Keeper |
| Foreign keys and `on_delete` | ✓ | ✓ | checked and run by hare |
| `UPDATE` in place | ✓ | ✓ | a mutation finished before the call returns, or a lightweight `UPDATE` |
| Full-text search (`__search`, `SearchRank`, `SearchHeadline`) | FTS5 | `tsvector` | — |
| Vector search (`VectorField`, `CosineDistance`) | sqlite-vec | pgvector | — |
| Geometry (`PointField`, `__dwithin`, `__contains`) | SpatiaLite | PostGIS | ClickHouse's geo types |

```python
from hare import Connections
from hare.dialects.postgresql.functions import TrigramSimilarity
from hare.search import SearchQuery, SearchRank, SearchType
from hare.vectors import CosineDistance

await Book.objects.filter(description__search=SearchQuery("white whale", search_type=SearchType.PHRASE))
await Book.objects.annotate(rank=SearchRank(("title", "description"), "white whale")).order_by("-rank")
await Book.objects.annotate(distance=CosineDistance("embedding", query_vector)).order_by("distance")[:10]
await Author.objects.annotate(score=TrigramSimilarity("name", "Gerard")).filter(score__gt=0.3)

listener = await Connections.get("default").listen("book_returned", on_notify)   # PostgreSQL LISTEN/NOTIFY
```

PostgreSQL adds `ArrayField`, range and multirange fields, `HStoreField`, `CitextField`,
[PostGIS](dialects/search-and-geodata/gis.md), trigram and `unaccent` lookups, and the GIN, GiST, BRIN,
Bloom, Hash, SP-GiST, IVFFlat and HNSW indexes. [ClickHouse](dialects/clickhouse/connecting.md) tables
declare their engine, sort key, partitioning, TTL, projections and distribution over a cluster;
ClickHouse adds arrays, maps, tuples and `Variant`/`Dynamic` fields, `final()`, `prewhere()`,
`limit_by()` and `with_totals()`, its own aggregates, materialized views and dictionaries, and row locks
in ClickHouse Keeper. Another database is a [dialect package](extending/writing-a-dialect/index.md) away.

## <a id="web-apis-and-integrations"></a>Web APIs and integrations

A request query turns the parameters of an HTTP request into a query, and the framework integrations open
the ORM with the application — see [Web frameworks](integrations/web-frameworks.md).

```python
from pydantic import BaseModel, ConfigDict

from hare.contrib.frameworks import PageSchema
from hare.contrib.frameworks.fastapi import HareFastAPI, RequestQueryDependency
from hare.contrib.request_query import OffsetPagination, OrderingConfig, Page, RequestQuery, SearchConfig


class BookSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str


class BookQuery(RequestQuery[Book]):        # ?author=3&search=dark&ordering=-published_at&limit=20
    author: int | None = None
    title__icontains: str | None = None

    class Meta:
        queryset = Book.objects.all().select_related("author")
        search = SearchConfig(fields=("title", "author__name"))
        ordering = OrderingConfig(fields=("title", "published_at"), default=("-published_at",))
        pagination = OffsetPagination(default_limit=50, max_limit=200)


app = HareFastAPI(hare_config=HARE_CONFIG, atomic_requests=True)   # every request in a transaction


@app.get("/books", response_model=PageSchema[BookSchema])
async def list_books(books: BookQuery = RequestQueryDependency.provide(BookQuery)) -> Page[Book]:
    return await books.page()
```

[Request queries](integrations/request-queries.md) turn the parameters of an HTTP request into a
validated, filtered, searched, ordered and paginated query. [Litestar](integrations/litestar.md),
[FastAPI](integrations/fastapi.md) and [Robyn](integrations/robyn.md) integrations open the ORM with the
app, can run every request in a transaction and turn ORM errors into HTTP statuses;
[Pydantic](integrations/pydantic.md) schemas are generated from the models, and
[taskiq](integrations/taskiq.md) workers run every task in the tenant scope of the code that sent it,
with its queries tagged by the task, and a task is sent only once the transaction asking for it commits.

## <a id="observability"></a>Observability

Observers see every query, changed rows and transactions; OpenTelemetry and the N+1 detector are built on
them — see [Observers](observability/observers.md) and
[N+1 query detection](observability/repeated-queries.md).

```python
from hare.contrib.opentelemetry import OpenTelemetryInstrumentor
from hare.contrib.repeated_queries import RepeatedQueryDetector
from hare.instrumentation import Observers, QueryExecuted, QueryTags

OpenTelemetryInstrumentor().instrument()                     # a span per query, pool metrics
Observers.observe(QueryExecuted, lambda event: metrics.observe(event.duration_ms))

async with RepeatedQueryDetector(threshold=10, window_seconds=1.0):  # warns about N+1 patterns as they happen
    with QueryTags.scope(job="sync_orders"):                 # ... /*job='sync_orders'*/ on every query
        await sync_orders()
```

Observers also see changed rows and transaction events; a slow-query log, pool health checks and metrics
complete the picture. See [Observability](observability/observers.md).

## <a id="testing-and-type-checking"></a>Testing and type checking

Factories make the objects of a test, the pytest plugin gives every test its database, and the mypy
plugin checks queries before they run — see [Test setup](testing/setup.md).

```python
from hare.contrib.factories import ModelFactory, Sequence, SubFactory


class AuthorFactory(ModelFactory[Author]):
    name = Sequence(lambda number: f"Author {number}")


class BookFactory(ModelFactory[Book]):
    isbn = Sequence(lambda number: f"{number:013d}")
    title = Sequence(lambda number: f"Book {number}")
    price = 10
    author = SubFactory(AuthorFactory)


async def test_list_books(hare_db, hare_assert_query_count):     # fixtures of the bundled pytest plugin
    await BookFactory.create_batch(3)
    async with hare_assert_query_count(1):
        await Book.objects.select_related("author")
```

```python
Book.objects.filter(titel="War")
# error: Unknown filter param 'titel': Book has no field 'titel'  [hare-query]

rows = await Book.objects.values("id", "title", "author__name")
reveal_type(rows[0])   # TypedDict({'id': int, 'title': str, 'author__name': str})
```

The pytest plugin creates the test databases, isolates every test in a rolled-back transaction and runs a
test only where the database has the features it needs; the
[mypy plugin and pyright stubs](querying/type-checking.md) check filters, orderings and `values()`
against the models.

## <a id="where-to-go-next"></a>Where to go next

- **[Installation](getting-started/installation.md)** and **[Your first
  model](getting-started/first-model.md)** — from an empty project to a migrated database.
- The sections of the navigation describe every field, `Meta` option, `QuerySet` method, migration
  operation and database feature against the code: what the code does is what the page says.
