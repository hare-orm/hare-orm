# Model methods

`Model` (`hare.models.Model`) holds what one row does: saving, deleting, restoring, refreshing,
copying and comparing it. Queries start from [`Model.objects`](../querying/managers.md#model-objects) -
a `QuerySet`, with `create()`, `get()`, `filter()`, `bulk_create()` and everything else that reads
or writes many rows.

## Instance methods {: #instance-methods }

```python
async def save(
    self, using=None, update_fields: Iterable[str] | None = None,
    force_create: bool = False, force_update: bool = False,
) -> None
```
Creates the row if it has no pk / hasn't been saved yet, otherwise updates it. `update_fields`
restricts the `UPDATE` to those columns (a forward relation's name, like Django, stands for its key
column(s): `update_fields=["author"]` writes `author_id`); passing it on an instance that already has a pk always
issues an `UPDATE` by primary key — even for an instance never loaded or saved
(`Model(id=known_id, name="x").save(update_fields=["name"])`, an update without a fetch) — and raises
`IntegrityError` if no such row exists rather than creating it (use `create()` or
`save(force_create=True)` to insert); it is only ignored when there is no pk yet (a DB-generated
one), where the row is inserted. A `.only()`/`.defer()` instance that left
an `auto_now` field unloaded can still be saved (the field is only written, the fresh value is
loaded onto the instance), but one that left `Meta.optimistic_lock_field` unloaded can't. Raises `IncompleteInstanceError` for a partial
(`.only()`/`.defer()`) instance missing its pk or the fields being saved; `IntegrityError` if
`force_create`/`force_update` can't be satisfied; `StaleObjectError` if `Meta.optimistic_lock_field` detects
a concurrent modification; `QueryError` if both `force_create` and `force_update` are passed.

- `update_fields=[]` (any empty collection) is a no-op: nothing is written, whether the instance is
  saved, unsaved or partial.
- `force_create=True` always inserts the instance's current pk when it has one — also a
  DB-generated pk of a loaded or already saved instance — so an existing row raises
  `IntegrityError` instead of a duplicate being inserted under a new pk.
- A pk that is `None` but has a `default`/`db_default` (e.g. `UUIDField(primary_key=True)`) gets
  that default when the row is inserted, so copying a row with `obj.pk = None; await obj.save()`
  works for such a pk just like for a DB-generated one; `Model(id=None)`/`create(pk=None)` apply the
  default too.
- When no column is left to write (a model with only a pk, or whose other fields all wait for a
  `db_default`/are generated), `save()` of a saved instance and `save(force_update=True)` still check
  that the row exists and raise `IntegrityError` when it doesn't.

```python
async def delete(self, using=None) -> None
```
A real `DELETE` — or, if `Meta.soft_delete_field` is set, an `UPDATE` that sets the timestamp and
cascades that same decision to related rows. The cascade and the `PROTECT`/`RESTRICT` checks see
every related row physically referencing the deleted ones — in every tenant, soft-deleted or hidden
by a custom `Meta.manager` filter. Whatever part of the cascade the database can't run itself (a
soft delete, a `db_constraint=False` relation) runs in Python, in the same transaction, wave by wave:
each wave costs one query per relation and model, however many rows it reaches. A related model that
overrides `delete()` has that override called for each of its rows. Raises `QueryError` (never
saved), `IncompleteInstanceError` (pk not loaded), `ProtectedError` (a `PROTECT` relation blocks it),
`IntegrityError`, `StaleObjectError`.

```python
async def delete_preview(self, *, using=None) -> DeletePreview
```
Reports what `delete()` would do, without writing anything — for a delete confirmation screen. It
walks the same transitive `CASCADE` tree as `delete()` itself, with the same soft-delete dispatch
per model, every tenant's rows that point at the deleted ones (like `delete()`, not just the
active tenant's), soft-deleted related rows, and related rows a custom `Meta.manager` filter hides
(like `delete()` itself). `DeletePreview` (importable from `hare.models`)
holds:

| Attribute | Meaning |
|---|---|
| `deleted: dict[type[Model], int]` | rows physically removed, **the instance itself included** |
| `soft_deleted: dict[type[Model], int]` | rows marked deleted via `Meta.soft_delete_field` (already soft-deleted ones are not counted) |
| `nulled: dict[type[Model], int]` | surviving rows whose `SET_NULL` FK becomes `NULL` |
| `set_default: dict[type[Model], int]` | surviving rows whose `SET_DEFAULT` FK is reset |
| `m2m_through: dict[str, int]` | rows removed from auto-generated M2M through tables, keyed by table name |
| `m2m_through_nulled: dict[str, int]` | auto-generated through rows whose key column becomes `NULL` (M2M `SET_NULL`) |
| `protected_by: list[Model]` | rows blocking the delete via `PROTECT`, transitively; empty if nothing protects it |
| `restricted_by: list[Model]` | rows blocking the delete via `RESTRICT`/`NO_ACTION` |
| `can_delete: bool` | `True` when both lists above are empty |

