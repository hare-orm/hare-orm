# Operations

The operations a migration file lists, from `hare.migrations.operations` (`from hare.migrations import
operations as ops`): what each one takes, what it does in the database and in the migration state,
and how to write one of your own.

## <a id="model-operations"></a>Models

```python
CreateModel(name: str, fields: list[tuple[str, FieldLike]], options: dict | None = None, bases: list[str] | None = None, state_only: bool = False)
RenameModel(old_name: str, new_name: str)
DeleteModel(name: str, state_only: bool = False)
AlterModelOptions(name: str, options: dict)
AlterModelTable(name: str, table: str)
```

## <a id="partition-operations"></a>Partitions

```python
AddPartition(model_name: str, partition: Any)
RemovePartition(model_name: str, partition: Any)
```

## <a id="field-operations"></a>Fields

```python
AddField(model_name: str, name: str, field: FieldLike, *, not_valid: bool = False)
RemoveField(model_name: str, name: str)
AlterField(model_name: str, name: str, field: FieldLike)
RenameField(model_name: str, old_name: str, new_name: str, field: FieldLike | None = None)
BackfillColumn(model_name: str, field_name: str, value: Any | Callable[[], Any], *, batch_size: int = 1000)
AlterColumnNotNullSafe(model_name: str, field_name: str)
```

`RenameField`'s `field` is the field under its new name, when the rename changes it too; without it
the field is kept as it was.

`AddField(..., not_valid=True)` of a `ForeignKeyField`/`OneToOneField` with a database constraint adds
the column, then its FOREIGN KEY `NOT VALID` (PostgreSQL) under the name the inline `REFERENCES` would
have: only new and updated rows must satisfy it, so adding it doesn't scan the table while the
writes to both tables wait; a later `ValidateConstraint(model_name, <the foreign key's name>)` checks
the existing rows with a lock that doesn't block writes. Another field, or a relation with
`db_constraint=False`, raises `ConfigurationError`. SQLite never checks the existing rows as it adds
the column, so the option changes nothing there.

`RemoveField` on SQLite drops the column with `ALTER TABLE ... DROP COLUMN` (SQLite 3.35.5 and newer,
`features.supports_drop_column`) when nothing keeps SQLite from it: the field isn't a key, unique,
indexed, a relation or generated; the model has no CHECK constraint, generated column or trigger and
no other index or unique constraint names the field; and the database holds no index on the column
(nor a partial or expression index on the table), no CHECK in the table, no trigger on it and no
view naming the column. Otherwise the table is rebuilt from the model
the migrations know: created anew, the rows copied, its indexes, unique constraints and triggers
created again. A rebuild keeps only the columns the migration state has, so a column the state no
longer knows (removed with [`SeparateDatabaseAndState`](#separate-database-and-state)) is dropped
by the next rebuild of its table; an index, trigger or view created by `RunSQL` on the table isn't
created again either.

`AddField` of a NOT NULL field whose only default is a Python `default=` (no `db_default`) works
on a table that already holds rows: the column is added nullable, every existing row is set to the
default, then the column becomes NOT NULL (`ALTER COLUMN ... SET NOT NULL` on PostgreSQL, a table
rebuild on SQLite). A callable default is evaluated **once**, so every existing row gets that same
value — for a per-row value (e.g. `uuid4` on a unique column) add the column nullable, fill it in a
`RunPython` step, then make it NOT NULL. A NOT NULL `DatetimeField`/`TimeField` with
`auto_now=True`/`auto_now_add=True` (and no `default`/`db_default`) is filled the same way, with the
current time written exactly as a regular `save()` writes it.

`AlterField` changing the column type of a primary key (e.g. `IntField` to `BigIntField`) or of
another column a relation targets through `to_field=` changes the key columns referencing it
too: every ForeignKeyField/OneToOneField column and every automatic ManyToManyField through-table
column. On PostgreSQL their foreign key constraints are dropped first and re-created once every
column has the new type; on SQLite the referencing tables are rebuilt. The values are converted
with a plain cast, so a type pair PostgreSQL can't cast (`integer` to `uuid`) still fails.

`AlterField` to a column type that can't hold a value the table already stores raises
`FieldNarrowingDataLossError` before anything changes, on every dialect: a shorter `max_length`
(or a text, number or boolean turned into a `CharField` too short for its text), a `DecimalField`
with fewer decimal places or whole digits, a smaller integer type, and a float or decimal turned
into an integer or a decimal it doesn't fit. PostgreSQL would otherwise cut or round the value in the
cast, and SQLite would keep it beyond the column's declared size. Fix or widen those values first;
a narrower type every stored value fits in alters as usual.

`AlterField` changing a relation's `to_field=` repoints its key column (and foreign key
constraint) at the new target column. Stored key values name rows through the old target column
and can't be translated, so the migration raises `ForeignKeyTargetChangeError` while any row
stores one — add a new relation field targeting the new column, fill it with
`RunPython`/`RunSQL`, then remove the old field. `makemigrations` prints the same warning.

Switching a `ManyToManyField` from hare's automatic through table to `through=SomeModel` (or back)
copies the relation's rows into the new table and drops the automatic one. When both use the same
table name, `makemigrations` first renames the automatic table to `<table>__swap`, so the through
model's table can be created under that name. The through model's other columns of the copied rows
get their Python `default` (a callable one is evaluated once, so every row gets that same value —
refused with `ConfigurationError` for a unique column), the current time for
`auto_now`/`auto_now_add`, or their `db_default`; a column with none of them must be nullable.

A model moved to another app with its table kept as it is (same table name, fields and options)
becomes a `CreateModel(..., state_only=True)` in the new app and a
`DeleteModel(..., state_only=True)` in the old one: both only change the migration state, the
table and its rows stay, and relations pointing at the model are repointed without DDL. The new
app's migration depends on the old app's migrations. A moved model whose fields or options also
change in the same run is refused — move it unchanged first, then change it.

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
models renamed at once) gets a "possible unrecognized rename" warning from `makemigrations` — the
generated `CreateModel`/`DeleteModel` pair drops the old table's rows; replace it with
`RenameModel` by hand if it's a rename.

