# Operations

The operations a migration file lists, from `hare.migrations.operations`
(`from hare.migrations import operations as ops`): what each one takes, what it does in the database and
in the migration state, and how to write one of your own.

## <a id="model-operations"></a>Models

The operations of a model's table: creating, renaming and deleting it, and changing its options and its
table name.

```python
class CreateModel:
    def __init__(
        self,
        name: str,
        fields: list[tuple[str, FieldLike]],
        options: dict[str, Any] | None = None,
        bases: list[str] | None = None,
        state_only: bool = False,
    ) -> None: ...

class RenameModel:
    def __init__(self, old_name: str, new_name: str) -> None: ...

class DeleteModel:
    def __init__(self, name: str, state_only: bool = False) -> None: ...

class AlterModelOptions:
    def __init__(self, name: str, options: dict[str, Any]) -> None: ...

class AlterModelTable:
    def __init__(self, name: str, table: str) -> None: ...

class AlterModelSchema:
    def __init__(self, name: str, schema: str | None) -> None: ...
```

`RenameModel` and `AlterModelTable` leave the raw SQL of the model's views and materialized views as it
is written: raw SQL is yours, hare never rewrites it. A view whose query you rewrote for the new table name
is a changed declaration — `makemigrations` removes it before the rename and adds it after.

A model moved to another app with its table kept as it is (same table name, fields and options), once
`makemigrations` is told it's moved (see [Renames](migrations.md#renames)), becomes a
`CreateModel(..., state_only=True)` in the new app and a `DeleteModel(..., state_only=True)` in the old
one: both only change the migration state, the table and its rows stay, and relations pointing at the
model are repointed without DDL. The new app's migration depends on the old app's migrations. A moved
model whose fields or options also change in the same run is refused — move it unchanged first, then
change it.

`makemigrations` orders the operations of every app it runs for together, each after what it needs: a
relation after the model it points at, the fields its `to_field` names and its through model; a model or
a field after the relations pointing at it are removed or repointed; a model taking a table after the
model that had it is deleted. Operations needing nothing of each other keep the order they were made in.
It then cuts them into migrations per app — an operation needing a new migration of another app that
already depends on its own app's goes into a next migration of its app — and each migration depends on
the new migrations of what it needs and on the existing migrations of an app whose model it needs. A
relation closing a cycle among models created together is added by an `AddField` after them (a
many-to-many relation rather than a foreign key).

Deleting a model referenced from another app makes the deleting migration depend on the other app's new
migration that removes or repoints the reference. `makemigrations` for the deleting app alone is refused
while the other app's migrations still reference the model. Migration files that can't be replayed in
order (a relation left pointing at a deleted or never created model) are reported by every command as a
plain error naming the relation.

A model deleted in the same run in which a new relation takes over one of its backward accessors (e.g.
`Book` replaced by `Novel`, both with `related_name="books"` on `Author`) is deleted before the new model
is created; while another model still references it until later in the migration, only its conflicting
relation field is removed first. An added model with exactly the same fields and options as a removed
model of the same app that isn't renamed (the rename question answered no, or not asked — e.g. two
identical models renamed at once) gets a "possible unrecognized rename" warning from `makemigrations` —
the generated `CreateModel`/`DeleteModel` pair drops the old table's rows; answer the rename with
`--rename` if it's a rename.

`AlterModelOptions.options` is the model's whole set of options that have no operation of their own
(everything except the table, schema, indexes, constraints, triggers, primary key, views, materialized
views, functions, sequences, row level security, policies and grants) — an option left out of it is
removed from the model. An empty option, `managed=True` and the options the migration state doesn't
keep — `ordering`, `get_latest_by`, `manager`, `returning`, `track_dirty_fields`, `change_capture`, which
change no schema — are left out of the state, as `makemigrations` leaves them. A changed
`table_description` updates the table comment on PostgreSQL.
`Meta.tenant_field`, `soft_delete_field` and `optimistic_lock_field` name a field in every state: when one
moves to another field, `makemigrations` writes the new field first, the `AlterModelOptions` after it and
the removal of the old field last.

Switching an existing model to `Meta.managed = False` generates an `AlterModelOptions` that keeps the
model in the migration state with `managed: False`: its table and rows stay, and while it stays unmanaged
no further change of it touches the table. `CreateModel`/`DeleteModel` of a model whose state is
unmanaged runs no DDL either, so removing such a model from the code leaves its table in place. Switching
it back to managed is again an `AlterModelOptions`, followed by whatever field/index changes the model
got in between. A model that was never managed by the migrations (unmanaged from the start) isn't part of
the migration state at all — making it managed generates a `CreateModel`; if its table already exists,
apply that migration with `migrate --fake`.

