# Zero-downtime migrations (expand-contract)

`BackfillColumn` and `AlterColumnNotNullSafe` (`hare.migrations.operations`) split a schema
change that would otherwise hold a lock/transaction open for as long as it takes to touch every
row of a big table into several small, independently-safe migrations — the "expand-contract"
pattern.

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
database column raises `ConfigurationError`.

`AlterColumnNotNullSafe` runs the same `ALTER COLUMN ... SET NOT NULL` `AlterField` would, but
first checks the column for any remaining `NULL`s and raises `hare.exceptions.ConfigurationError`
— naming the model/field and suggesting `BackfillColumn` — instead of letting the raw
NOT-NULL-violation surface straight from the database. On Postgres, running this migration with
`atomic=True` (the default) closes the gap between that check and the `ALTER`: it takes a
`LOCK TABLE ... IN SHARE ROW EXCLUSIVE MODE` for the rest of the migration's transaction before
checking, so a concurrent writer can't insert a new `NULL` in between. With `atomic=False`, or on
SQLite (no equivalent table-level lock there), that gap is real — run this operation during a
maintenance window in that case, or add a `CHECK (col IS NOT NULL)` constraint first if you need
an airtight guarantee regardless of atomicity.

## Pattern 1: adding a required column to a big table {: #adding-a-required-column }

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
    operations = [
        BackfillColumn("Order", "status", value="pending", batch_size=5000),
    ]

# 0006_status_not_null.py
class Migration(Migration):
    dependencies = [("models", "0005_backfill_status")]
    operations = [
        AlterColumnNotNullSafe("Order", "status"),
    ]
```

## Pattern 2: renaming a column via dual-write {: #renaming-a-column }

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

4. **Contract** — once every reader/writer has shipped and every row has been backfilled, drop the
   old column:

   ```python
   operations = [RemoveField("Order", "email")]
   ```

Each step ships and bakes in production before the next one runs — that's what makes the whole
thing zero-downtime, not any machinery inside hare-orm itself.
