# Zero-downtime migrations

A migration applied while the application keeps running must not keep a big table locked for long,
and must not break the code still running during the deployment — the old version, which reads the
columns and tables as they were. hare checks migrations for both (the
[safety check](#checking-migrations)), and its operations make each risky change in small, safe
steps — the "expand-contract" pattern.

## <a id="checking-migrations"></a>Checking migrations

**`makemigrations`** checks the migrations it writes and prints what is risky, each with how to make
the same change safely:

```text
WARNING: risky on a database in use if the tables are large - `hare checkmigrations` checks against the database:
  app.0007_remove_book_pages: Remove field pages from Book [remove_field]
    The column of Book.pages is dropped while the code still running reads it.
    Safely: First remove the field from the models only - SeparateDatabaseAndState(state_operations=[RemoveField(model_name='Book', name='pages')]) - and drop the column with RunSQL in a later migration, once no running code reads it.
```

It doesn't read the database, so every table that exists before the migration may be large.

**`hare checkmigrations [APP ...]`** checks the migrations `migrate` would apply, in its order,
against the database: a table counts as large from `migrations.safety.large_table_rows` rows
(100 000 by default) — PostgreSQL reads the planner's estimate (`pg_class.reltuples`), counting the
rows itself, no further than that, for a table never analyzed; SQLite counts them the same way; ClickHouse
reads the count its table engines keep (`system.tables.total_rows`) — a view, which keeps none,
counts as large. The command exits with 1 while a risk isn't exempted — run it in CI before deploying.

```python
# hare config
{
    ...
    "migrations": {"safety": {"large_table_rows": 1_000_000}},
}
```

`large_table_rows` is a whole number from 0 (every table is large) to 10<sup>12</sup>; anything else
raises `ConfigurationError` when the config is loaded.

The same check from code:

```python
from hare.migrations.api import checkmigrations

risks = await checkmigrations(config=HARE_ORM, app_labels=["shop"])
for risk in risks:
    print(risk.code, risk.app_label, risk.migration_name, risk.operation, risk.message, risk.safe_alternative)
```

Each `MigrationRisk` names the rule (`code`, a `MigrationRiskCode`), the migration and its operation
(as `describe()` writes it), what goes wrong (`message`) and the safe way (`safe_alternative`);
`exempted` is True for a risk its migration accepts.

- A table the same migration creates is new and empty — none of the lock rules apply to it.
- In a [`SeparateDatabaseAndState`](operations.md#separate-database-and-state), the database operations
  are checked by the lock rules; the rules about running code (renamed and removed fields and models)
  don't apply — such an operation is how the code and the database are kept in step by hand.
- `MigrationSafetyChecker(large_table_rows=...)` (`hare.migrations.safety`) checks one migration on a
  given state: `await checker.check(migration, state, dialect=..., client=...)` — without `client`,
  every existing table may be large.

### <a id="exempting-a-risk"></a>Exempting a risk

A risk checked by hand — raw SQL read through, a table known to stay small — is accepted by listing
its code in the migration's `safety_exemptions`. `checkmigrations` still lists it, marked
`(exempted)`, and doesn't fail for it:

```python
from hare import migrations
from hare.migrations import operations as ops
from hare.migrations.safety import MigrationRiskCode


class Migration(migrations.Migration):
    dependencies = [("shop", "0006_orders")]
    safety_exemptions = [MigrationRiskCode.RUN_SQL]
    operations = [ops.RunSQL("UPDATE shop_settings SET value = 'on' WHERE key = 'beta'")]
```

An entry that isn't a `MigrationRiskCode` raises `ConfigurationError`.

## <a id="rules"></a>The rules

The rules live in the dialects (`Dialect.migration_safety_rules`): every database has the shared
ones, PostgreSQL adds those of its locks. "Large" below means a table that may hold
`large_table_rows` rows or more.

| Code | Databases | Found |
| --- | --- | --- |
| `add_field_backfills_rows` | all | A NOT NULL field with only a Python default added to a large table |
| `add_field_volatile_default` | PostgreSQL | A field with a volatile database default added to a large table |
| `alter_field_rewrites_table` | all | A field change that rewrites a large table |
| `add_index_without_concurrently` | PostgreSQL | An index built on a large table while its writes wait |
| `add_check_constraint_validates_rows` | PostgreSQL | A CHECK constraint added to a large table without `not_valid=True` |
| `add_foreign_key_validates_rows` | PostgreSQL | A FOREIGN KEY added to a large table without `not_valid=True` |
| `add_unique_constraint_builds_index` | PostgreSQL | A unique constraint whose index is built on a large table |
| `set_not_null_scans_table` | PostgreSQL | NOT NULL set by scanning a large table while it is locked |
| `rename_field` | all | A field renamed together with its column |
| `remove_field` | all | A field removed together with its column |
| `rename_model` | all | A table renamed |
| `delete_model` | all | A model deleted together with its table |
| `run_sql` | all | Raw SQL |
| `schema_change_with_run_python` | all | Python code in the transaction that changes the schema of a large table |

### <a id="add-field-backfills-rows"></a>`add_field_backfills_rows`

`AddField` of a NOT NULL field whose only default is a Python `default=` (or `auto_now`) adds the
column nullable, updates every existing row, then makes it NOT NULL — all inside the migration,
holding the table. **Safely:** add it nullable, fill it with
[`BackfillColumn`](#backfillcolumn-and-altercolumnnotnullsafe) in a migration with `atomic = False`,
then make it NOT NULL with `AlterColumnNotNullSafe` ([pattern 1](#adding-a-required-column)) — or
give it a constant `db_default`, which the database stores once without touching the rows.

### <a id="add-field-volatile-default"></a>`add_field_volatile_default`

A database default calling a volatile function — `random()`, `gen_random_uuid()`, `uuidv7()`,
`clock_timestamp()`, `nextval()`... — is evaluated for every existing row: PostgreSQL rewrites the
table. A constant or a stable function (`Now()`) is stored once. **Safely:** add the field nullable
without `db_default`, set the `db_default` with `AlterField` (new rows get it), fill the existing rows
with `BackfillColumn`, then `AlterColumnNotNullSafe`.

### <a id="alter-field-rewrites-table"></a>`alter_field_rewrites_table`

A field change the database makes by rewriting the table, which can't be read or written meanwhile.
The dialect's schema editor tells which (`rewrites_table_on_alter()`): PostgreSQL rewrites for a
column type change, except to a type that holds every old value as it is — a longer or unlimited
`varchar`, `varchar` to `text` and back to an unlimited one, `numeric` with more digits and the same
scale, `cidr` to `inet`; SQLite rebuilds the table for every change but a plain rename and one that
leaves the column declared as it was (a Python-side option such as `sensitive`, the field's own
index); ClickHouse rewrites for every column type change. **Safely:**
add a field of the new definition, fill it in batches, switch the code to it, and remove the old one
([pattern 2](#renaming-a-column)).

### <a id="add-index-without-concurrently"></a>`add_index_without_concurrently`

`AddIndex` without `concurrently=True`, and the index of a field added (or altered to get one) with
`db_index=True` — a foreign key gets one by default — are built while the table's writes wait.
**Safely:** `AddIndex(..., concurrently=True)` in a migration with `atomic = False`
([pattern 3](#adding-an-index)); for a field, declare it with `db_index=False` and add its index in
`Meta.indexes` that way.

### <a id="add-check-constraint-validates-rows"></a>`add_check_constraint_validates_rows`

`AddConstraint` of a `CheckConstraint` checks every row while the table can't be read or written.
**Safely:** `AddConstraint(..., not_valid=True)` — only new and updated rows are checked — and
`ValidateConstraint` in a later migration, which doesn't block writes
([pattern 4](#adding-a-constraint)).

### <a id="add-foreign-key-validates-rows"></a>`add_foreign_key_validates_rows`

`AddField` of a `ForeignKeyField`/`OneToOneField` with a database constraint checks every row while the
writes to both tables wait; so does an `AlterField` giving a relation a constraint or another target.
**Safely:** `AddField(..., not_valid=True)` and `ValidateConstraint` with the foreign key's name later
([pattern 4](#adding-a-constraint)); an `AlterField` goes into a `SeparateDatabaseAndState` with the
constraint added `NOT VALID` by `RunSQL`.

### <a id="add-unique-constraint-builds-index"></a>`add_unique_constraint_builds_index`

`AddConstraint` of a `UniqueConstraint`, and a field added or altered with `unique=True`, build the
constraint's unique index while the table's writes wait. **Safely:** build the index first with
`AddIndex(..., unique=True, concurrently=True)`, then let the constraint take it over with
`AddConstraint(..., using_index=...)` ([pattern 4](#adding-a-constraint)). A unique constraint with a
`condition` can't take over an index — declare it as a unique `PartialIndex` instead.

### <a id="set-not-null-scans-table"></a>`set_not_null_scans_table`

`AlterField` from `null=True` to `null=False` scans the table while it can't be read or written; so
does `AlterColumnNotNullSafe` inside a transaction. **Safely:** `AlterColumnNotNullSafe` in a
migration with `atomic = False` ([below](#backfillcolumn-and-altercolumnnotnullsafe)).

### <a id="rename-field"></a>`rename_field` and `rename_model`

A field renamed with its column (`RenameField`), a model renamed with its table (`RenameModel` of a
model without `Meta.table`), or a changed `Meta.table` (`AlterModelTable`): the code still running
uses the old name and fails until it is replaced. A rename that keeps the column or table changes
nothing in the database and isn't reported. **Safely:** keep the name in the database —
`source_field="<old column>"` on the field, `Meta.table = "<old table>"` on the model — or move to a
new column ([pattern 2](#renaming-a-column)).

### <a id="remove-field"></a>`remove_field` and `delete_model`

`RemoveField` drops the column, `DeleteModel` the table, while the code still running reads them.
**Safely:** remove them from the models first — `SeparateDatabaseAndState(state_operations=[RemoveField(...)])`,
`DeleteModel(..., state_only=True)` — and drop the column or table with `RunSQL` in a later migration,
once no running code reads it ([pattern 5](#removing-a-field)).

### <a id="run-sql"></a>`run_sql`

`RunSQL` and `SQLOperation`: the check can't tell which tables the SQL locks, or for how long.
**Safely:** read the statements through, then [exempt](#exempting-a-risk) the code in the migration.

### <a id="schema-change-with-run-python"></a>`schema_change_with_run_python`

`RunPython` in an atomic migration that also changes the schema of a large table, on a database whose
schema changes are transactional: the locks the schema changes take are held until the transaction
ends — for as long as the code runs. **Safely:** move the `RunPython` into a migration of its own, or
set `atomic = False` on the migration.

## <a id="backfillcolumn-and-altercolumnnotnullsafe"></a>BackfillColumn and AlterColumnNotNullSafe

```python
BackfillColumn(model_name: str, field_name: str, value: Any | Callable[[], Any], *, batch_size: int = 1000)
AlterColumnNotNullSafe(model_name: str, field_name: str)
```

`BackfillColumn` runs a loop of bounded `UPDATE`s (at most `batch_size` rows per statement,
repeated until none remain) instead of one giant `UPDATE` across the whole table. `value` is
either a literal or a zero-argument callable; the callable is evaluated **once** (not once per
row), so it's for "the value to use for this migration run" (e.g. a timestamp captured at run
time), not a per-row-derived value — it can't reference another column. It only changes data, so
`state_forward()` is a no-op, and it's not meaningfully reversible (the NULLs it overwrote aren't
recoverable) — `migrate` to an earlier migration refuses to unapply it. `field_name` may name a
foreign key by its relation name (`"author"`) as well as its column field (`"author_id"`); `value`
is then the related row's primary key (a model instance is accepted too). A name without a single
database column raises `ConfigurationError`. A batch picks its rows by the primary key — every
column of a composite one — and, for a model without one, by the row's own name in the database
(`ctid` on PostgreSQL, `rowid` on SQLite); a database with no such name raises `ConfigurationError`
for a model without a primary key.

`AlterColumnNotNullSafe` makes a column NOT NULL after checking that no row holds `NULL` there: a
row that does raises `hare.exceptions.ConfigurationError` naming the model and field and suggesting
`BackfillColumn`, instead of the database's NOT NULL violation. How it does it:

- **PostgreSQL, migration with `atomic = False`** — the table is never scanned while it is locked:
  a `CHECK (column IS NOT NULL)` is added `NOT VALID` (no scan), validated with
  `VALIDATE CONSTRAINT` (a scan that doesn't block writes), the column is set NOT NULL — PostgreSQL
  reads it off the validated check instead of the rows — and the check is dropped. A `NULL` found by
  the validation drops the check again before the error.
- **PostgreSQL, atomic migration** — `LOCK TABLE ... IN SHARE ROW EXCLUSIVE MODE` for the rest of the
  transaction, the `NULL` check, then `ALTER COLUMN ... SET NOT NULL`: no `NULL` slips in between, but
  writes wait while the table is scanned (the [`set_not_null_scans_table`](#set-not-null-scans-table)
  risk).
- **SQLite** — the `NULL` check, then the table rebuilt with the column NOT NULL.
- **ClickHouse** — the `NULL` check, then the column altered the way `AlterField` alters it.

## <a id="adding-a-required-column"></a>Pattern 1: adding a required column to a big table

Splitting this into three separate migrations means no single migration blocks the table for the
full duration of the bulk backfill:

```python
# 0004_add_status_nullable.py
class Migration(Migration):
    operations = [
        AddField("Order", "status", CharField(max_length=20, null=True)),
    ]

# 0005_backfill_status.py
class Migration(Migration):
    dependencies = [("models", "0004_add_status_nullable")]
    atomic = False
    operations = [
        BackfillColumn("Order", "status", value="pending", batch_size=5000),
    ]

# 0006_status_not_null.py
class Migration(Migration):
    dependencies = [("models", "0005_backfill_status")]
    atomic = False
    operations = [
        AlterColumnNotNullSafe("Order", "status"),
    ]
```

## <a id="renaming-a-column"></a>Pattern 2: renaming a column via dual-write

There's no `RenameColumnSafe` operation — a real zero-downtime rename is a 4-step recipe built
from existing operations (`AddField`/`RemoveField`) plus `BackfillColumn`, with an
application-code dual-write period in between that's not hare-orm's concern at all:

1. **Expand** — add the new column; old readers/writers stay unaffected:

   ```python
   operations = [AddField("Order", "customer_email", CharField(max_length=255, null=True))]
   ```

2. **Backfill** — populate the new column for existing rows. `BackfillColumn` fits when every row
   gets the same value (a shared default/placeholder); when the new column has to carry each row's
   *own* value copied from the old one, use a `RunPython` data migration issuing a batched
   `UPDATE ... SET customer_email = email WHERE customer_email IS NULL` instead — `BackfillColumn`
   only ever writes one value across a whole batch, it can't reference another column per row.

   ```python
   async def backfill_customer_email(apps, schema_editor):
       while True:
           rowcount, _ = await schema_editor.client.execute(
               'UPDATE "order" SET "customer_email" = "email" '
               'WHERE "id" IN (SELECT "id" FROM "order" WHERE "customer_email" IS NULL LIMIT 5000)'
           )
           if not rowcount:
               break

   operations = [RunPython(backfill_customer_email, RunPython.noop)]
   ```

3. **Dual-write** — deploy application code that writes both columns for a while, so old and new
   readers both see current data (a plain `Model.save()` override, or the equivalent wherever rows
   get written):

   ```python
   class Order(Model):
       email = fields.CharField(max_length=255)
       customer_email = fields.CharField(max_length=255, null=True)

       async def save(self, *args, **kwargs):
           self.customer_email = self.email
           await super().save(*args, **kwargs)
   ```

4. **Contract** — once every reader/writer has shipped and every row has been backfilled, remove the
   old column the way [pattern 5](#removing-a-field) does.

Each step ships and bakes in production before the next one runs — that's what makes the whole
thing zero-downtime, not any machinery inside hare-orm itself.

## <a id="adding-an-index"></a>Pattern 3: adding an index

`CREATE INDEX CONCURRENTLY` builds the index while the table is written to; it can't run inside a
transaction, so the migration is not atomic:

```python
class Migration(Migration):
    dependencies = [("models", "0006_status_not_null")]
    atomic = False
    operations = [
        AddIndex("Order", Index(fields=("status",), name="order_status"), concurrently=True),
    ]
```

## <a id="adding-a-constraint"></a>Pattern 4: adding a constraint

A CHECK constraint or a foreign key is added unvalidated — only new and updated rows are checked,
nothing is scanned — and the existing rows are checked by `ValidateConstraint` in a later migration,
with a lock that doesn't block writes:

```python
# 0008_constraints.py
operations = [
    AddConstraint("Order", CheckConstraint(check=Q(total__gte=0), name="order_total_positive"), not_valid=True),
    AddField("Order", "coupon", ForeignKeyField("models.Coupon", null=True, db_index=False), not_valid=True),
]

# 0009_validate_constraints.py
operations = [
    ValidateConstraint("Order", "order_total_positive"),
    ValidateConstraint("Order", "fk_order_coupon_4b1c0d2a"),  # the foreign key's name, as the database shows it
]
```

A unique constraint takes over a unique index built concurrently — `UNIQUE USING INDEX` renames the
index to the constraint's name and checks nothing again:

```python
class Migration(Migration):
    atomic = False
    operations = [
        AddIndex("Order", Index(fields=("number",), name="order_number_unique", unique=True), concurrently=True),
        AddConstraint(
            "Order", UniqueConstraint(fields=("number",), name="order_number_unique"), using_index="order_number_unique"
        ),
    ]
```

The model declares only the constraint: `using_index` takes the index out of the model's
`Meta.indexes` in the migration state.

## <a id="removing-a-field"></a>Pattern 5: removing a field or a model

The field leaves the models first; the column is dropped once no running code reads it:

```python
# 0010_forget_legacy_code.py - deployed with the code that no longer has the field
operations = [
    SeparateDatabaseAndState(state_operations=[RemoveField(model_name="Order", name="legacy_code")]),
]

# 0011_drop_legacy_code.py - after the deployment
safety_exemptions = [MigrationRiskCode.RUN_SQL]
operations = [
    RunSQL('ALTER TABLE "order" DROP COLUMN "legacy_code"', reverse_sql=RunSQL.noop),
]
```

A model the same way: `DeleteModel("Order", state_only=True)` first, then `DROP TABLE` by `RunSQL`.
On SQLite, drop the column before another change of that table rebuilds it — a rebuild keeps only the
columns the migration state knows (see `RemoveField` in [Operations](operations.md)).