`AlterModelSchema(name: str, schema: str | None)` moves the model's table (`ALTER TABLE ... SET SCHEMA` on
PostgreSQL, `RENAME TABLE` into another database of the server on ClickHouse — refused for a model whose
views, materialized views, dictionaries or `Distributed` table name the table by its database; SQLite
can't move a table to another attached database and refuses the move) together with the partitions of a
partitioned table and the automatic through tables of the model's own `ManyToManyField`s, which live in the
schema of the model declaring them, and the views, materialized views, functions and sequences the model
declares (an `ENUM` type of a field stays in its own schema); a database that can't move one of them raises
`UnSupportedError` before any DDL. `schema=None` moves them into the connection's current schema, where a
table without `Meta.schema` is created.

## <a id="partition-operations"></a>Partitions

Adding and removing a partition of a partitioned table (PostgreSQL) — see [Partitioned
tables](../dialects/postgresql/indexes-and-tables.md#partitioning).

```python
class AddPartition:
    def __init__(self, model_name: str, partition: Any) -> None: ...

class RemovePartition:
    def __init__(self, model_name: str, partition: Any) -> None: ...
```

`AddPartition`/`RemovePartition` add and remove one partition of a [partitioned
table](../dialects/postgresql/indexes-and-tables.md#partitioning) — a `ListPartition`, `RangePartition`
or `DefaultPartition` of the model's `PostgresqlTableOptions`. `makemigrations` writes one per partition
that came or went, so the migration file shows which. `AddPartition` creates an empty partition with the
table's storage parameters and tablespace. `RemovePartition` detaches the partition and drops it **with
its rows** — `makemigrations` warns about it; a partition whose values or bounds changed is removed and
added again. Both are reversible: unapplying `AddPartition` removes the partition with the rows it got
meanwhile, unapplying `RemovePartition` adds it back empty. On a connection of another dialect they only
change the state. Everything else about how a table is partitioned — partitioning added or removed,
another strategy, key or count of hash partitions — PostgreSQL can't change in place: that
`AlterModelOptions` creates the table anew and copies its rows (its indexes, constraints, comments,
triggers and the foreign keys of other tables referencing it are set again), rewriting the whole table
under an exclusive lock.

## <a id="field-operations"></a>Fields

The operations on a model's fields: adding, removing, changing and renaming one, and filling a column of
a big table.

```python
class AddField:
    def __init__(
        self,
        model_name: str,
        name: str,
        field: FieldLike,
        *,
        not_valid: bool = False,
    ) -> None: ...

class RemoveField:
    def __init__(self, model_name: str, name: str) -> None: ...

class AlterField:
    def __init__(self, model_name: str, name: str, field: FieldLike) -> None: ...

class RenameField:
    def __init__(
        self,
        model_name: str,
        old_name: str,
        new_name: str,
        field: FieldLike | None = None,
    ) -> None: ...

class BackfillColumn:
    def __init__(
        self,
        model_name: str,
        field_name: str,
        value: Any | Callable[[], Any],
        *,
        batch_size: int = 1000,
    ) -> None: ...

class AlterColumnNotNullSafe:
    def __init__(self, model_name: str, field_name: str) -> None: ...
```

### <a id="add-field"></a>`AddField`

`AddField` adds the column and the field's own index — `db_index=True`, and a relation's index — after
filling the existing rows; the index is the field's flag, not an index operation of its own. `AlterField`
creates or drops it as the flag changes, `RemoveField` drops it with the column.

`AddField(..., not_valid=True)` of a `ForeignKeyField`/`OneToOneField` with a database constraint adds
the column, then its FOREIGN KEY `NOT VALID` (PostgreSQL) under the name the inline `REFERENCES` would
have: only new and updated rows must satisfy it, so adding it doesn't scan the table while the writes to
both tables wait; a later `ValidateConstraint(model_name, <the foreign key's name>)` checks the existing
rows with a lock that doesn't block writes. Another field, or a relation with `db_constraint=False`,
raises `ConfigurationError`. SQLite never checks the existing rows as it adds the column, so the option
changes nothing there.

`AddField` of a NOT NULL field whose only default is a Python `default=` (no `db_default`) works on a
table that already holds rows: the column is added nullable, every existing row is set to the default,
then the column becomes NOT NULL (`ALTER COLUMN ... SET NOT NULL` on PostgreSQL, a table rebuild on
SQLite). A callable default is evaluated **once**, so every existing row gets that same value — for a
per-row value (e.g. `uuid4` on a unique column) add the column nullable, fill it in a `RunPython` step,
then make it NOT NULL. A NOT NULL `DatetimeField`/`TimeField` with `auto_now=True`/`auto_now_add=True`
(and no `default`/`db_default`) is filled the same way, with the current time written exactly as a
regular `save()` writes it.

A NOT NULL field with no default at all can't be added to a table holding rows: PostgreSQL and SQLite
refuse the column, the migration rolled back. ClickHouse adds it and its stored rows read the type's
empty value (`0`, `''`) — a `unique=True` field too, its rows then sharing that value.

### <a id="remove-field"></a>`RemoveField`

`RemoveField` on SQLite drops the column with `ALTER TABLE ... DROP COLUMN`
(`features.supports_drop_column`) when the model keeps SQLite from nothing — decided by the migration
state alone, nothing read from the database: the field isn't a key, unique, indexed, a relation or
generated; the model has no CHECK constraint, generated column or trigger; and none of its indexes is a
partial or an expression index or names the field, nor is any unique constraint conditional or naming it.
An object the migrations don't know of over the column (an index, a trigger or a view made by `RunSQL`)
makes SQLite refuse the `DROP COLUMN`. Otherwise the table is rebuilt from the model the migrations know:
created anew, the rows copied, its indexes, unique constraints and triggers created again. A rebuild keeps
only the columns the migration state has, so a column the state no longer knows (removed with
[`SeparateDatabaseAndState`](#separate-database-and-state)) is dropped by the next rebuild of its table;
an index, trigger or view created by `RunSQL` on the table isn't created again either.

### <a id="alter-field"></a>`AlterField`

`AlterField` changing the column type of a primary key (e.g. `IntField` to `BigIntField`) or of another
column a relation targets through `to_field=` changes the key columns referencing it too: every
ForeignKeyField/OneToOneField column and every automatic ManyToManyField through-table column. On
PostgreSQL their foreign key constraints are dropped first and re-created once every column has the new
type; on SQLite the referencing tables are rebuilt. The values are converted with a plain cast, so on
PostgreSQL a type pair it can't cast (`integer` to `uuid`) fails.

`AlterField` to a column type that can't hold a value the table already stores raises
`FieldNarrowingDataLossError` before anything changes, on every dialect: a shorter `max_length` (or a
text, number or boolean turned into a `CharField` too short for its text), a `DecimalField` with fewer
decimal places or whole digits, a smaller integer type, and a float or decimal turned into an integer or
a decimal it doesn't fit. PostgreSQL would otherwise cut or round the value in the cast, and SQLite would
keep it beyond the column's declared size. Fix or widen those values first; a narrower type every stored
value fits in alters as usual. SQLite copies every value into the rebuilt table as it is, so a value the new
field doesn't read — a text that is no number, date or boolean, no `UUID`, no JSON document, no duration,
no bytes — raises `OperationalError` before the table changes (`the column holds "abc", which is no
UUIDField`).

On ClickHouse, a text column changed to another type converts its texts after the column has taken the new
type: a text the server can't read as a value of that type would leave the table unreadable, turn into NULL
in a nullable column, or (an impossible date such as `2024-02-30`) become another value. Such a text raises
`OperationalError` before anything changes (`column "value" of table "note" holds "abc", which is no
Int32`) — a text that reads as the type, a date or moment written as `YYYY-MM-DD[ hh:mm:ss]`, a decimal
with no more digits than the field holds, a JSON object for a `JSONField`, an enum's label, converts. A
column of another type is checked the same way: a value the new type doesn't hold (an integer of no `UUID`)
raises `OperationalError` before the change, and so does a type the server converts to no value of the new
one (`DateTime` to `Int16`, `UUID` to `Int32`) — a change between two number types is the narrowing check's.
A field the server keeps as text but hare reads as another type (a `TimeField`) has each stored text read
as the new field reads it: one it doesn't read (`x`, `25:00:00`) raises `OperationalError` before the
change.

`AlterField` changing a relation's `to_field=` repoints its key column (and foreign key constraint) at
the new target column. Stored key values name rows through the old target column and can't be translated,
so the migration raises `ForeignKeyTargetChangeError` while any row stores one — add a new relation field
targeting the new column, fill it with `RunPython`/`RunSQL`, then remove the old field. `makemigrations`
prints the same warning.

A plain field turned into a `ForeignKeyField`/`OneToOneField` over its column —
`author_id = fields.IntField()` replaced by `author = fields.ForeignKeyField("app.Author")` — becomes a
`RenameField` and an `AlterField`: the column and its values stay, its type changes to the target's key
type, and the relation's index, uniqueness and foreign key constraint are added (on PostgreSQL every row
is checked against the constraint). The reverse drops them and keeps the column. A `ManyToManyField` or a
relation to a composite key has no one column: such a change raises `ConfigurationError` — remove the
field and add the new one.

Switching a `ManyToManyField` from hare's automatic through table to `through=SomeModel` (or back) copies
the relation's rows into the new table and drops the automatic one. When both use the same table name,
`makemigrations` first renames the automatic table to `<table>__swap`, so the through model's table can
be created under that name. The through model's other columns of the copied rows get their Python
`default` (a callable one is evaluated once, so every row gets that same value — refused with
`ConfigurationError` for a unique column), the current time for `auto_now`/`auto_now_add`, or their
`db_default`; a column with none of them must be nullable.

### <a id="rename-field"></a>`RenameField`

`RenameField`'s `field` is the field under its new name, when the rename changes it too; without it the
field is kept as it was. The rename renames the field wherever a declaration names it as a field: the
model's indexes — their fields, key expressions (`Lower("title")`, an ordering) and `Q` conditions — and
constraints (their fields, `Q` conditions and checks, an exclusion constraint's field entries), a
trigger's `Q` condition, a policy's `Q`, a materialized view's `unique_columns`, a sequence's `owned_by`,
`Meta.tenant_field`/`soft_delete_field`/`optimistic_lock_field`, the table options naming fields (a
ClickHouse `order_by`), and an unnamed index or unique constraint named after its keys takes the name the
renamed model gives it. It renames the columns other tables name after a component of a composite key —
a relation's `<relation>_<component>` column, an automatic through table's — however many relations deep:
a relation whose `to_field` names such a key field, or the key fields of a renamed relation itself,
follows the new names.

Raw SQL (`RawSQLTerm` — a raw condition, check or key, a trigger's body, a view's query, a generated
column's expression, a raw table option, a projection's query) is yours: the rename leaves it as it is
written. A declaration whose raw SQL you rewrote for the new name is a changed declaration —
`makemigrations` removes it before the rename and adds it after (a generated field is removed and added
the same way), so unapplying the migration brings the old declaration back only once the column has its
old name again. A `RenameField` written by hand next to raw SQL still naming the old column works where
the database renames the column under it (PostgreSQL, SQLite rewrite their own DDL); where it doesn't,
the database refuses the rename — as with `RunSQL`, the raw SQL is yours to change.

### <a id="backfill-column"></a>`BackfillColumn` and `AlterColumnNotNullSafe`

Two operations for a big table in use — filling a column in batches and making it NOT NULL without a long
lock — see [BackfillColumn and
AlterColumnNotNullSafe](zero-downtime.md#backfillcolumn-and-altercolumnnotnullsafe).

## <a id="index-operations"></a>Indexes

```python
class AddIndex:
    def __init__(self, model_name: str, index: Index, *, concurrently: bool = False) -> None: ...

class RemoveIndex:
    def __init__(
        self,
        model_name: str,
        name: str | None = None,
        fields: list[str] | None = None,
        *,
        concurrently: bool = False,
    ) -> None: ...

class RenameIndex:
    def __init__(
        self,
        model_name: str,
        new_name: str,
        *,
        old_name: str | None = None,
        old_fields: list[str] | None = None,
    ) -> None: ...
```

`concurrently=True` builds or drops the index without blocking writes to the table — PostgreSQL
`CREATE INDEX CONCURRENTLY`/`DROP INDEX CONCURRENTLY`, which can't run inside a transaction: set
`atomic = False` on the migration, or the operation raises `ConfigurationError`. `makemigrations` writes
plain `AddIndex`/`RemoveIndex`; add `concurrently=True` by hand for a big, busy table. A database without
it (`Features.supports_concurrent_indexes` — SQLite, ClickHouse) builds and drops the index the plain way.

```python
class Migration(migrations.Migration):
    atomic = False
    operations = [
        ops.AddIndex("Order", Index(fields=["placed_at"], name="orders_placed_idx"), concurrently=True),
    ]
```

`RemoveIndex(name=...)` also finds an unnamed expression-based index (e.g. `Index(Lower("name"))`) by the
name it was created under (`idx_<table>_expr_<hash>`, `uidx_...` for a unique one) — `makemigrations`
writes that name when such an index is removed or changed.

## <a id="constraint-operations"></a>Constraints

```python
class AddConstraint:
    def __init__(
        self,
        model_name: str,
        constraint: UniqueConstraint | CheckConstraint | ExclusionConstraint,
        *,
        not_valid: bool = False,
        using_index: str | None = None,
    ) -> None: ...

class RemoveConstraint:
    def __init__(
        self,
        model_name: str,
        name: str | None = None,
        fields: list[str] | None = None,
    ) -> None: ...

class RenameConstraint:
    def __init__(self, model_name: str, old_name: str, new_name: str) -> None: ...

class ValidateConstraint:
    def __init__(self, model_name: str, name: str) -> None: ...
```

Every constraint lives in the model's `Meta.constraints`, an unnamed `UniqueConstraint(fields=...)`
included — `RemoveConstraint(fields=[...])` finds that one by its fields.

`AddConstraint(..., not_valid=True)` adds a CHECK constraint only new and updated rows must pass
(PostgreSQL `NOT VALID`), without scanning and locking a big table; a later `ValidateConstraint` checks
the existing rows with a lock that doesn't block writes, failing with `IntegrityError` while one breaks
it. `not_valid=True` takes a `CheckConstraint` only (`ConfigurationError` otherwise). The model's state
holds the constraint as declared either way, and drift doesn't flag the NOT VALID one; inspectdb notes
it. SQLite adds the constraint the plain way (checking every row) and has nothing to validate. ClickHouse
writes a plain `ADD CONSTRAINT ... CHECK`, which the server checks only on the rows written later — the
existing ones aren't checked, and `ValidateConstraint` checks nothing there.

`AddConstraint(..., using_index="<index name>")` makes a named `UniqueConstraint` without a `condition`
take over a unique index the model already has — one built before with
`AddIndex(..., unique=True, concurrently=True)` — instead of building its own while the table's writes
wait: PostgreSQL `ADD CONSTRAINT ... UNIQUE USING INDEX`, which renames the index to the constraint's
name. In the migration state the index leaves the model's `Meta.indexes`, so the model declares only the
constraint. Unapplied, the constraint is dropped and the index built again, for the `AddIndex` before it.
SQLite keeps an index of the constraint's name as it is (its unique constraint is a unique index of its
own name); under another name it builds the constraint's and drops the index. Another constraint raises
`ConfigurationError`. See [zero-downtime migrations](zero-downtime.md#adding-a-constraint).

## <a id="trigger-operations"></a>Triggers

Adding, removing, changing and renaming a trigger of `Meta.triggers` — see
[Constraints and triggers](../models/constraints-and-triggers.md).

```python
class AddTrigger:
    def __init__(self, model_name: str, trigger: Trigger) -> None: ...

class RemoveTrigger:
    def __init__(self, model_name: str, name: str) -> None: ...

class AlterTrigger:
    def __init__(self, model_name: str, trigger: Trigger) -> None: ...

class RenameTrigger:
    def __init__(self, model_name: str, old_name: str, new_name: str) -> None: ...
```

## <a id="raw-operations"></a>SQL, Python, schemas, extensions and collations

```python
class RunPython(HareOperation):
    # code(apps, schema_editor) -> None | Awaitable[None]
    def __init__(
        self,
        code: Callable[..., Any],
        reverse_code: Callable[..., Any] | None = None,
        *,
        atomic: bool | None = None,
        elidable: bool = False,
        tenant_schema: bool = False,
    ) -> None: ...

class RunSQL(HareOperation):
    # a string, a list of strings, or a list of (sql, parameters) - each parameter at a %s placeholder on
    # every database, as raw() takes them (a literal % of such sql written %%)
    def __init__(
        self,
        sql: str | Sequence[Any],
        reverse_sql: str | Sequence[Any] | None = None,
        *,
        atomic: bool | None = None,
        elidable: bool = False,
        tenant_schema: bool = False,
    ) -> None: ...

class CreateSchema:
    def __init__(self, schema_name: str) -> None: ...

class DropSchema:
    def __init__(self, schema_name: str) -> None: ...

class CreateExtension(HareOperation):
    # PostgreSQL
    def __init__(self, extension_name: str) -> None: ...

class RemoveExtension(HareOperation):
    # PostgreSQL
    def __init__(self, extension_name: str) -> None: ...

class CreateCollation(HareOperation):
    # PostgreSQL
    def __init__(
        self,
        name: str,
        locale: str,
        *,
        provider: str = "libc",
        deterministic: bool = True,
    ) -> None: ...

class RemoveCollation(CreateCollation):
    # PostgreSQL
    def __init__(
        self,
        name: str,
        locale: str,
        *,
        provider: str = "libc",
        deterministic: bool = True,
    ) -> None: ...
```

`DropSchema`, and `CreateSchema` going back, drop only an empty schema — the database refuses one that still
holds objects, so a schema made outside the migrations is never dropped with what it holds.

`CreateCollation` creates a collation for `Collate(...)` — e.g. a case-insensitive ICU one:
`CreateCollation("case_insensitive", "und-u-ks-level2", provider="icu", deterministic=False)`.
`RemoveCollation` takes the same arguments, to create it again going back. SQLite and ClickHouse have no
collation DDL (`Features.supports_collations`) — both are skipped there with a warning.

The `apps` given to `code` and `reverse_code` hold the models as the migrations applied before this one left
them. On a rollback they hold only migrations still applied: an app depending on this one that the same
run has already unapplied is not in them, so deleting rows through `apps.get_model()` does not cascade
into its dropped tables.

`RunPython.noop` is a ready-made no-op you can use as `reverse_code` for a one-way data migration.
`elidable=True` leaves the operation out when its migration is
[squashed](migrations.md#squashing-migrations) — for data a squashed history has no need to write again.
`tenant_schema=True` runs the operation in each tenant's schema instead of the shared one, on a
connection with a [schema per tenant](../soft-delete-versions-tenants/schema-per-tenant.md#migrations) —
where an operation on a model's table follows the model's `Meta.tenant_schema` by itself.

## <a id="separate-database-and-state"></a>Database and state apart

```python
class SeparateDatabaseAndState:
    def __init__(
        self,
        database_operations: list[Operation] | None = None,
        state_operations: list[Operation] | None = None,
    ) -> None: ...
```

Applies two lists of operations separately: `state_operations` change only the models the migrations know
(what `makemigrations` compares the code with), `database_operations` change only the database. For a
change the database already has, one made in steps, or SQL the state can't describe:

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

- **Applied:** the state operations change the state; the database operations run in order, each on the
  models as the operations before it in `database_operations` left them — their own changes to the state
  go no further than that and are not what the migration leaves behind.
- **Unapplied:** the database operations run backwards in reverse order and the state goes back to what
  it was before the migration. The operation is reversible when every database operation is
  (`RunSQL`/`RunPython` with a reverse, every schema operation); with no database operations it is always
  reversible.
- **`sqlmigrate`** shows the SQL of the database operations; the state operations have none.
- **Migration file:** written as the call above, each list with its operations; an empty list is left
  out.
- **Squash:** the operations on either side of it are never merged across it, and nothing inside it is
  merged with the operations around it.
- **SQLite:** a column the state no longer knows is dropped by the next table rebuild of its table — see
  [`RemoveField`](#remove-field). Drop it with a `RunSQL` before another field of that table changes in a
  way SQLite rebuilds the table for.

## <a id="operation-deconstruct"></a>How an operation is written to a file

Every operation — yours included — is written into a migration file from `Operation.deconstruct()`, which
returns `(class path, positional args, keyword args)`; the writer renders that as the call. The default
takes the constructor's parameters from attributes of the same name and leaves out a keyword argument
still at its default, so an operation whose `__init__` stores each argument under its own name needs
nothing more:

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
class Book(Model):
    ...
    class Meta:
        extensions = ("pg_trgm",)
```

A field type, an index or a constraint that needs an extension names it itself — you don't repeat it
in `Meta.extensions`: a field through its dialect's column type (`TypeMapping.extension` — `citext` for a
`CitextField`, `postgis` for a `PostGISField` or a `GeometryField`, `vector` for a `VectorField`), an index
of an access method an extension provides through its class (`BloomIndex` needs `bloom`; a `GistIndex` or
a `GinIndex` over a plain scalar column without an operator class needs `btree_gist`/`btree_gin`), and a
constraint through the dialect's schema editor (a GiST `ExclusionConstraint` or a key compared
`WITHOUT OVERLAPS` over a scalar column needs `btree_gist`). A migration file runs on any database, so the
autodetector creates what any dialect needs — a database without extensions skips `CreateExtension` — and
`generate_schemas()` creates what its connection's dialect needs; `CREATE EXTENSION` runs before any table
that depends on it. The extensions the migrations created are part of the migration state: a model needing
one again later gets no second `CreateExtension`. An extension no model needs any more stays — the
database may use it elsewhere; a hand-written `RemoveExtension` drops it.

## <a id="enum-types"></a>ENUM types

```python
class CreateEnumType:
    def __init__(self, name: str, labels: Sequence[str], schema: str | None = None) -> None: ...

class AlterEnumType:
    def __init__(
        self, name: str, old_labels: Sequence[str], new_labels: Sequence[str], schema: str | None = None
    ) -> None: ...

class DropEnumType:
    def __init__(self, name: str, labels: Sequence[str], schema: str | None = None) -> None: ...
```

The `ENUM` types of [`NativeEnumField`](../dialects/postgresql/fields.md#nativeenumfield) columns. Like
extensions they aren't kept in the state: the autodetector derives them from the fields
(`field.requires_enum_type`) every time it diffs two states — `CreateEnumType` before the first column of
a type, `AlterEnumType` when its labels change, `DropEnumType` after its last column is gone. A type is
named by its schema and name: `schema` is the field's `type_schema`, None for the connection's default
schema, so same-named types of two schemas are two types. Two fields naming one
type with different labels raise `ConfigurationError`. A migration declares the enum of such a
field itself (`Mood = Enum("Mood", {...})`), its members as they are when it is written, rather than
importing the class — a member added to the class later is a change the next `makemigrations` sees.

- `AlterEnumType` adds the new labels in place (`ALTER TYPE ... ADD VALUE ... BEFORE ...`) while the old
  labels keep their order. A removed or reordered label replaces the type in one statement: the type is
  renamed, the new one created, every column of it (and every array column) converted through text with
  its default, and the old type dropped — a row still holding a removed label stops it with an error, so
  update those rows in an earlier migration. Going back runs the opposite change.
- `DropEnumType` going back creates the type again with `labels`; `CreateEnumType` going back drops it.
- On a database without `ENUM` types (`features.supports_enum_types` false) the three are skipped with a
  warning — a migration file runs on any database.

## <a id="constraints-and-triggers"></a>Indexes, constraints and triggers as objects

Indexes, constraints and triggers are one type of thing to the migrations — a named schema object of a
model: `AddIndex`/`AddConstraint`/`AddTrigger`, the `Remove*`, `Rename*` and `AlterTrigger` operations
are thin subclasses of four generic ones (`AddSchemaObject`, `RemoveSchemaObject`, `RenameSchemaObject`,
`AlterSchemaObject` in `hare.migrations.operations.schema_objects`), and the autodetector diffs all three
with one comparison.

See [Indexes](../models/indexes.md) for `Index`/`PartialIndex`, and [PostgreSQL: index
types](../dialects/postgresql/indexes-and-tables.md#postgresql-index-types) for
`GinIndex`/`GistIndex`/`BrinIndex`/`BloomIndex`/ `HashIndex`/`SpGistIndex`.

See [Constraints](../models/constraints-and-triggers.md#constraints) and
[Triggers](../models/constraints-and-triggers.md#triggers) for `UniqueConstraint`, `CheckConstraint`,
`ExclusionConstraint`, and `Trigger` — the same dataclasses used in `Meta.constraints`/`Meta.triggers`
are what you pass to `AddConstraint`/`AddTrigger` when hand-writing a migration.

## <a id="schema-objects"></a>Views, functions, sequences and access

```python
class AddView:
    def __init__(self, model_name: str, view: View) -> None: ...

class AlterView:
    def __init__(self, model_name: str, view: View) -> None: ...

class RemoveView:
    def __init__(self, model_name: str, name: str) -> None: ...

class RenameView:
    def __init__(self, model_name: str, old_name: str, new_name: str) -> None: ...

class AddMaterializedView:
    def __init__(self, model_name: str, view: MaterializedView) -> None: ...

class AlterMaterializedView:
    def __init__(self, model_name: str, view: MaterializedView) -> None: ...

class RemoveMaterializedView:
    def __init__(self, model_name: str, name: str) -> None: ...

class RenameMaterializedView:
    def __init__(self, model_name: str, old_name: str, new_name: str) -> None: ...

class RefreshMaterializedView:
    def __init__(self, model_name: str, name: str, concurrently: bool = False) -> None: ...

class AddFunction:
    def __init__(self, model_name: str, function: DatabaseFunction) -> None: ...

class AlterFunction:
    def __init__(self, model_name: str, function: DatabaseFunction) -> None: ...

class RemoveFunction:
    def __init__(self, model_name: str, name: str) -> None: ...

class RenameFunction:
    def __init__(self, model_name: str, old_name: str, new_name: str) -> None: ...

class AddSequence:
    def __init__(self, model_name: str, sequence: DatabaseSequence) -> None: ...

class AlterSequence:
    def __init__(self, model_name: str, sequence: DatabaseSequence) -> None: ...

class RemoveSequence:
    def __init__(self, model_name: str, name: str) -> None: ...

class RenameSequence:
    def __init__(self, model_name: str, old_name: str, new_name: str) -> None: ...

class AlterRowLevelSecurity:
    def __init__(self, model_name: str, row_level_security: RowLevelSecurity | None) -> None: ...

class AddPolicy:
    def __init__(self, model_name: str, policy: Policy) -> None: ...

class AlterPolicy:
    def __init__(self, model_name: str, policy: Policy) -> None: ...

class RemovePolicy:
    def __init__(self, model_name: str, name: str) -> None: ...

class RenamePolicy:
    def __init__(self, model_name: str, old_name: str, new_name: str) -> None: ...

class AddGrant:
    def __init__(self, model_name: str, grant: Grant) -> None: ...

class RemoveGrant:
    def __init__(self, model_name: str, grant: Grant) -> None: ...
```

The operations on what a model declares in `Meta.views`, `Meta.materialized_views`, `Meta.functions`,
`Meta.sequences`, `Meta.row_level_security`, `Meta.policies` and `Meta.grants` — the declarations, what
each change does in the database and the order `makemigrations` writes them in are described in
[Views, functions, sequences and access](../models/schema-objects.md). Like the index, constraint and
trigger operations they keep the object in the model's migration state: an `Add` operation puts it there,
an `Alter` one replaces the version of the same name, a `Rename` one renames it, and `Remove` finds it
there by its name — so every one of them is reversible, going back creating, restoring or renaming the
object as the state before held it. `AlterRowLevelSecurity` going back restores the setting before. A
grant has no name: `RemoveGrant` takes the whole grant and finds the equal one in the state.

An `AddView`/`AddMaterializedView` written by hand may take a queryset
(`View("paid", query= Invoice.objects.filter(paid=True))`): the migration state keeps its SQL, and the
operation runs it as SQL of the connection the migration runs on. A migration file `makemigrations` or
`squashmigrations` writes holds the SQL as `RawSQLTerm(...)`.

`RefreshMaterializedView` fills a materialized view with the rows of its query again — after a data
migration that changed the rows it reads, say. It changes no state, and going back does nothing.
`concurrently=True` keeps the view readable meanwhile and needs its `unique_columns`
(`ConfigurationError` before any SQL otherwise).

On a database without the type of object (`features.supports_views`, `supports_materialized_views`,
`supports_database_functions`, `supports_sequences`, `supports_row_level_security`, `supports_grants`)
each operation raises `UnSupportedError` before any SQL is sent — `sqlmigrate` included. `sqlmigrate`
shows the statements each operation runs on PostgreSQL:

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
class AddDictionary:
    def __init__(self, model_name: str, dictionary: Dictionary) -> None: ...

class AlterDictionary:
    def __init__(self, model_name: str, dictionary: Dictionary) -> None: ...

class RemoveDictionary:
    def __init__(self, model_name: str, name: str) -> None: ...

class RenameDictionary:
    def __init__(self, model_name: str, old_name: str, new_name: str) -> None: ...

class SynchronizeKeySeries:
    def __init__(self, model_name: str) -> None: ...
```

The dictionary operations work on what a model declares in `Meta.dictionaries` — see [ClickHouse
dictionaries](../dialects/clickhouse/schema-objects.md#dictionaries). `SynchronizeKeySeries` moves the
series of numbers a ClickHouse model's generated key is taken from past the greatest key of its table —
see [ClickHouse keys](../dialects/clickhouse/models.md#keys).
