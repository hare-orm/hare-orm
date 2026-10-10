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
| `abstract` | No table is generated for this model; fields and `Meta` are inherited (deep-copied, not shared) by concrete subclasses. |
| `table` | DB table name override (default: derived from the model name). PostgreSQL cuts an identifier to 63 bytes: a generated table name, M2M through table or through column longer than that is shortened with a digest suffix, so two long names never become the same identifier; an explicit table, column (`source_field` or the field name), through table, index or constraint name over 63 bytes raises `ConfigurationError` on every backend. |
| `primary_key` | Only `None`: the table has no primary key and no `id` is added — see [Models without a primary key](#primary_key). A primary key is declared with `primary_key=True` on its field or with `CompositePrimaryKey`. |
| `managed` | `bool`, default `True`. `False` makes `makemigrations`/`migrate`, `Hare.generate_schemas()`, `hare drift` and `truncate_all_models()` skip this model's table entirely — it's never created, altered, or flagged as drift, as if it didn't exist to the migration system. For a model backed by something the migration system shouldn't own (a view, a table introspected at runtime) — see [`Hare.register_live_models()`](runtime-models.md). Switching an existing model's `managed` either way only alters its options in the migration state — its table is never dropped or created for it (see [Operations](../migrations/operations.md)). |
| `schema` | DB schema name (PostgreSQL). Two models may use the same `table` in different schemas — `Hare.generate_schemas()` orders tables by schema and name. SQLite has no schemas — the table is created and queried unqualified there. |
| `app` | App label, used in `"app.Model"` string references. |
| `swappable` | Name of the `swappable` config setting (e.g. `"USER_MODEL"`) through which a project can replace this model with another one — see [Swappable models](relations.md#swappable-models). Inherited from an abstract base like any other key. |
| `extensions` | Tuple of PostgreSQL extension names this model needs (e.g. `("pg_trgm",)`) — see [Migration operations — extensions](../migrations/operations.md#extensions). A field type that always needs one (`CitextField`, `PostGISField`) declares it on itself; no need to repeat it here. |
| `constraints` | Tuple of constraint objects from `hare.ddl.constraints`: `UniqueConstraint`, `CheckConstraint`, `ExclusionConstraint`. A composite `UNIQUE` is `UniqueConstraint(fields=("field_a", "field_b"))` — a name is optional (one is generated), and it can't include a `ManyToManyField`. |
| `triggers` | Tuple of `hare.ddl.Trigger` objects — created by `migrate` and by `Hare.generate_schemas()` alike. |
| `views` | List of `hare.ddl.View` — views in the model's schema, of SQL or a queryset. See [Views, functions, sequences and access](schema-objects.md#views). |
| `materialized_views` | List of `hare.ddl.MaterializedView` — see [Materialized views](schema-objects.md#materialized-views). |
| `dictionaries` | List of `ClickhouseDictionary` — dictionaries of the model's rows, read by key (ClickHouse). See [Dictionaries](../dialects/clickhouse/schema-objects.md#dictionaries). |
| `functions` | List of `hare.ddl.DatabaseFunction` — functions stored in the database. See [Functions](schema-objects.md#functions). |
| `sequences` | List of `hare.ddl.DatabaseSequence` — created before the table. See [Sequences](schema-objects.md#sequences). |
| `row_level_security` | `RowLevelSecurity.ENABLED`, `RowLevelSecurity.FORCED` or `None` (off, the default). See [Row level security](schema-objects.md#row-level-security). |
| `policies` | List of `hare.ddl.Policy` — row level security policies of the table. |
| `grants` | List of `hare.ddl.Grant` — privileges on the table or on the model's views, sequences and functions. See [Grants](schema-objects.md#grants). |
| `indexes` | Tuple of `hare.ddl.indexes.Index`/`PartialIndex` instances, or plain tuples of field names. |
| `ordering` | Tuple of ordering strings, e.g. `("-created_at", "name")` — the default `.order_by()`; `.order_by()` with no arguments drops it. |
| `get_latest_by` | A field name or a sequence of them (`"-created_at"` for descending), e.g. `("created_at", "id")` — what `latest()`/`earliest()` order by when given no fields. Anything else raises `ConfigurationError` when the model is declared. |
| `soft_delete_field` | Name of a nullable `DatetimeField` — see [Soft delete](../soft-delete-versions-tenants/soft-delete.md). |
| `soft_delete_hard_cascade` | `bool`, default `False`. Needs `soft_delete_field`. `True` makes a soft delete really delete the cascaded rows of models without `soft_delete_field` and the auto-generated M2M through rows, as a hard delete would; by default they are kept, so `restore()` brings the relations back — see [Soft delete](../soft-delete-versions-tenants/soft-delete.md). |
| `optimistic_lock_field` | Name of a non-nullable `IntField` used for optimistic locking, bumped on every `save()`. Can't also be a PK component or a DB-generated field. |
| `tenant_field` | Name of the column holding the row's tenant — see [Multi-tenancy](../soft-delete-versions-tenants/multi-tenancy.md). A forward `ForeignKeyField`/`OneToOneField` name means its key column (`company` → `company_id`); a relation with a composite key or a field without a column of its own raises `ConfigurationError`. |
| `tenant_schema` | `True` keeps the table in each tenant's own schema, on a connection with `tenant_schema_template` — see [Schema per tenant](../soft-delete-versions-tenants/schema-per-tenant.md). Not with `schema`; a shared model can't relate to such a model (`ConfigurationError`). |
| `track_dirty_fields` | `bool`, default `False` — opt-in per-row snapshot enabling `get_dirty_fields()`. |
| `change_capture` | A `ChangeSink` — `hare.contrib.outbox.ChangeCapture(...)`: every ORM write of the model writes its changed rows to the transactional outbox in its own transaction; see [Capturing a model's changes](../integrations/outbox.md#change-capture). Inherited from an abstract base model. |
| `returning` | `bool`, default `False` — default for `bulk_create()`/`bulk_update()`'s own `returning` parameter when a call leaves it as `None`; an explicit `True`/`False` on a given call always overrides it. See [QuerySet methods](../querying/queryset-methods.md#method-reference). |
| `table_description` | Table-level description/comment (auto-filled from the model's docstring first line if unset). |
| `table_options` | How each dialect stores the table — `SqliteTableOptions`, `PostgresqlTableOptions`, `ClickhouseTableOptions`, one entry per dialect; see [Table options](#table_options). |
| `manager` | A `Manager()` instance customizing the default queryset (e.g. auto-filters). Fresh-copied per concrete subclass. Whatever its `get_queryset()` filters is also applied to every JOIN to the model (`select_related()`, `.only()`, `.order_by()`, nested `.filter()`), so a joined row is scoped exactly like `Model.objects.all()`; only plain `.filter()`/`.exclude()` conditions on the model's own fields are supported there (annotations, `LIMIT`, or a filter across a further relation raise `ConfigurationError`). Its filter is not applied by `refresh_from_db()`, nor by `delete()`/`delete_preview()`'s `CASCADE`/`SET_NULL`/`SET_DEFAULT` handling and `PROTECT`/`RESTRICT` checks — those see every row that exists (and every row physically referencing the deleted ones), keeping only `Meta.tenant_field`/`Meta.soft_delete_field` handling. A `Manager` assigned as a class attribute (e.g. `all_objects = Manager()`) on the model, an abstract base or a plain mixin class is copied together with its constructor arguments and bound to each concrete model. |

An extension of hare reads options of its own from `Meta` too: `outbox_wakeup` of an
[outbox model](../integrations/outbox.md#outboxevent), `new_version_excluded_fields` of a
[versioned model](../soft-delete-versions-tenants/versioned-models.md). They describe what the
extension does, not the table, so migrations don't write them.

`default_connection` is **not** a `Meta` option — it's set at app-registration time via
`Hare.init()`'s `apps.<name>.default_connection` config key, not declared inside `class Meta`; one
written in `Meta` is ignored.

## <a id="primary_key"></a>Models without a primary key

An append-only log, an event stream or a columnar store's table often has no primary key.
`Meta.primary_key = None` declares such a model — no `id` is added and the table is created
without a `PRIMARY KEY`:

```python
class VisitLog(Model):
    venue = fields.ForeignKeyField("models.Venue", related_name="visits")
    visitor = fields.CharField(max_length=50)
    visited_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        primary_key = None
```

What works: `create()`/`save()` of a new row, `bulk_create()`, every read (`filter()`, `exclude()`
— across relations too — `values()`, `aggregate()`, `annotate()`, `group_by()`, `select_related()`,
`prefetch_related()` of its forward relations, `iterator()`, `get()`, `first()`),
`QuerySet.update()` and `QuerySet.delete()` by any condition, and reaching the rows from the related
model (`venue.visits.all()`, `Venue.objects.filter(visits__visitor=...)`, `Count("visits")`,
`visits__isnull=True`, `prefetch_related("visits")`). A condition through a relation in `exclude()`,
`update()` or `delete()` matches the written row by every column, NULLs matching NULLs — rows equal
in every column are indistinguishable anyway.

What needs a primary key raises `ConfigurationError` naming the model: `save()` of a fetched row (an
`UPDATE`), `delete()`/`restore()`/`refresh_from_db()` of one instance,
`update()`/`delete()` of a sliced or ordered queryset, a `ForeignKeyField`/`OneToOneField` to the
model without `to_field=`, and a `ManyToManyField` on it or to it. `pk` in a filter or an ordering is
a `FieldError`; `instance.pk` is `None`, and an instance is equal only to itself. Across the relation
from the other model, only `isnull`/`not_isnull` compare the related rows as a whole
(`visits__isnull=True`); `visits=`, `visits__in=` name a row by its key and raise `FieldError`.

Migrations create and alter the table like any other (the state keeps `primary_key: None`);
giving the model a primary key or taking it away, like any change of a primary key, needs a
hand-written migration.
`inspectdb` turns a table with neither a primary key nor a unique index over every column into such
a model.

## <a id="table_options"></a>Table options

`Meta.table_options` lists how each dialect stores the model's table — one entry per dialect. A
connection uses the entry of its own dialect and ignores the others, so one model runs on every
database it is routed to:

```python
from hare.dialects.postgresql.postgresql_table_options import PostgresqlTableOptions
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.sqlite.sqlite_table_options import SqliteTableOptions


class Measurement(Model):
    sensor = fields.CharField(max_length=40, primary_key=True)
    value = fields.FloatField()

    class Meta:
        table_options = [
            SqliteTableOptions(without_rowid=True),
            PostgresqlTableOptions(unlogged=True, storage_parameters={"fillfactor": 70}),
            ClickhouseTableOptions(engine="ReplacingMergeTree", order_by=("sensor",)),
        ]
```

| Options | Option | `CREATE TABLE` |
|---|---|---|
| `SqliteTableOptions` | `without_rowid` | `WITHOUT ROWID` — the rows are stored in the primary key's index; needs a primary key the database doesn't generate. |
| | `strict` | `STRICT` (SQLite 3.37+) — each column holds values of its declared type only, a wrong value is refused by the database; a column of another type is created as the `STRICT` type of its affinity (`INTEGER`, `REAL`, `TEXT`, `BLOB`, `ANY`). An older SQLite raises `UnSupportedError` before any DDL; changing it rebuilds the table. |
| `PostgresqlTableOptions` | `unlogged` | `CREATE UNLOGGED TABLE` — no write-ahead log: faster writes, emptied after a crash, not replicated. |
| | `storage_parameters` | `WITH (...)` — `{"fillfactor": 70, "autovacuum_enabled": False}`; a name maps to a number, a boolean or a string. |
| | `tablespace` | `TABLESPACE name`; `None` is the database's default tablespace. |
| | `partitioning` | `PARTITION BY ...` and a table per partition — see [Partitioned tables](#partitioning). |
| `ClickhouseTableOptions` | `engine` | `ENGINE = ...` — `"MergeTree"` by default, any engine with its arguments (`"ReplacingMergeTree(version)"`). |
| | `order_by` | `ORDER BY (...)` — the fields the rows are sorted by, and `RawSQLTerm` expressions; empty for the primary key (`tuple()` without one). A primary key must be its beginning. |
| | `partition_by` | `PARTITION BY ...` — `RawSQLTerm` of an SQL expression (`RawSQLTerm("toYYYYMM(viewed_at)")`); `None` for one partition. |
| | `ttl` | `TTL ...` — `RawSQLTerm` of the table's `TTL` clause; `None` for none. |
| | `settings` | `SETTINGS name = value, ...` — pairs of a name and a value. |
| | `sample_by`, `column_codecs`, `column_ttls`, `projections`, `distributed_over`, `sharding_key`, `lightweight_updates` | See [ClickHouse table options](../dialects/clickhouse/models.md#table-options). |

Two entries for one dialect, or an entry that isn't a `TableOptions`, raise `ConfigurationError`. A
dialect package declares its own options class (see [Writing a dialect](../extending/writing-a-dialect.md#table-options)).

Migrations write the entries into migration files; a change of the connection's entry is an
`AlterModelOptions` — PostgreSQL alters the table in place (`SET TABLESPACE`,
`SET LOGGED`/`SET UNLOGGED`, `SET (...)`/`RESET (...)`), SQLite rebuilds it. `hare drift` compares
the entry of the connection's dialect with what the table was created with — a tablespace named
explicitly that is the database's default counts as the default — and `inspectdb` writes a table's
options into `Meta.table_options`.

### <a id="partitioning"></a>Partitioned tables (PostgreSQL)

`PostgresqlTableOptions(partitioning=...)` makes the table a partitioned one: it is created
`PARTITION BY <strategy> (<key columns>)`, and each partition as a table of its own named
`<table>_<partition name>` (within 63 bytes, like every name hare generates). The classes live in
`hare.dialects.postgresql.partitioning`:

```python
import datetime

from hare import fields
from hare.dialects.postgresql.partitioning import (
    HashPartitioning, ListPartition, ListPartitioning, RangeBound, RangePartition, RangePartitioning,
)
from hare.dialects.postgresql.postgresql_table_options import PostgresqlTableOptions
from hare.models import Model


class UnreadReport(Model):
    user_id = fields.BigIntField()
    report_id = fields.BigIntField()
    pk = fields.CompositePrimaryKey("user_id", "report_id")

    class Meta:
        table_options = [
            PostgresqlTableOptions(
                # unreadreport_p0 ... unreadreport_p31, FOR VALUES WITH (MODULUS 32, REMAINDER i)
                partitioning=HashPartitioning(fields=("user_id",), partition_count=32),
                storage_parameters={"autovacuum_vacuum_scale_factor": 0.02},
            )
        ]


class RegionSale(Model):
    region = fields.CharField(max_length=10)
    number = fields.IntField()
    pk = fields.CompositePrimaryKey("region", "number")

    class Meta:
        table_options = [
            PostgresqlTableOptions(
                partitioning=ListPartitioning(
                    fields=("region",),
                    partitions=[
                        ListPartition("west", values=["us", "ca"]),  # regionsale_west FOR VALUES IN ('us', 'ca')
                        ListPartition("east", values=["jp"]),
                    ],
                    default_partition="other",  # regionsale_other DEFAULT - every other region
                )
            )
        ]


class DailyEvent(Model):
    day = fields.DateField()
    number = fields.IntField()
    pk = fields.CompositePrimaryKey("day", "number")

    class Meta:
        table_options = [
            PostgresqlTableOptions(
                partitioning=RangePartitioning(
                    fields=("day",),
                    partitions=[
                        RangePartition("old", from_values=(RangeBound.MINVALUE,), to_values=(datetime.date(2026, 1, 1),)),
                        RangePartition(
                            "y2026", from_values=(datetime.date(2026, 1, 1),), to_values=(datetime.date(2027, 1, 1),)
                        ),
                    ],
                    default_partition="later",
                )
            )
        ]
```

| Class | Arguments | Partitions |
|---|---|---|
| `HashPartitioning` | `fields`, `partition_count` (1 to 1024) | `<table>_p0` ... `<table>_p<n-1>`, rows spread evenly by a hash of the key. |
| `ListPartitioning` | `fields` (one), `partitions` (`ListPartition(name, values)`), `default_partition` | Each partition holds the rows of the key values it lists; `None` among them means the rows with a `NULL` key. |
| `RangePartitioning` | `fields`, `partitions` (`RangePartition(name, from_values, to_values)`), `default_partition` | Each partition holds the rows from its lower bound up to, not including, its upper one — one value per key column. `RangeBound.MINVALUE`/`RangeBound.MAXVALUE` stand below/above every value. |

`fields` name the key: fields with a column, or a foreign key, which stands for its key column(s).
`default_partition` is the name of the partition for the rows no other one holds; without it, a row
no partition holds is refused by the database (`IntegrityError`). The order the partitions are
listed in doesn't matter.

What PostgreSQL requires is checked before any DDL is written — `ConfigurationError` names the
problem:

- the key fields exist and have columns;
- the primary key, every unique field, `UniqueConstraint`, unique `Index` and `ExclusionConstraint`
  (with `=`) includes every key column — PostgreSQL enforces each within one partition. A model
  whose key is one generated `id` can only be partitioned by that `id`;
- a partitioned table can't be `unlogged`;
- the partitions' names differ, a `ListPartitioning` value is listed once, `RangePartition`s don't
  overlap and give one value per key column.

An `ExclusionConstraint` on a partitioned table needs PostgreSQL 17 (`UnSupportedError` on an
older server). Foreign keys to and from a partitioned table work as on a plain one.

A partitioned table holds no rows itself, so `storage_parameters` are set on each partition (at
creation and when they change), and `tablespace` on the table — where new partitions go — and on
each partition. Indexes and constraints are created on the table and reach every partition. A
concurrent index (`AddIndex(concurrently=True)`) can't be built on a partitioned table as a whole:
hare creates it on the table alone, builds it concurrently on each partition and attaches each —
it is valid once the last one is attached; dropping it is never concurrent.

How a change reaches the database:

| Change | Migration |
|---|---|
| A `ListPartition`/`RangePartition` or the default partition added | `AddPartition` — a new, empty partition. PostgreSQL refuses it while the default partition holds rows that belong to it. |
| One removed | `RemovePartition` — the partition is detached and dropped **with its rows**; `makemigrations` warns. |
| A partition's values or bounds changed | `RemovePartition` and `AddPartition` of the same name — its rows are lost. |
| `storage_parameters`, `tablespace` | `AlterModelOptions`, in place on every partition. |
| Partitioning added or removed, another strategy, key or `partition_count` | `AlterModelOptions` that creates the table anew: the rows are copied, the indexes, constraints, comments, triggers and the foreign keys of other tables referencing it are set again. It rewrites the whole table under an exclusive lock. |
| `Meta.table` changed, `RenameModel` | The partitions are renamed with the table. |

On SQLite (and any other dialect) the `PostgresqlTableOptions` entry is not used: the table is a
plain one, and the partition operations only change the migration state there.
