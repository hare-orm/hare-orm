# Soft delete

`Meta.soft_delete_field` turns `delete()` into marking the row deleted: the row stays in the table, and
every query leaves it out unless it asks for deleted rows.

```python
class Book(Model):
    title = fields.CharField(max_length=200)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        soft_delete_field = "deleted_at"
```

The field must exist on the model and allow `None`.

## <a id="deleting"></a>Deleting: `delete()`

- **`delete()`** becomes an `UPDATE` setting `deleted_at = now()`, run in the same transaction as
  cascading that same soft-delete decision to related rows — instead of a real `DELETE`. This applies to
  `QuerySet.filter(...).delete()` (bulk delete) too, not just a single instance's `delete()`: a model
  nothing references converts to one bulk `UPDATE`; otherwise the matched rows and their cascade are
  soft-deleted in batches — a few queries per wave of related rows, not per row. Every row one `delete()`
  soft-deletes, cascaded ones included, gets the same `deleted_at`. The rows being deleted are locked
  first (`SELECT ... FOR NO KEY UPDATE` on PostgreSQL): a row another `delete()` soft-deleted after this
  instance or queryset read it — a stale instance, or two deletes racing — is left alone, keeps its
  original `deleted_at`, isn't cascaded again and isn't counted. A stale instance takes the row's
  `deleted_at` instead.