`AddPartition`/`RemovePartition` add and remove one partition of a
[partitioned table](../models/meta-options.md#partitioning) — a `ListPartition`, `RangePartition` or
`DefaultPartition` of the model's `PostgresqlTableOptions`. `makemigrations` writes one per partition
that came or went, so the migration file shows which. `AddPartition` creates an empty partition with
the table's storage parameters and tablespace. `RemovePartition` detaches the partition and drops it
**with its rows** — `makemigrations` warns about it; a partition whose values or bounds changed is
removed and added again. Both are reversible: unapplying `AddPartition` removes the partition with
the rows it got meanwhile, unapplying `RemovePartition` adds it back empty. On a connection of
another dialect they only change the state. Everything else about how a table is partitioned —
partitioning added or removed, another strategy, key or count of hash partitions — PostgreSQL can't
change in place: that `AlterModelOptions` creates the table anew and copies its rows (its indexes,
constraints, comments, triggers and the foreign keys of other tables referencing it are set again),
rewriting the whole table under an exclusive lock.

`AlterModelOptions.options` is the model's whole set of options that have no operation of their
own (everything except the table, schema, indexes, constraints, triggers, primary key, views, materialized views, functions, sequences, row level security, policies and grants) — an option left out of it is removed from the model. A changed `table_description`
updates the table comment on PostgreSQL.

Switching an existing model to `Meta.managed = False` generates an `AlterModelOptions` that keeps
the model in the migration state with `managed: False`: its table and rows stay, and while it
stays unmanaged no further change of it touches the table. `CreateModel`/`DeleteModel` of a model
whose state is unmanaged runs no DDL either, so removing such a model from the code leaves its
table in place. Switching it back to managed is again an `AlterModelOptions`, followed by whatever
field/index changes the model got in between. A model that was never managed by the migrations
(unmanaged from the start) isn't part of the migration state at all — making it managed generates a
`CreateModel`; if its table already exists, apply that migration with `migrate --fake`.

`AlterModelSchema(name: str, schema: str | None)` moves the model's table (`ALTER TABLE ... SET
SCHEMA`, PostgreSQL only — a no-op on SQLite) together with the automatic through tables of the
model's own `ManyToManyField`s, which live in the schema of the model declaring them. `schema=None`
moves them into the connection's current schema, where a table without `Meta.schema` is created.

## <a id="index-operations"></a>Indexes

```python
AddIndex(model_name: str, index: Index, *, concurrently: bool = False)
RemoveIndex(model_name: str, name: str | None = None, fields: list[str] | None = None, *, concurrently: bool = False)
RenameIndex(model_name: str, new_name: str, *, old_name: str | None = None, old_fields: list[str] | None = None)
```

`concurrently=True` builds or drops the index without blocking writes to the table — PostgreSQL
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
by the name it was created under (`idx_<table>_expr_<hash>`, `uidx_...` for a unique one) —
`makemigrations` writes that name when such an index is removed or changed.

## <a id="constraint-operations"></a>Constraints

```python
AddConstraint(
    model_name: str,
    constraint: UniqueConstraint | CheckConstraint | ExclusionConstraint,
    *,
    not_valid: bool = False,
    using_index: str | None = None,
)
RemoveConstraint(model_name: str, name: str | None = None, fields: list[str] | None = None)
RenameConstraint(model_name: str, old_name: str, new_name: str)
ValidateConstraint(model_name: str, name: str)
```

Every constraint lives in the model's `Meta.constraints`, an unnamed `UniqueConstraint(fields=...)`
included — `RemoveConstraint(fields=[...])` finds that one by its fields.

`AddConstraint(..., not_valid=True)` adds a CHECK constraint only new and updated rows must pass
(PostgreSQL `NOT VALID`), without scanning and locking a big table; a later `ValidateConstraint` checks
the existing rows with a lock that doesn't block writes, failing with `IntegrityError` while one
breaks it. `not_valid=True` takes a `CheckConstraint` only (`ConfigurationError` otherwise). The
model's state holds the constraint as declared either way, and drift doesn't flag the NOT VALID one;
inspectdb notes it. SQLite adds the constraint the plain way (checking every row) and has nothing to
validate.

`AddConstraint(..., using_index="<index name>")` makes a named `UniqueConstraint` without a
`condition` take over a unique index the model already has — one built before with
`AddIndex(..., unique=True, concurrently=True)` — instead of building its own while the table's
writes wait: PostgreSQL `ADD CONSTRAINT ... UNIQUE USING INDEX`, which renames the index to the
constraint's name. In the migration state the index leaves the model's `Meta.indexes`, so the model
declares only the constraint. Unapplied, the constraint is dropped and the index built again, for the
`AddIndex` before it. SQLite keeps an index of the constraint's name as it is (its unique constraint
is a unique index of its own name); under another name it builds the constraint's and drops the
index. Another constraint raises `ConfigurationError`. See
[zero-downtime migrations](zero-downtime.md#adding-a-constraint).

## <a id="trigger-operations"></a>Triggers

```python
AddTrigger(model_name: str, trigger: Trigger)
RemoveTrigger(model_name: str, name: str)
AlterTrigger(model_name: str, trigger: Trigger)
RenameTrigger(model_name: str, old_name: str, new_name: str)
```

## <a id="raw-operations"></a>SQL, Python, schemas, extensions and collations

```python
RunPython(code, reverse_code=None, *, atomic: bool | None = None, elidable: bool = False, tenant_schema: bool = False)   # code(apps, schema_editor) -> None | Awaitable[None]
RunSQL(sql, reverse_sql=None, *, atomic: bool | None = None, elidable: bool = False, tenant_schema: bool = False)        # a string, list of strings, or list of (sql, parameters)
SQLOperation(query: str, values: list)                               # one statement with bind parameters; irreversible
CreateSchema(schema_name: str)
DropSchema(schema_name: str)
CreateExtension(extension_name: str)   # Postgres only
RemoveExtension(extension_name: str)   # Postgres only
CreateCollation(name: str, locale: str, *, provider: str = "libc", deterministic: bool = True)   # Postgres only
RemoveCollation(name: str, locale: str, *, provider: str = "libc", deterministic: bool = True)   # Postgres only
```

`CreateCollation` creates a collation for `Collate(...)` and `db_collation=` — e.g. a case-insensitive
ICU one: `CreateCollation("case_insensitive", "und-u-ks-level2", provider="icu", deterministic=False)`.
`RemoveCollation` takes the same arguments, to create it again going back. SQLite has no collation DDL —
both are skipped there with a warning.

`RunPython.noop` is a ready-made no-op you can use as `reverse_code` for a one-way data migration.
`elidable=True` leaves the operation out when its migration is [squashed](migrations.md#squashing-migrations) —
for data a squashed history has no need to write again.
`tenant_schema=True` runs the operation in each tenant's schema instead of the shared one, on a
connection with a [schema per tenant](../soft-delete-versions-tenants/schema-per-tenant.md#migrations) —
where an operation on a model's table follows the model's `Meta.tenant_schema` by itself.

## <a id="separate-database-and-state"></a>Database and state apart

```python
SeparateDatabaseAndState(database_operations: list[Operation] | None = None, state_operations: list[Operation] | None = None)
```

Applies two lists of operations separately: `state_operations` change only the models the
migrations know (what `makemigrations` compares the code with), `database_operations` change only
the database. For a change the database already has, one made in steps, or SQL the state can't
describe:

```python
from hare.migrations import operations as ops

operations = [
    # The field leaves the code and the state now; its column is dropped by a later migration,
    # once no running code reads it.
    ops.SeparateDatabaseAndState(
        state_operations=[ops.RemoveField(model_name="Book", name="legacy_code")],
    ),
]
```

```python
operations = [
    # The column and the field arrive together, the index is built by hand.
    ops.SeparateDatabaseAndState(
        database_operations=[
            ops.AddField(model_name="Book", name="pages", field=fields.IntField(null=True)),
            ops.RunSQL(
                "CREATE INDEX book_pages ON book (pages)",
                reverse_sql="DROP INDEX book_pages",
            ),
        ],
        state_operations=[ops.AddField(model_name="Book", name="pages", field=fields.IntField(null=True))],
    ),
]
```

- **Applied:** the state operations change the state; the database operations run in order, each
  on the models as the operations before it in `database_operations` left them — their own changes
  to the state go no further than that and are not what the migration leaves behind.
- **Unapplied:** the database operations run backwards in reverse order and the state goes back to
  what it was before the migration. The operation is reversible when every database operation is
  (`RunSQL`/`RunPython` with a reverse, every schema operation); with no database operations it is
  always reversible.
- **`sqlmigrate`** shows the SQL of the database operations; the state operations have none.
- **Migration file:** written as the call above, each list with its operations; an empty list is
  left out.
- **Squash:** the operations on either side of it are never merged across it, and nothing inside it
  is merged with the operations around it.
- **SQLite:** a column the state no longer knows is dropped by the next table rebuild of its table —
  see `RemoveField` above. Drop it with a `RunSQL` before
  another field of that table changes in a way SQLite rebuilds the table for.

## <a id="operation-deconstruct"></a>How an operation is written to a file

Indexes, constraints and triggers are one type of thing to the migrations — a named schema object of
a model: `AddIndex`/`AddConstraint`/`AddTrigger`, the `Remove*`, `Rename*` and `AlterTrigger`
operations are thin subclasses of four generic ones (`AddSchemaObject`, `RemoveSchemaObject`,
`RenameSchemaObject`, `AlterSchemaObject` in `hare.migrations.operations.schema_objects`), and the
autodetector diffs all three with one comparison.

Every operation — yours included — is written into a migration file from `Operation.deconstruct()`,
which returns `(class path, positional args, keyword args)`; the writer renders that as the call.
The default takes the constructor's parameters from attributes of the same name and leaves out a
keyword argument still at its default, so an operation whose `__init__` stores each argument under
its own name needs nothing more:

```python
from hare.migrations.operations import Operation


class CreateReportSnapshot(Operation):
    def __init__(self, name: str, query: str, *, with_data: bool = True) -> None:
        self.name = name
        self.query = query
        self.with_data = with_data
    ...
```

Override `deconstruct()` only when the arguments aren't stored as given.

hare's own operations subclass `HareOperation` (`hare.migrations.operations`), an `Operation` that
changes the state and the database separately; an operation of your own subclasses `Operation`.

## <a id="extensions"></a>Extensions

Declare which PostgreSQL extensions a model needs via `Meta.extensions`, instead of hand-writing
`RunSQL("CREATE EXTENSION IF NOT EXISTS ...")`:

```python
class Widget(Model):
    ...
    class Meta:
        extensions = ("pg_trgm",)
```

A field type that always needs one (`CitextField` needs `citext`, `PostGISField` needs `postgis`)
declares it on itself — you don't need to repeat it in `Meta.extensions`. The autodetector
re-derives the full set of extensions a migration needs straight from `Meta.extensions`, a
field's own `requires_extension` and the extension each dialect's column type of the field
needs (`TypeMapping.extension` — `postgis` for a `GeometryField`, `vector` for a `VectorField`)
every time it diffs two model states — the same way it derives schema
presence from `Meta.schema` rather than tracking either as separate stored state — and emits
`CreateExtension`/`RemoveExtension` operations for whatever changed, ordered so a `CREATE EXTENSION`
runs before any table that depends on it, and a `DROP EXTENSION` only after every dependent table is
gone.

## <a id="enum-types"></a>ENUM types

```python
CreateEnumType(name: str, labels: Sequence[str])                      # Postgres only
AlterEnumType(name: str, old_labels: Sequence[str], new_labels: Sequence[str])   # Postgres only
DropEnumType(name: str, labels: Sequence[str])                        # Postgres only
```

The `ENUM` types of [`NativeEnumField`](../dialects/postgresql/fields.md#nativeenumfield) columns.
Like extensions they aren't kept in the state: the autodetector derives them from the fields
(`field.requires_enum_type`) every time it diffs two states — `CreateEnumType` before the first column of
a type, `AlterEnumType` when its labels change, `DropEnumType` after its last column is gone. Two fields
naming one type with different labels raise `ConfigurationError`.

- `AlterEnumType` adds the new labels in place (`ALTER TYPE ... ADD VALUE ... BEFORE ...`) while the old
  labels keep their order. A removed or reordered label replaces the type in one statement: the type is
  renamed, the new one created, every column of it (and every array column) converted through text with
  its default, and the old type dropped — a row still holding a removed label stops it with an error, so
  update those rows in an earlier migration. Going back runs the opposite change.
- `DropEnumType` going back creates the type again with `labels`; `CreateEnumType` going back drops it.
- On a database without `ENUM` types (`features.supports_enum_types` false) the three are skipped with a
  warning — a migration file runs on any database.

## <a id="constraints-and-triggers"></a>Constraints and triggers as objects

See [Constraints](../models/constraints-and-triggers.md#constraints) and
[Triggers](../models/constraints-and-triggers.md#triggers) for `UniqueConstraint`, `CheckConstraint`,
`ExclusionConstraint`, and `Trigger` — the same dataclasses used in `Meta.constraints`/`Meta.triggers`
are what you pass to `AddConstraint`/`AddTrigger` when hand-writing a migration.

## <a id="schema-objects"></a>Views, functions, sequences and access

```python
AddView(model_name: str, view: View)
AlterView(model_name: str, view: View)
RemoveView(model_name: str, name: str)
RenameView(model_name: str, old_name: str, new_name: str)

AddMaterializedView(model_name: str, view: MaterializedView)
AlterMaterializedView(model_name: str, view: MaterializedView)
RemoveMaterializedView(model_name: str, name: str)
RenameMaterializedView(model_name: str, old_name: str, new_name: str)
RefreshMaterializedView(model_name: str, name: str, concurrently: bool = False)

AddFunction(model_name: str, function: DatabaseFunction)
AlterFunction(model_name: str, function: DatabaseFunction)
RemoveFunction(model_name: str, name: str)
RenameFunction(model_name: str, old_name: str, new_name: str)

AddSequence(model_name: str, sequence: DatabaseSequence)
AlterSequence(model_name: str, sequence: DatabaseSequence)
RemoveSequence(model_name: str, name: str)
RenameSequence(model_name: str, old_name: str, new_name: str)

AlterRowLevelSecurity(model_name: str, row_level_security: RowLevelSecurity | None)
AddPolicy(model_name: str, policy: Policy)
AlterPolicy(model_name: str, policy: Policy)
RemovePolicy(model_name: str, name: str)
RenamePolicy(model_name: str, old_name: str, new_name: str)

AddGrant(model_name: str, grant: Grant)
RemoveGrant(model_name: str, grant: Grant)
```

The operations on what a model declares in `Meta.views`, `Meta.materialized_views`,
`Meta.functions`, `Meta.sequences`, `Meta.row_level_security`, `Meta.policies` and `Meta.grants` —
the declarations, what each change does in the database and the order `makemigrations` writes them in
are described in [Views, functions, sequences and access](../models/schema-objects.md). Like the
index, constraint and trigger operations they keep the object in the model's migration state: an
`Add` operation puts it there, an `Alter` one replaces the version of the same name, a `Rename` one
renames it, and `Remove` finds it there by its name — so every one of them is reversible, going back
creating, restoring or renaming the object as the state before held it. `AlterRowLevelSecurity` going
back restores the setting before. A grant has no name: `RemoveGrant` takes the whole grant and finds
the equal one in the state.

An `AddView`/`AddMaterializedView` written by hand may take a queryset (`View("paid", query=
Invoice.objects.filter(paid=True))`): the migration state keeps its SQL, and the operation runs it as
SQL of the connection the migration runs on. A migration file `makemigrations` or `squashmigrations`
writes holds the SQL as `RawSQLTerm(...)`.

`RefreshMaterializedView` fills a materialized view with the rows of its query again — after a data
migration that changed the rows it reads, say. It changes no state, and going back does nothing.
`concurrently=True` keeps the view readable meanwhile and needs its `unique_columns`
(`ConfigurationError` before any SQL otherwise); `concurrently` that isn't a bool raises
`ConfigurationError`.

On a database without the type of object (`features.supports_views`, `supports_materialized_views`,
`supports_database_functions`, `supports_sequences`, `supports_row_level_security`,
`supports_grants`) each operation raises `UnSupportedError` before any SQL is sent — `sqlmigrate`
included. `sqlmigrate` shows the statements each operation runs on PostgreSQL:

```sql
CREATE SEQUENCE "invoice_number" INCREMENT BY 1 START WITH 1000 CACHE 1 NO CYCLE;
ALTER SEQUENCE "invoice_number" OWNED BY "invoice"."number";
CREATE MATERIALIZED VIEW "invoice_totals" AS
SELECT tenant_id, sum(amount) AS total FROM invoice GROUP BY tenant_id
WITH DATA;
CREATE UNIQUE INDEX "invoice_totals_unique" ON "invoice_totals" ("tenant_id");
ALTER TABLE "invoice" ENABLE ROW LEVEL SECURITY;
CREATE POLICY "tenant_rows" ON "invoice" AS PERMISSIVE FOR SELECT TO "reporting" USING (tenant_id = current_tenant());
GRANT SELECT ON TABLE "invoice" TO "reporting";
REFRESH MATERIALIZED VIEW CONCURRENTLY "invoice_totals";
```

## <a id="clickhouse-operations"></a>ClickHouse

```python
AddDictionary(model_name: str, dictionary: Dictionary)
AlterDictionary(model_name: str, dictionary: Dictionary)
RemoveDictionary(model_name: str, name: str)
RenameDictionary(model_name: str, old_name: str, new_name: str)
SynchronizeKeySeries(model_name: str)
```

The dictionary operations work on what a model declares in `Meta.dictionaries` — see
[ClickHouse dictionaries](../dialects/clickhouse/schema-objects.md#dictionaries).
`SynchronizeKeySeries` moves the series of numbers a ClickHouse model's generated key is taken from
past the greatest key of its table — see [ClickHouse keys](../dialects/clickhouse/models.md#keys).

## <a id="indexes"></a>Index classes

See [Indexes](../models/indexes.md) for `Index`/`PartialIndex`, and
[PostgreSQL index types](../models/indexes.md#postgresql-index-types) for `GinIndex`/`GistIndex`/`BrinIndex`/`BloomIndex`/
`HashIndex`/`SpGistIndex`.
