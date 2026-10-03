# Operations (`hare.migrations.operations`)

**Model-level**
```python
CreateModel(name: str, fields: list[tuple[str, FieldLike]], options: dict | None = None, bases: list[str] | None = None, state_only: bool = False)
RenameModel(old_name: str, new_name: str)
DeleteModel(name: str, state_only: bool = False)
AlterModelOptions(name: str, options: dict)
AlterModelTable(name: str, table: str)
```

**Partitions**
```python
AddPartition(model_name: str, partition: Any)
RemovePartition(model_name: str, partition: Any)
```

**Field-level**
```python
AddField(model_name: str, name: str, field: FieldLike)
RemoveField(model_name: str, name: str)
AlterField(model_name: str, name: str, field: FieldLike)
RenameField(model_name: str, old_name: str, new_name: str, field: FieldLike | None = None)
BackfillColumn(model_name: str, field_name: str, value: Any | Callable[[], Any], *, batch_size: int = 1000)
AlterColumnNotNullSafe(model_name: str, field_name: str)
```

`RenameField`'s `field` is the field under its new name, when the rename changes it too; without it
the field is kept as it was.

`AddField` of a NOT NULL field whose only default is a Python `default=` (no `db_default`) works
on a table that already holds rows: the column is added nullable, every existing row is set to the
default, then the column becomes NOT NULL (`ALTER COLUMN ... SET NOT NULL` on Postgres, a table
rebuild on SQLite). A callable default is evaluated **once**, so every existing row gets that same
value — for a per-row value (e.g. `uuid4` on a unique column) add the column nullable, fill it in a
`RunPython` step, then make it NOT NULL. A NOT NULL `DatetimeField`/`TimeField` with
`auto_now=True`/`auto_now_add=True` (and no `default`/`db_default`) is filled the same way, with the
current time written exactly as a regular `save()` writes it.

`AlterField` changing the column type of a primary key (e.g. `IntField` to `BigIntField`) or of
another column a relation targets through `to_field=` changes the key columns referencing it
too: every ForeignKeyField/OneToOneField column and every automatic ManyToManyField through-table
column. On Postgres their foreign key constraints are dropped first and re-created once every
column has the new type; on SQLite the referencing tables are rebuilt. The values are converted
with a plain cast, so a type pair Postgres can't cast (`integer` to `uuid`) still fails.

`AlterField` to a column type that can't hold a value the table already stores raises
`FieldNarrowingDataLossError` before anything changes, on every dialect: a shorter `max_length`
(or a text, number or boolean turned into a `CharField` too short for its text), a `DecimalField`
with fewer decimal places or whole digits, a smaller integer type, and a float or decimal turned
into an integer or a decimal it doesn't fit. Postgres would otherwise cut or round the value in the
cast, and SQLite would keep it beyond the column's declared size. Fix or widen those values first;
a narrower type every stored value fits in alters as usual.

`AlterField` changing a relation's `to_field=` repoints its key column (and foreign key
constraint) at the new target column. Stored key values name rows through the old target column
and can't be translated, so the migration raises `ForeignKeyTargetChangeError` while any row
stores one - add a new relation field targeting the new column, fill it with
`RunPython`/`RunSQL`, then remove the old field. `makemigrations` prints the same warning.

Switching a `ManyToManyField` from hare's automatic through table to `through=SomeModel` (or back)
copies the relation's rows into the new table and drops the automatic one. When both use the same
table name, `makemigrations` first renames the automatic table to `<table>__swap`, so the through
model's table can be created under that name. The through model's other columns of the copied rows
get their Python `default` (a callable one is evaluated once, so every row gets that same value -
refused with `ConfigurationError` for a unique column), the current time for
`auto_now`/`auto_now_add`, or their `db_default`; a column with none of them must be nullable.

A model moved to another app with its table kept as it is (same table name, fields and options)
becomes a `CreateModel(..., state_only=True)` in the new app and a
`DeleteModel(..., state_only=True)` in the old one: both only change the migration state, the
table and its rows stay, and relations pointing at the model are repointed without DDL. The new
app's migration depends on the old app's migrations. A moved model whose fields or options also
change in the same run is refused - move it unchanged first, then change it.

Deleting a model referenced from another app makes the deleting migration depend on the other
app's new migration that removes or repoints the reference. `makemigrations` for the deleting app
alone is refused while the other app's migrations still reference the model. Migration files that
can't be replayed in order (a relation left pointing at a deleted or never created model) are
reported by every command as a plain error naming the relation.