A `through=Model` M2M relation is an ordinary FK of its through model, so its rows show up under that
model in `deleted`/`nulled`. Every count is of distinct rows; a row the same delete removes is not
also counted in `nulled`/`set_default`. A row reached from two sides (diamond, both ends of an M2M
row) is counted once. An already soft-deleted instance gets an empty preview, since `delete()` is a
no-op for it — also when `.only()`/`.defer()` left `soft_delete_field` unloaded (it is read from the
database then). Blocking rows follow what the database really does: a `RESTRICT`/`NO_ACTION` row that
the same `DELETE` statement removes as well (a diamond: the row is cascaded away through one parent
and restricts another) doesn't block — `NO_ACTION` is checked at the end of the statement on every
backend, `RESTRICT` too on Postgres, while SQLite checks `RESTRICT` at once, so there such a row still
blocks. A row that is soft-deleted in the same cascade stays in the table, so its `PROTECT`/`RESTRICT`
still blocks. Raises the same `QueryError` (tenant scope mismatch, never saved) and
`IncompleteInstanceError` as `delete()`. The preview is a snapshot: rows changed concurrently
between the preview and the real `delete()` are not accounted for.

```python
preview = await author.delete_preview()
if not preview.can_delete:
    raise ValidationError(f"blocked by {preview.protected_by + preview.restricted_by}")
print(preview.deleted)  # {Author: 1, Book: 2, Chapter: 3}
```

```python
async def restore(self, using=None, *, cascade: bool = False) -> None
```
Reverses a soft delete. By default only this row comes back. `cascade=True` also restores the rows
the soft delete of this one removed along with it: every row `on_delete=CASCADE` leads to that
still carries **this row's deletion time**. A row soft-deleted on its own - before or after - stays
deleted, and so do the rows below it; values `SET_NULL`/`SET_DEFAULT` overwrote are not brought
back. Raises `QueryError` if the model has no `Meta.soft_delete_field` or the instance was never
saved. See [Soft delete](../soft-delete-versions-tenants/soft-delete.md).

```python
async def hard_delete(self, using=None) -> None
```
A real `DELETE` even when `Meta.soft_delete_field` is set, including for a row that is already
soft-deleted. Related rows follow their `on_delete` as for a model without soft delete; `PROTECT`
still raises `ProtectedError`. On a model without soft delete it is the same as `delete()`.

```python
async def refresh_from_db(self, fields: Iterable[str] | None = None, using=None) -> None
```
`refresh_from_db` reloads all (or the named) fields from the database in place, and loads again
every `lazy=` relation the refresh reset (a forward FK/O2O whose key column it reloaded, or an M2M
on a full refresh). The row is looked up without the `Meta.manager` filter (tenant isolation still
applies), so an existing row that filter hides is refreshed instead of raising `DoesNotExist`.
`fields=[]` (an empty collection) is a no-op.

To load relations of instances you already hold, use
[`prefetch_related_objects()`](relations.md#prefetch-related-objects):
`await prefetch_related_objects([book], "author", "tags")`.

```python
def update_from_dict(self, data: dict) -> Self
```
Mass-assign from a dict, ignoring unknown keys, converting types as needed; `pk` sets the primary
key as `Model(pk=...)` does. Raises `QueryError` for a reverse/M2M key, `ValidationError` for
a value the field rejects, `QueryError` for `None` in a non-null field.

```python
def clone(self, pk=EMPTY) -> Self
```
Returns a new, unsaved copy (deep-copying mutable field values). For a composite primary key, each
component is resolved independently — a component with its own `default=`/`db_default=` doesn't need
an explicit value; only components with neither need to come from `pk=(...)`. A single-column pk that's
neither `generated=True` nor has a `default` needs an explicit `pk=` too — otherwise `QueryError`.
An explicit `pk=` is always used, even for an auto-generated pk, and every `auto_now_add` field is reset
so the new row gets its own first-save value (set one on the clone before `save()` to keep it).

```python
def to_dict(self) -> dict[str, Any]
```
The instance's direct field values as a plain dict, keyed by model field name (not DB column
name) — equivalent to `dict(self)`. A relation field itself isn't included, but an FK/O2O's own
shadow id column is (e.g. `author_id`, not `author`), so this never touches an unfetched relation.
A direct field that a `.only()`/`.defer()` query left unloaded raises `NoValuesFetched` naming it.

