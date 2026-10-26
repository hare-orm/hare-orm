# Meta options

Database-level model behavior beyond individual fields is configured through a nested `Meta` class:

```python
class Foo(Model):
    ...
    class Meta:
        table = "custom_table"
        constraints = [UniqueConstraint(fields=("field_a", "field_b"))]
```

| Option | Meaning |
|---|---|
| `abstract` | No table; the fields and `Meta` pass to the concrete subclasses — see [`abstract`](#abstract). |
| `table` | The table's name — see [`table`](#table). |
| `primary_key` | `None` for a table without a primary key — see [Models without a primary key](#primary_key). |
| `managed` | `False` leaves the table to something other than migrations — see [`managed`](#managed). |
| `schema` | The table's schema — an attached database on SQLite, a database of the server on ClickHouse — see [`schema`](#schema). |
| `app` | The app label of `"app.Model"` references — see [`app`](#app). |
| `swappable` | The setting through which a project replaces the model — see [Swappable models](relations.md#swappable-models). |
| `extensions` | The PostgreSQL extensions the model needs — see [Extensions](../migrations/operations.md#extensions). |
| `constraints` | Unique, check and exclusion constraints — see [Constraints](constraints-and-triggers.md#constraints). |
| `triggers` | The table's triggers — see [Triggers](constraints-and-triggers.md#triggers). |
| `views` | Views of SQL or of a queryset — see [Views](schema-objects.md#views). |
| `materialized_views` | Materialized views — see [Materialized views](schema-objects.md#materialized-views). |
| `dictionaries` | Dictionaries of the model's rows, read by key (ClickHouse) — see [Dictionaries](../dialects/clickhouse/schema-objects.md#dictionaries). |
| `functions` | Functions stored in the database — see [Functions](schema-objects.md#functions). |
| `sequences` | Sequences, created before the table — see [Sequences](schema-objects.md#sequences). |
| `row_level_security` | Row level security of the table, on or forced — see [Row level security](schema-objects.md#row-level-security). |
| `policies` | The row level security policies of the table — see [Row level security](schema-objects.md#row-level-security). |
| `grants` | Privileges on the table and the model's other objects — see [Grants](schema-objects.md#grants). |
| `indexes` | The table's indexes — see [Indexes](indexes.md). |
| `ordering` | The default ordering of the model's queries — see [`ordering`](#ordering). |
| `get_latest_by` | What `latest()` and `earliest()` order by — see [`get_latest_by`](#get-latest-by). |
| `soft_delete_field` | The `DatetimeField` marking a soft-deleted row — see [Soft delete](../soft-delete-versions-tenants/soft-delete.md). |
| `soft_delete_hard_cascade` | `True` really deletes the rows a soft delete cascades to — see [`soft_delete_hard_cascade`](#soft-delete-hard-cascade). |
| `optimistic_lock_field` | The version every write of an instance checks and bumps — see [Optimistic locking](model-methods.md#optimistic-locking). |
| `tenant_field` | The column holding the row's tenant — see [`tenant_field`](#tenant-field). |
| `tenant_schema` | `True` keeps the table in each tenant's schema — see [Schema per tenant](../soft-delete-versions-tenants/schema-per-tenant.md). |
| `track_dirty_fields` | `True` keeps the snapshot of each row `get_dirty_fields()` compares with — see [Tracking changes](model-methods.md#tracking-changes). |
| `change_capture` | Every write of the model also writes its changed rows to the outbox — see [Capturing a model's changes](../integrations/outbox.md#change-capture). |
| `returning` | The default of `returning` of `bulk_create()` and `bulk_update()` — see [`returning`](#returning). |
| `table_description` | The table's comment — see [`table_description`](#table-description). |
| `table_options` | How each dialect stores the table — see [Table options](#table_options). |
| `manager` | The manager of `Model.objects` — see [`manager`](#manager). |

## <a id="abstract"></a>`abstract`

`True`: no table is generated for the model; its fields and `Meta` are inherited by the concrete
subclasses, each getting a deep copy of them rather than a shared object. A model is abstract only when
its own `Meta` says so: a `Meta` subclassing the abstract model's (`class Meta(Base.Meta)`) leaves it
concrete.

A concrete model subclassing another concrete model is a model of its own: a table of its own, with copies of
the fields, and none of the other model's `Meta` options but its [`manager`](#manager), copied for the
subclass — its `Model.objects` filters the subclass's rows the same way. A relation other models point at the
other model with isn't the subclass's — its rows aren't in that table — so reading it on the subclass
(`child.refs`) raises `AttributeError`.

## <a id="table"></a>`table`

Database table name override (default: derived from the model name). PostgreSQL cuts an identifier to 63
bytes: a generated table name, M2M through table or through column longer than that is shortened with a
digest suffix, so two long names never become the same identifier; an explicit table, column
(`source_field` or the field name), through table, index or constraint name over 63 bytes raises
`ConfigurationError` on every database. A name holding a dot raises `ConfigurationError` too — the schema
is `Meta.schema`.

## <a id="managed"></a>`managed`

`bool`, default `True`. `False` makes `makemigrations`/`migrate`, `Hare.generate_schemas()`, `hare drift`
and `truncate_all_models()` skip this model's table entirely — it's never created, altered, or flagged as
drift, as if it didn't exist to the migration system. For a model backed by something the migration
system shouldn't own (a view, a table introspected at runtime) — see
[`Hare.register_live_models()`](runtime-models.md). Switching an existing model's `managed` either way
only alters its options in the migration state — its table is never dropped or created for it (see
[Operations](../migrations/operations.md)).

## <a id="schema"></a>`schema`

The schema the table is in: a schema on PostgreSQL, an attached database on SQLite (the connection
attaches it — see [`attached_databases`](../dialects/sqlite/connecting.md#attached-databases)), a
database of the server on ClickHouse. Two models may use the same `table` in different schemas —
`Hare.generate_schemas()` orders the tables by schema and name. Migrations create and drop a schema where
the database has `CREATE SCHEMA` (`Features.supports_schemas`, PostgreSQL); elsewhere the database has to
exist. On SQLite a relation with `db_constraint=True` to a model of another schema is refused when the
model is bound — a foreign key there references only a table of its own database
(`Features.references_other_schemas`).

## <a id="app"></a>`app`

The app label in `"app.Model"` string references.

## <a id="ordering"></a>`ordering`

Tuple of ordering strings, e.g. `("-created_at", "name")`, or `F("field").asc()`/`.desc()`, or an
expression (`Lower("name").desc(nulls_last=True)`, `F("price") * F("quantity")`) — the default
`.order_by()`, also of `first()`/`last()`/`reverse()`, `iterator()`, a slice, a related manager and
`prefetch_related()`, of a query with an aggregate annotation too; a `.values()` query grouped by the
fields named leaves it out ([Implicit `GROUP BY`](../querying/aggregation.md#implicit-group-by));
`.order_by()` with no arguments drops it.

## <a id="get-latest-by"></a>`get_latest_by`

A field name or a sequence of them (`"-created_at"` for descending), e.g. `("created_at", "id")` — what
`latest()`/`earliest()` order by when given no fields. Anything else, or a name that is not a field of
the model, raises `ConfigurationError` when the model is declared.

## <a id="soft-delete-hard-cascade"></a>`soft_delete_hard_cascade`

`bool`, default `False`. Needs `soft_delete_field`. `True` makes a soft delete really delete the cascaded
rows of models without `soft_delete_field` and the auto-generated M2M through rows, as a hard delete
would; by default they are kept, so `restore()` brings the relations back — see
[Soft delete](../soft-delete-versions-tenants/soft-delete.md).

## <a id="tenant-field"></a>`tenant_field`

Name of the column holding the row's tenant — see
[Multi-tenancy](../soft-delete-versions-tenants/multi-tenancy.md). A forward
`ForeignKeyField`/`OneToOneField` name means its key column (`company` → `company_id`); a relation with a
composite key or a field without a column of its own raises `ConfigurationError`.

## <a id="returning"></a>`returning`

`bool`, default `False` — the default of the `returning` parameter of `bulk_create()`/`bulk_update()`
when a call leaves it as `None`; an explicit `True`/`False` on a call always overrides it — see
[`bulk_create()`](../querying/queryset-methods.md#bulk-create).

## <a id="table-description"></a>`table_description`

The table's description, written as its comment; when unset, the first line of the model's docstring.
SQLite keeps it as a comment inside the parentheses of `CREATE TABLE` — a migration changing it rebuilds
the table.

## <a id="manager"></a>`manager`

A `Manager()` instance customizing the default queryset (e.g. auto-filters). Fresh-copied per concrete
subclass. Whatever its `get_queryset()` filters is also applied to every JOIN to the model
(`select_related()`, `.only()`, `.order_by()`, nested `.filter()`), so a joined row is scoped exactly
like `Model.objects.all()`; only plain `.filter()`/`.exclude()` conditions on the model's own fields are
supported there (annotations, `LIMIT`, or a filter across a further relation raise `ConfigurationError`).
Its filter is not applied by `refresh_from_db()`, nor by `delete()`/`delete_preview()`'s
`CASCADE`/`SET_NULL`/`SET_DEFAULT` handling and `PROTECT`/`RESTRICT` checks — those see every row that
exists (and every row physically referencing the deleted ones), keeping only
`Meta.tenant_field`/`Meta.soft_delete_field` handling. A `Manager` assigned as a class attribute (e.g.
`all_objects = Manager()`) on the model, an abstract base or a plain mixin class is copied together with
its constructor arguments and bound to each concrete model.

## <a id="extension-options"></a>Options of extensions

An extension of hare reads options of its own from `Meta` too: `outbox_wakeup` of an
[outbox model](../integrations/outbox.md#outboxevent), `new_version_excluded_fields` of a [versioned
model](../soft-delete-versions-tenants/versioned-models.md). They describe what the extension does, not
the table, so migrations don't write them. An extension declares the options it reads with
`ExtensionModelOptions.allow("name", ...)` (`hare.models.class_building.extension_model_options`) when it
is imported.

## <a id="refused-options"></a>What `Meta` refuses

Any other `Meta` attribute — inherited from a base of `Meta` too — raises `ConfigurationError` when the
class is defined, naming the option it is closest to
(`Meta.ordring isn't a model option - did you mean Meta.ordering?`): a misspelled option would otherwise
silently do nothing. `db_table`, `app_label` and `db_table_comment` name the hare option to use instead
(`table`, `app`, `table_description`).

An option naming something takes a non-empty text, and each entry of `extensions` the name of an
extension — another value raises `ConfigurationError`: it would go into the DDL.

`default_connection` is **not** a `Meta` option — it's set at app-registration time via `Hare.init()`'s
`apps.<name>.default_connection` config key, not declared inside `class Meta`; one written in `Meta`
raises `ConfigurationError` saying so.

## <a id="primary_key"></a>Models without a primary key

An append-only log, an event stream or a columnar store's table often has no primary key.
`Meta.primary_key = None` declares such a model — no `id` is added and the table is created without a
`PRIMARY KEY`:

```python
class ReadingLog(Model):
    book = fields.ForeignKeyField("models.Book", related_name="readings")
    reader = fields.CharField(max_length=50)
    read_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        primary_key = None
```

What works: `create()`/`save()` of a new row, `bulk_create()`, every read (`filter()`, `exclude()` —
across relations too — `values()`, `aggregate()`, `annotate()`, `group_by()`, `select_related()`,
`prefetch_related()` of its forward relations, `iterator()`, `get()`, `first()`), `QuerySet.update()` and
`QuerySet.delete()` by any condition, and reaching the rows from the related model
(`book.readings.all()`, `Book.objects.filter(readings__reader=...)`, `Count("readings")`,
`readings__isnull=True`, `prefetch_related("readings")`). A condition through a relation in `exclude()`,
`update()` or `delete()` matches the written row by every column, NULLs matching NULLs — rows equal in
every column are indistinguishable anyway.

What needs a primary key raises `ConfigurationError` naming the model: `save()` of a fetched row (an
`UPDATE`), `delete()`/`restore()`/`refresh_from_db()` of one instance, a `ForeignKeyField`/`OneToOneField`
to the model without `to_field=`, and a
`ManyToManyField` on it or to it. `pk` in a filter or an ordering is a `FieldError`; `instance.pk` is
`None`, and an instance is equal only to itself. Across the relation from the other model, only
`isnull`/`not_isnull` compare the related rows as a whole (`readings__isnull=True`); `readings=`,
`readings__in=` name a row by its key and raise `FieldError`.

Migrations create and alter the table like any other (the state keeps `primary_key: None`); giving the
model a primary key or taking it away, like any change of a primary key, needs a hand-written migration.
`inspectdb` turns a table without a primary key into such a model — a unique index of the table stays a
unique index.

## <a id="table_options"></a>Table options

`Meta.table_options` lists how each dialect stores the model's table — one entry per dialect. A
connection uses the entry of its own dialect and ignores the others, so one model runs on every database
it is routed to:

```python
from hare.dialects.postgresql.postgresql_table_options import PostgresqlTableOptions
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.sqlite.sqlite_table_options import SqliteTableOptions


class Book(Model):
    isbn = fields.CharField(max_length=13, primary_key=True)
    copies = fields.IntField()

    class Meta:
        table_options = [
            SqliteTableOptions(without_rowid=True),
            PostgresqlTableOptions(unlogged=True, storage_parameters={"fillfactor": 70}),
            ClickhouseTableOptions(engine="ReplacingMergeTree", order_by=("isbn",)),
        ]
```

| Options | Option | `CREATE TABLE` |
|---|---|---|
| `SqliteTableOptions` | `without_rowid` | `WITHOUT ROWID` — the rows are stored in the primary key's index; needs a primary key the database doesn't generate. |
|  | `strict` | `STRICT` (SQLite 3.37+) — each column holds values of its declared type only — see [`strict`](#strict). |
| `PostgresqlTableOptions` | `unlogged` | `CREATE UNLOGGED TABLE` — no write-ahead log: faster writes, emptied after a crash, not replicated. |
| | `storage_parameters` | `WITH (...)` — `{"fillfactor": 70, "autovacuum_enabled": False}`; a name maps to a number, a boolean or a string. |
| | `tablespace` | `TABLESPACE name`; `None` is the database's default tablespace. |
| | `partitioning` | `PARTITION BY ...` and a table per partition — see [Partitioned tables](../dialects/postgresql/indexes-and-tables.md#partitioning). |
| `ClickhouseTableOptions` | `engine` | `ENGINE = ...` — `"MergeTree"` by default, any engine with its arguments (`"ReplacingMergeTree(version)"`). |
| | `order_by` | `ORDER BY (...)` — the fields the rows are sorted by, and [expressions](../dialects/clickhouse/models.md#expressions) of hare or `RawSQLTerm`; empty for the primary key (`tuple()` without one). A primary key must be its beginning. |
| | `partition_by` | `PARTITION BY ...` — an expression (`Trunc("viewed_at", "month")`); `None` for one partition. |
| | `ttl` | `TTL ...` — an expression of the moment a row is deleted (`F("viewed_at") + timedelta(days=365)`), or `RawSQLTerm` of the table's whole `TTL` clause; `None` for none. |
| | `settings` | `SETTINGS name = value, ...` — pairs of a name and a value. |
| | `sample_by`, `column_codecs`, `column_ttls`, `projections`, `distributed_over`, `sharding_key`, `lightweight_updates` | See [ClickHouse table options](../dialects/clickhouse/models.md#table-options). |

Two entries for one dialect, or an entry that isn't a `TableOptions`, raise `ConfigurationError`. A
dialect package declares its own options class (see [Writing a
dialect](../extending/writing-a-dialect/schema.md#table-options)).

Migrations write the entries into migration files; a change of the connection's entry is an
`AlterModelOptions` — PostgreSQL alters the table in place (`SET TABLESPACE`,
`SET LOGGED`/`SET UNLOGGED`, `SET (...)`/`RESET (...)`), SQLite rebuilds it. `hare drift` compares the
table's storage with the storage the entry of the connection's dialect gives a table, and `inspectdb`
writes a table's options into `Meta.table_options`.

### <a id="strict"></a>`strict` (SQLite)

`STRICT` (SQLite 3.37+) — each column holds values of its declared type only, a wrong value is refused by
the database; a column of another type is created as the `STRICT` type of its affinity (`INTEGER`,
`REAL`, `TEXT`, `BLOB`, `ANY`). An older SQLite raises `UnSupportedError` before any DDL; changing it
rebuilds the table.