A model deleted in the same run in which a new relation takes over one of its backward accessors (e.g.
`Book` replaced by `Novel`, both with `related_name="books"` on `Author`) is deleted before the
new model is created; while another model still references it until later in the migration, only
its conflicting relation field is removed first. An added model with exactly the same fields and
options as a removed model of the same app that isn't recognized as its rename (e.g. two identical
models renamed at once) gets a "possible unrecognized rename" warning from `makemigrations` - the
generated `CreateModel`/`DeleteModel` pair drops the old table's rows; replace it with
`RenameModel` by hand if it's a rename.

`AddPartition`/`RemovePartition` add and remove one partition of a
[partitioned table](../models/meta-options.md#partitioning) - a `ListPartition`, `RangePartition` or
`DefaultPartition` of the model's `PostgresqlTableOptions`. `makemigrations` writes one per partition
that came or went, so the migration file shows which. `AddPartition` creates an empty partition with
the table's storage parameters and tablespace. `RemovePartition` detaches the partition and drops it
**with its rows** - `makemigrations` warns about it; a partition whose values or bounds changed is
removed and added again. Both are reversible: unapplying `AddPartition` removes the partition with
the rows it got meanwhile, unapplying `RemovePartition` adds it back empty. On a connection of
another dialect they only change the state. Everything else about how a table is partitioned -
partitioning added or removed, another strategy, key or count of hash partitions - PostgreSQL can't
change in place: that `AlterModelOptions` creates the table anew and copies its rows (its indexes,
constraints, comments, triggers and the foreign keys of other tables referencing it are set again),
rewriting the whole table under an exclusive lock.

`AlterModelOptions.options` is the model's whole set of options that have no operation of their
own (everything except the table, schema, indexes, constraints, triggers and primary key) - an option left out of it is removed from the model. A changed `table_description`
updates the table comment on Postgres.

Switching an existing model to `Meta.managed = False` generates an `AlterModelOptions` that keeps
the model in the migration state with `managed: False`: its table and rows stay, and while it
stays unmanaged no further change of it touches the table. `CreateModel`/`DeleteModel` of a model
whose state is unmanaged runs no DDL either, so removing such a model from the code leaves its
table in place. Switching it back to managed is again an `AlterModelOptions`, followed by whatever
field/index changes the model got in between. A model that was never managed by the migrations
(unmanaged from the start) isn't part of the migration state at all - making it managed generates a
`CreateModel`; if its table already exists, apply that migration with `migrate --fake`.

`AlterModelSchema(name: str, schema: str | None)` moves the model's table (`ALTER TABLE ... SET
SCHEMA`, Postgres only - a no-op on SQLite) together with the automatic through tables of the
model's own `ManyToManyField`s, which live in the schema of the model declaring them. `schema=None`
moves them into the connection's current schema, where a table without `Meta.schema` is created.

**Indexes**
```python
AddIndex(model_name: str, index: Index, *, concurrently: bool = False)
RemoveIndex(model_name: str, name: str | None = None, fields: list[str] | None = None, *, concurrently: bool = False)
RenameIndex(model_name: str, new_name: str, *, old_name: str | None = None, old_fields: list[str] | None = None)
```

`concurrently=True` builds or drops the index without blocking writes to the table - Postgres
`CREATE INDEX CONCURRENTLY`/`DROP INDEX CONCURRENTLY`, which can't run inside a transaction: set
`atomic = False` on the migration, or the operation raises `ConfigurationError`. `makemigrations`
writes plain `AddIndex`/`RemoveIndex`; add `concurrently=True` by hand for a big, busy table. SQLite
builds and drops the index the plain way.

```python
class Migration(migrations.Migration):
    atomic = False
    operations = [
        ops.AddIndex("Order", Index(fields=["placed_at"], name="orders_placed_idx"), concurrently=True),
    ]
```

`RemoveIndex(name=...)` also finds an unnamed expression-based index (e.g. `Index(Lower("name"))`)
by the name it was created under (`idx_<table>_expr_<hash>`, `uidx_...` for a unique one) -
`makemigrations` writes that name when such an index is removed or changed.

**Constraints**
```python
AddConstraint(
    model_name: str,
    constraint: UniqueConstraint | CheckConstraint | ExclusionConstraint,
    *,
    not_valid: bool = False,
)
RemoveConstraint(model_name: str, name: str | None = None, fields: list[str] | None = None)
RenameConstraint(model_name: str, old_name: str, new_name: str)
ValidateConstraint(model_name: str, name: str)
```

Every constraint lives in the model's `Meta.constraints`, an unnamed `UniqueConstraint(fields=...)`
included - `RemoveConstraint(fields=[...])` finds that one by its fields.

`AddConstraint(..., not_valid=True)` adds a CHECK constraint only new and updated rows must pass
(Postgres `NOT VALID`), without scanning and locking a big table; a later `ValidateConstraint` checks
the existing rows with a lock that doesn't block writes, failing with `IntegrityError` while one
breaks it. `not_valid=True` takes a `CheckConstraint` only (`ConfigurationError` otherwise). The
model's state holds the constraint as declared either way, and drift doesn't flag the NOT VALID one;
inspectdb notes it. SQLite adds the constraint the plain way (checking every row) and has nothing to
validate.

**Triggers**
```python
AddTrigger(model_name: str, trigger: Trigger)
RemoveTrigger(model_name: str, name: str)
AlterTrigger(model_name: str, trigger: Trigger)
RenameTrigger(model_name: str, old_name: str, new_name: str)
```

**Raw operations**
```python
RunPython(code, reverse_code=None, *, atomic: bool | None = None)   # code(apps, schema_editor) -> None | Awaitable[None]
RunSQL(sql, reverse_sql=None, *, atomic: bool | None = None)        # a string, list of strings, or list of (sql, params)
SQLOperation(query: str, values: list)                               # one statement with bind parameters; irreversible
CreateSchema(schema_name: str)
DropSchema(schema_name: str)
CreateExtension(extension_name: str)   # Postgres only
RemoveExtension(extension_name: str)   # Postgres only
CreateCollation(name: str, locale: str, *, provider: str = "libc", deterministic: bool = True)   # Postgres only
RemoveCollation(name: str, locale: str, *, provider: str = "libc", deterministic: bool = True)   # Postgres only
```

`CreateCollation` creates a collation for `Collate(...)` and `db_collation=` - e.g. a case-insensitive
ICU one: `CreateCollation("case_insensitive", "und-u-ks-level2", provider="icu", deterministic=False)`.
`RemoveCollation` takes the same arguments, to create it again going back. SQLite has no collation DDL -
both are skipped there with a warning.

`RunPython.noop` is a ready-made no-op you can use as `reverse_code` for a one-way data migration.

## How an operation is written to a file {: #operation-deconstruct }

Indexes, constraints and triggers are one type of thing to the migrations - a named schema object of
a model: `AddIndex`/`AddConstraint`/`AddTrigger`, the `Remove*`, `Rename*` and `AlterTrigger`
operations are thin subclasses of four generic ones (`AddSchemaObject`, `RemoveSchemaObject`,
`RenameSchemaObject`, `AlterSchemaObject` in `hare.migrations.operations.schema_objects`), and the
autodetector diffs all three with one comparison.

Every operation - yours included - is written into a migration file from `Operation.deconstruct()`,
which returns `(class path, positional args, keyword args)`; the writer renders that as the call.
The default takes the constructor's parameters from attributes of the same name and leaves out a
keyword argument still at its default, so an operation whose `__init__` stores each argument under
its own name needs nothing more:

```python
from hare.migrations.operations import Operation


class CreateMaterializedView(Operation):
    def __init__(self, name: str, query: str, *, with_data: bool = True) -> None:
        self.name = name
        self.query = query
        self.with_data = with_data
    ...
```

Override `deconstruct()` only when the arguments aren't stored as given.

## Extensions {: #extensions }

Declare which Postgres extensions a model needs via `Meta.extensions`, instead of hand-writing
`RunSQL("CREATE EXTENSION IF NOT EXISTS ...")`:

```python
class Widget(Model):
    ...
    class Meta:
        extensions = ("pg_trgm",)
```

A field type that always needs one (`CitextField` needs `citext`, `PostGISField` needs `postgis`)
declares it on itself — you don't need to repeat it in `Meta.extensions`. The autodetector
re-derives the full set of extensions a migration needs straight from `Meta.extensions`/
`field.requires_extension` every time it diffs two model states — the same way it derives schema
presence from `Meta.schema` rather than tracking either as separate stored state — and emits
`CreateExtension`/`RemoveExtension` operations for whatever changed, ordered so a `CREATE EXTENSION`
runs before any table that depends on it, and a `DROP EXTENSION` only after every dependent table is
gone.

## Constraints & triggers as objects {: #constraints-and-triggers }

See [Constraints](../models/constraints-and-triggers.md#constraints) and
[Triggers](../models/constraints-and-triggers.md#triggers) for `UniqueConstraint`, `CheckConstraint`,
`ExclusionConstraint`, and `Trigger` — the same dataclasses used in `Meta.constraints`/`Meta.triggers`
are what you pass to `AddConstraint`/`AddTrigger` when hand-writing a migration.

## Indexes (`hare.ddl.indexes` / `hare.dialects.postgresql.indexes`) {: #indexes }

See [Indexes](../models/indexes.md) for `Index`/`PartialIndex`, and
[PostgreSQL index types](../models/indexes.md#postgresql-index-types) for `GinIndex`/`GistIndex`/`BrinIndex`/`BloomIndex`/
`HashIndex`/`SpGistIndex`.