Also: `.pk` (get/set — a tuple for a composite key), `__str__` (`"Tournament object (1)"`, as in Django)/`__repr__` (`<Tournament: 1>`), `__eq__`, `__hash__`
(`TypeError` if the pk isn't set yet), `__iter__` (yields `(field_name, value)` pairs — same shape
as `to_dict()`), `__await__` (awaiting an already-loaded instance just returns itself).

## Tracking changes {: #tracking-changes }

### `get_dirty_fields()` {: #get-dirty-fields }

```python
class Meta:
    track_dirty_fields = True
```

Opt-in (default `False`) — enabling it costs a per-hydration snapshot, so only turn it on for models
that actually need it.

```python
def get_dirty_fields(self) -> dict[str, tuple[Any, Any]]
```

```python
user = await User.objects.get(pk=1)
user.name = "New Name"
user.get_dirty_fields()  # {"name": ("Old Name", "New Name")}
```

Returns `{field: (old_value, new_value)}` for every direct field that changed since the object was
last hydrated from the database, or last `save()`d. A brand-new, unsaved instance reports every set
field as dirty relative to `None`. Raises `QueryError` if `Meta.track_dirty_fields` isn't
set.

A `db_default` placeholder not yet replaced by a real value, and an expression assigned for the
next write (`counter = F("counter") + 1`), are compared by identity: they aren't dirty against
themselves, and after `save()` the expression stays on the instance as clean until
`refresh_from_db()` reads the written value. The same holds for `snapshot()`/`diff_against()`.
When a transaction containing `save()`/`delete()`/`restore()` rolls back, the dirty baseline
goes back to what it was before the write.

This is the mechanism to reach for instead of a manual "fetch a fresh copy and diff `to_dict()`
against it" pattern before publishing an event — the diff is computed for you, from an in-memory
snapshot rather than an extra `SELECT`.

It is built on the same copy/compare logic as `snapshot()`/`diff_against()` below.

### `snapshot()` / `diff_against()` {: #snapshot }

```python
def snapshot(self, fields: Iterable[str] | None = None) -> FieldSnapshot
def diff_against(self, snapshot: FieldSnapshot) -> dict[str, tuple[Any, Any]]
```
A manual "before/after" pair for audit logs, approvals or undo — works on **any** model, no
`Meta.track_dirty_fields` needed. `snapshot()` captures the direct field values (every loaded
direct field by default, or just `fields=`) into an immutable `FieldSnapshot` (`from hare.models
import FieldSnapshot`) — a read-only mapping with `.fields` and `.model_class`. `diff_against()`
returns `{field: (snapshot_value, current_value)}` for the snapshotted fields that changed since.

```python
snapshot = order.snapshot()
order.config["url"] = "https://new"      # in-place JSON edit
order.status = "approved"
order.diff_against(snapshot)
# {"config": ({"url": "https://old"}, {"url": "https://new"}), "status": ("draft", "approved")}

order.snapshot(fields=["status"])        # only these fields are captured and compared
```

- Mutable values (`dict`, `list`, `set`, `bytearray`, other objects) are deep-copied at capture time,
  so an in-place edit like `obj.config["url"] = ...` shows up in the diff. Immutable values
  (`int`, `float`, `str`, `bool`, `bytes`, `None`, `datetime`/`date`/`time`/`timedelta`, `Decimal`,
  `UUID`, `Enum`) are stored as-is, not copied. `clone()` and `track_dirty_fields` use the same rule.
- A JSON value that only changes type — `{"a": 1}` to `{"a": True}` or `{"a": 1.0}`, at any nesting
  depth, or a JSON field's top-level `1` to `True` — counts as changed, since the database stores it
  differently. Other values compare with `==` (`Decimal("1.50")` and `Decimal("1.5")` are equal).
  `get_dirty_fields()` uses the same comparison.
- Nothing happens automatically: loading and `save()` never take a snapshot unless the model has
  `Meta.track_dirty_fields`, so models that don't call `snapshot()` pay nothing.
- `fields=` takes any iterable of direct field names (an FK's shadow column such as `author_id`,
  not the relation `author`). An unknown name raises `FieldError`, a bare string raises `TypeError`,
  and a field that a `.only()`/`.defer()` query didn't load raises `IncompleteInstanceError` (the
  default `fields=None` just skips unloaded fields).
- `diff_against()` accepts a snapshot of another instance of the same model too (compare two rows);
  a snapshot of a different model, or anything that isn't a `FieldSnapshot`, raises `TypeError`.
- The snapshot is independent of the instance: `save()`/`refresh_from_db()` don't update it.
- `snapshot` and `diff_against` are reserved names — a model can't declare a field with either name.