- A row the cascade reaches whose model has no `soft_delete_field` of its own is **kept**, still pointing
  at the deleted row, and so are the rows of auto-generated M2M through tables: they read as no related
  row while it is deleted (see [What queries see](#reading)) and point at it again once it is restored.
  `Meta.soft_delete_hard_cascade = True` on the model being soft-deleted makes its soft delete really
  delete them instead, as a hard delete would — `restore()` can't bring them back then. Either way
  `SET_NULL`/`SET_DEFAULT` relations are updated and `PROTECT`/`RESTRICT` ones block the delete.
- `.delete()` of a queryset soft-deletes only the rows that are still live: a row that is already deleted
  keeps its original `deleted_at` and isn't counted, exactly as `instance.delete()` leaves an
  already-deleted instance alone (so `only_deleted().delete()` returns `0`). The instance check reads
  `deleted_at` from the database when `.only()`/`.defer()` left it unloaded.
- A cascade reaches related rows of every tenant, and deletes them whether they are soft-deleted or
  removed with a real `DELETE` — the active `Tenancy.scope()` is only checked against the instance
  `delete()` is called on. A related model that overrides `delete()` has that override called inside
  `Tenancy.scope()` of the row's own tenant.
- After `delete()`/`restore()`, `get_dirty_fields()` doesn't report the `optimistic_lock_field` and
  `auto_now` fields the soft-delete `UPDATE` wrote along with `soft_delete_field`.

## <a id="restoring"></a>Restoring: `restore()`

**`restore()`** reverses the delete, setting the field back to `None`. By default only that row comes
back. **`restore(cascade=True)`** also restores the rows its soft delete removed along with it: every row
`on_delete=CASCADE` leads to that still carries **this row's deletion time** — the cascade stamps one
time on everything it removes, so that time identifies the delete. A row soft-deleted on its own, before
or after, stays deleted, and so do the rows below it; values `SET_NULL`/`SET_DEFAULT` overwrote are not
brought back.

## <a id="hard-delete"></a>Deleting for real: `hard_delete()`

**`hard_delete()`** removes the row for real, whether it is live or already soft-deleted (emptying a
trash); related rows follow their `on_delete` as a real `DELETE` would — an `on_delete=CASCADE` row is
deleted for real too, also of a model with a `soft_delete_field` and through a `db_constraint=False`
relation. A delete of a model without a `soft_delete_field` cascades the same way.
`QuerySet.hard_delete()` does the same for every row a queryset matches:

```python
await Book.objects.only_deleted().filter(deleted_at__lt=cutoff).hard_delete()
```

## <a id="reading"></a>What queries see

- The default manager adds `WHERE deleted_at IS NULL` to every query (`.filter()`, `.all()`, ...).
- A filter from another model across a reverse relation skips soft-deleted related rows too, whether
  nested (`Author.objects.filter(books__title=...)`) or on the relation itself (`books__isnull=...`,
  `books=book`, `books__in=[...]`, and their `exclude()` forms).
- A forward FK/O2O pointing at a soft-deleted row (or at a row of another tenant, or one a custom
  `Meta.manager` filters out — see [Multi-tenancy](multi-tenancy.md)) reads as no related row:
  `book.author`, `select_related()`/`prefetch_related()` and `values("author__name")` give `None`, a
  filter through it (`Book.objects.filter(author__name=...)`) doesn't match it, and the relation-level
  null checks agree — `author__isnull=True` (and `author=None`, `author__not_isnull=False`) matches such
  a book, `author__isnull=False` doesn't; so does `author__name__isnull=True` (`author__name=None`),
  since a hidden target has no visible columns. The key column itself keeps comparing the stored value:
  `author_id__isnull`, `author=obj` and `author_id=...` see the key as written, deleted target or not.
  With `include_deleted()`/`only_deleted()` on the filtered queryset the target is visible again, so
  `author__isnull` is a plain key check there.

**`Model.objects.include_deleted()`** returns a `QuerySet` that skips that filter:

```python
await Book.objects.include_deleted().filter(id=book_id).first()
```

**`Model.objects.only_deleted()`** / **`QuerySet.only_deleted()`** return only the soft-deleted rows
(`WHERE deleted_at IS NOT NULL`) — a trash view to restore from:

```python
for book in await Book.objects.only_deleted().filter(shelf=shelf):
    await book.restore()
```

`only_deleted()` and `include_deleted()` replace each other — the last call wins
(`Book.objects.only_deleted().include_deleted()` sees every row). Like `include_deleted()`, it keeps the
`Meta.tenant_field` filter (chain `.all_tenants()` to drop it) and lets relations reached from the query
(`select_related()`/`prefetch_related()`/a `relation__field` filter) see deleted rows too. It works on a
related manager's queryset as well: `author.books.all().only_deleted()`.

## <a id="writing-the-field"></a>Writing `soft_delete_field`

- `QuerySet.update(...)`, `bulk_update()`, `save(update_fields=[...])`, `merge().when_matched(update=...)`
  and `bulk_create(update_fields=[...])` refuse to write `soft_delete_field` (`QueryError`) — go through
  `.delete()`/`.restore()` instead, so the cascade logic runs. A plain `save()` never writes it either.
- An upsert (`bulk_create(update_fields=..., on_conflict=...)`) that conflicts with a soft-deleted row
  updates that row's other columns but leaves it deleted — `restore()` it to bring it back. So do
  `save()` and `bulk_update()` of instances read before their rows were soft-deleted elsewhere: both
  match a row by its key alone, as the instance was read; an `optimistic_lock_field` makes them raise
  `StaleObjectError` instead.
- `create()` and `bulk_create()` do accept `soft_delete_field` and `Meta.optimistic_lock_field` values,
  so a row can be inserted already deleted or at a given version — for importing or restoring data that
  was deleted or versioned elsewhere. `update()`, `save()` and `bulk_update()` don't write either:
  `update()`/`bulk_update()` raise `QueryError`, `save()` raises for a changed `soft_delete_field` and
  treats `optimistic_lock_field` as the version to check against. Don't pass a request's data straight
  into `create(**data)`/`bulk_create()` — pick the allowed fields first, or a client can insert a hidden
  (soft-deleted) row or preset its version.

## <a id="unique-among-live-rows"></a>Unique among live rows

hare does **not** rewrite a `unique=True` field or a `Meta.constraints` entry to exclude soft-deleted
rows: a deleted book still holds its ISBN. For "unique among non-deleted rows", add the condition
explicitly:

```python
from hare import Q
from hare.ddl import UniqueConstraint


class Book(Model):
    isbn = fields.CharField(max_length=13)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        soft_delete_field = "deleted_at"
        constraints = (UniqueConstraint(fields=("isbn",), condition=Q(deleted_at__isnull=True)),)
```

## <a id="optimistic-locking"></a>With `optimistic_lock_field`

`soft_delete_field` and [`optimistic_lock_field`](../models/model-methods.md#optimistic-locking) combine
on one model. A soft delete checks each row against the version the delete read it at and bumps it once;
a concurrent change raises `StaleObjectError`, carrying `.model`/`.pk`/ `.expected_version` — see
[Exceptions](../errors/exceptions.md). A row soft-deleted meanwhile is the exception: deleting it again is
a no-op, as for any soft-deleted row — the instance takes the row's deletion time, whatever version the
row reached.

## <a id="failed-write"></a>The instance after a failed write

`save()`/`delete()`/`restore()` update `auto_now`/`auto_now_add` and `soft_delete_field` on the Python
instance eagerly, before the write's outcome is known. If the write then fails — a `StaleObjectError`
from an `optimistic_lock_field` conflict, or any other exception mid-write — those in-memory values are
rolled back to match what's actually in the database, so the instance never ends up looking saved or
deleted when it wasn't.