## Classmethods {: #classmethods }

```python
def get_connection(cls, for_write: bool = False) -> DatabaseClient
```
The connection a query on the model would run on now: the configured
[router](../connections/multiple-databases.md#routers)'s choice (`db_for_write` with `for_write=True`,
`db_for_read` otherwise), else the model's default connection. Inside an open transaction on that
connection it is the transaction's client. Raises `ConfigurationError` without an active Hare context and
`ConfigurationError` for a model swapped out by its `swappable` setting.
`QuerySet.get_connection()` answers the same for one queryset, taking its `using()` into account.

Pin queries to it with `using=`/`.using()`, and read its `features` to use only what the
database supports - for example, lock a row where the database locks rows (SQLite serializes
writers instead and rejects `select_for_update()`):

```python
connection = Order.get_connection(for_write=True)
async with Transactions.atomic(connection.connection_name) as transaction:
    queryset = Order.objects.filter(pk=order_id).using(transaction)
    if transaction.features.supports_select_for_update:
        queryset = queryset.select_for_update()
    order = await queryset.get()
    ...
```

```python
def construct(cls, _saved_in_db: bool = False, **kwargs) -> Self
```
Builds a "detached" instance with **no** validation, no DB checks, no FK-saved checks — for tests
and deserialization. Nested relation values passed as plain values/lists are wrapped so they read
back as already-fetched. An unsaved instance given an auto-generated pk is inserted with that pk;
`get_dirty_fields()` on a `track_dirty_fields` model reports every field until the first `save()`.

```python
await Model[some_pk]  # -> Self, or raises DoesNotExist
```
`Model[pk]` is `Model.objects.get(pk=pk)`; a key that can't be a value of the primary key field
(`Book["abc"]` for an integer key) is `DoesNotExist` too.

## `Model._meta` {: #model-meta }

`Model._meta` describes the model's table and fields. These attributes are public and read-only -
set them through `Meta` options, never by assigning to `_meta`:

| Attribute | Holds |
|---|---|
| `full_name` | `"app.ModelName"` - the label relations and `swappable` settings name the model by. |
| `app` | The app label. |
| `db_table` | The table name - `Meta.table`, or the lower-cased class name. |
| `schema` | The database schema (`Meta.schema`), or `None`. |
| `default_connection` | The alias of the connection the model's app is configured on. |
| `pk_attr` | The primary key field's name; a tuple of names for a `CompositePrimaryKey`; `()` for a model with `Meta.primary_key = None`. |
| `pk_attr_names` | `pk_attr` as a tuple in every case - `("id",)`, `("id", "version")`, `()`. |
| `pk` | The primary key field; `None` for a composite key or no key. |
| `pk_fields` | The fields of a composite primary key, in order; `()` otherwise. |
| `has_primary_key` / `has_composite_primary_key` | Whether the model has a primary key / a composite one. |
| `fields_map` | `{name: Field}` of every field: data fields, relations (forward, backward, M2M) and the key columns of forward relations (`author_id`). |
| `fields` | The names in `fields_map`. |
| `fields_db_projection` | `{field name: column name}` of every field that has a column. |
| `db_fields` | The table's column names, in declaration order. |
| `fk_fields` / `o2o_fields` / `m2m_fields` | Names of the forward `ForeignKeyField`s / `OneToOneField`s / `ManyToManyField`s. |
| `backward_fk_fields` / `backward_o2o_fields` | Names of the backward relations - the `related_name`s other models' FK/O2O fields give this one. |
| `fetch_fields` | Names of every relation above - what `prefetch_related()`/`prefetch_related_objects()` accept. |
| `sensitive_fields` | Names of the fields declared `sensitive=True` (see [Sensitive fields](encrypted-and-sensitive-fields.md#sensitive-fields)). |
| `soft_delete_field` / `tenant_field` / `optimistic_lock_field` | The field names `Meta` declares for these, or `None`. |
| `managed` | `Meta.managed`. |

A field tells its own type through its attributes - `relation_type`, `encrypted`, `enum_type`
(see [The base `Field` class](field-types.md#the-base-field-class)):

```python
from hare.fields import RelationType

for name, field in Book._meta.fields_map.items():
    if field.relation_type is RelationType.MANY_TO_MANY:
        ...
```

`get_lookup_info()`, `get_lookups()` and `get_ordering_info()` describe filter keys and orderings -
see [Describing filters and orderings](../querying/describing-filters.md). Together with `fields_map` they are the description of a
model: each field's constructor arguments come from `field.deconstruct()` (`(path, args, kwargs)` -
what a migration file writes), its Python type from `field.get_annotation()`.
