# Relations

## `ForeignKeyField` {: #foreignkeyfield }

```python
def ForeignKeyField(
    to: type[TModel] | str | SwappableModelReference,
    related_name: str | None | Literal[False] = None,
    on_delete: OnDelete = CASCADE,
    db_constraint: bool = True,
    null: bool = False,
    **kwargs: Any,
) -> ForeignKeyRelation[TModel] | ForeignKeyNullableRelation[TModel]
```

`to_field`, `db_index` and `lazy` are passed as keyword arguments, together with the
[base `Field` arguments](field-types.md#the-base-field-class).

```python
class Book(Model):
    author = fields.ForeignKeyField("models.Author", related_name="books", on_delete=fields.CASCADE)
```

| kwarg | Meaning |
|---|---|
| `to` | The related model class, a `"app.Model"` string, or `swappable("SETTING")` (see [Swappable models](#swappable-models)). |
| `related_name` | The reverse accessor name on the related model. `False` creates no reverse accessor (and no reverse filter or `prefetch_related()` name) - `on_delete` still applies to the rows pointing back, exactly as with a named one; `None` autogenerates one. A name already taken by a `Model` attribute (`save`, `filter`, `pk`, ...) or by an attribute of the related model raises `ConfigurationError`. |
| `on_delete` | See below. |
| `to_field` | The attribute on the related model to reference (default: its primary key). A related row whose `to_field` value is `NULL` is referenced by no row: its reverse accessor, `prefetch_related()` and delete cascades never match rows with a `NULL` foreign key, and creating a row through that reverse accessor (`.create()`) raises `QueryError` instead of inserting a detached row. When the related model's primary key is a `OneToOneField(primary_key=True)`, the foreign key references that field's own column (e.g. `user_id`). |
| `db_constraint` | Whether to emit a real DB `FOREIGN KEY` constraint (default `True`). A relation to a model whose app lives on another connection must set `False` - a constraint can't span two databases, and `Hare.init()` raises `ConfigurationError` otherwise. |
| `db_index` | Whether to index the key column(s) (default `True` for a `ForeignKeyField`) - see [Index on the key column](#index-on-the-key-column). |
| `lazy` | `RelationLoadStrategy.JOINED` (implicit `select_related`) or `.SELECT` (implicit `prefetch_related`); `None` by default — nothing is preloaded automatically. Either strategy loads one level: the query a prefetch runs doesn't apply the related model's own `lazy=RelationLoadStrategy.SELECT` defaults (so a self-referential relation stops after one hop, even on a cycle in the data) - name deeper levels explicitly, e.g. `prefetch_related("mentor__mentor")`. `refresh_from_db()` loads a refreshed relation of either strategy again. |

### `on_delete` {: #on-delete }

```python
class OnDelete(StrEnum):
    CASCADE = "CASCADE"
    RESTRICT = "RESTRICT"
    SET_NULL = "SET_NULL"     # requires null=True
    SET_DEFAULT = "SET_DEFAULT"  # requires db_default (or default, with db_constraint=False) — see below
    NO_ACTION = "NO_ACTION"
    PROTECT = "PROTECT"       # enforced in Python; the DDL gets a deferrable NO ACTION backstop
```

`PROTECT` has no SQL keyword of its own: hare-orm checks it before issuing the `DELETE`, and the
database constraint is `ON DELETE NO ACTION DEFERRABLE INITIALLY IMMEDIATE`. A hard `delete()` or
`QuerySet.delete()` whose cascade reaches a PROTECT relation defers these constraints for its own
`DELETE` only (on Postgres `SET CONSTRAINTS <names> DEFERRED`, then `IMMEDIATE` right after it, in
the transaction the delete runs in), so a protector that the same cascade removes together with the
row it protects never blocks the delete, while a protector outside the cascade still fails it. The
rest of the transaction keeps immediate checks: writes leave no pending trigger events, so an
`ALTER TABLE`/`TRUNCATE` later in the same transaction (an atomic migration, for example) works, and
your own deferrable constraints keep their timing. SQLite checks the constraint at the end of the
whole statement and needs no deferral, except for a delete that falls back to a cascade in Python
(several statements), which runs with `PRAGMA defer_foreign_keys` and checks the affected tables
before switching it back off.

Shortcuts `CASCADE`, `RESTRICT`, `SET_NULL`, `SET_DEFAULT`, `NO_ACTION`, `PROTECT` are all importable
directly from `hare.fields`.

#### `SET_DEFAULT` needs a `db_default` {: #set-default-needs-db-default }

With a real foreign key constraint (`db_constraint=True`, the default), the DDL contains
`ON DELETE SET DEFAULT` and **the database itself** resets the column when the referenced row is
deleted. It resets it to the column's SQL `DEFAULT` — and the only thing hare-orm ever emits as a
column `DEFAULT` is `db_default`. A Python-side `default=` is applied by hare-orm before an `INSERT`
and never reaches the DDL, so the database would silently reset the column to `NULL` (or fail the
whole delete on a `NOT NULL` column) instead of to `default=`, while hare-orm's own Python-side
cascade would apply `default=`. To keep both paths consistent, the field is rejected at definition
time (`ConfigurationError`) unless it has a `db_default`:

```python
class Post(Model):
    # The row with id=1 is the "deleted user" placeholder every orphaned post falls back to.
    author = fields.ForeignKeyField("models.User", on_delete=fields.SET_DEFAULT, db_default=1)
```

Setting `default=` next to `db_default=` is fine here and does not emit `RedundantDbDefaultWarning`
— the database really does read `db_default` on delete.

With `db_constraint=False` there is no database-level constraint, so only hare-orm's own
Python-side cascade resets the column. It reads `default=` first and falls back to `db_default`
(rendered as a SQL expression), so either one is enough. The same Python-side cascade is what
runs for a target model with `Meta.soft_delete_field`. For a composite-target foreign key, the same
`db_default` value is copied onto every shadow column and can't be split per component, so a
different value per component needs `db_constraint=False` with a tuple `default=`.

Fields declared inside a migration file are not checked: they describe a schema that was already
applied. To give such a column a `DEFAULT`, add `db_default` to the model and generate a new
migration — it sets the column `DEFAULT` on the existing table.

`PROTECT` raises `hare.exceptions.ProtectedError` (carrying `.protected_objects: list[Model]`) when
you try to delete a row something still references — atomically, no separate existence check needed
before the delete:

```python
class Event(Model):
    tournament = fields.ForeignKeyField("models.Tournament", null=True, on_delete=fields.PROTECT)

try:
    await tournament.delete()
except ProtectedError as exc:
    raise ValidationError(f"{len(exc.protected_objects)} event(s) still reference this tournament")
```

`QuerySet.filter(...).delete()` (bulk delete) enforces the same `PROTECT` check, once against the
whole matched set rather than per row, before deleting anything.

The check also follows `CASCADE` chains: deleting a row raises `ProtectedError` when the cascade would
remove a row that a `PROTECT` relation still guards, however many `CASCADE` hops away it is (for
`Meta.soft_delete_field` models, too). `protected_objects` holds the rows doing the protecting; a
protecting row that the same cascade deletes as well doesn't count. A model with no `PROTECT`
anywhere below it in its `CASCADE` chain pays nothing extra for this.

To find out up front what a delete would cascade to, null out or be blocked by — without deleting —
use [`instance.delete_preview()`](model-methods.md).

### Index on the key column {: #index-on-the-key-column }

A `ForeignKeyField` indexes its key column by default, as in Django: filtering by the relation,
JOINs, `prefetch_related()` and every delete cascade - the database's own `ON DELETE` and
hare-orm's Python-side one - look up rows by that column, and without an index each lookup scans
the whole table.

```python
class Membership(Model):
    user = fields.ForeignKeyField("models.User", related_name="memberships")  # led by the UniqueConstraint
    group = fields.ForeignKeyField("models.Group", related_name="memberships")  # indexed
    invited_by = fields.ForeignKeyField("models.User", related_name="invites", db_index=False)  # no index

    class Meta:
        constraints = [UniqueConstraint(fields=("user", "group"))]
```

- `db_index=False` turns the index off.
- No separate index is created when an index the model already declares starts with the key
  column(s), in any order: a plain `Meta.indexes` entry (`Index(fields=...)`, unique or not), an
  unconditional `UniqueConstraint`, or the composite
  primary key. A partial, expression or non-btree index doesn't count - except an unnamed one over
  exactly the key column(s): it gets the same generated name as the relation's own index, so the
  relation's index is left out. Give one of them a `name=` to get both.
- A relation to a [composite primary key](#targeting-a-composite-primary-key) gets one index over
  all of its key columns.
- `db_constraint=False` keeps the index - hare-orm's own cascade looks rows up by the column.
- `OneToOneField` gets no separate index: its `UNIQUE` constraint already indexes the column.
- The index is named like any field's own index: `idx_<table>_<column>_<hash>`.

## `OneToOneField` {: #onetoonefield }

Same kwargs as `ForeignKeyField`; internally forces `unique=True` on the underlying field. Only a
`OneToOneField` can be the model's primary key (`primary_key=True`) — `ForeignKeyField(...,
primary_key=True)` raises `ConfigurationError`. Likewise `ForeignKeyField(..., unique=True)` raises
`ConfigurationError`: a relation with at most one row per related object is a `OneToOneField`.
Its `UNIQUE` constraint already indexes the column, so `db_index` is off by default.

```python
def OneToOneField(
    to: type[TModel] | str | SwappableModelReference,
    related_name: str | None | Literal[False] = None,
    on_delete: OnDelete = CASCADE,
    db_constraint: bool = True,
    null: bool = False,
    **kwargs: Any,
) -> OneToOneRelation[TModel] | OneToOneNullableRelation[TModel]
```

## `ManyToManyField` {: #manytomanyfield }

```python
def ManyToManyField(
    to: type[TModel] | str | SwappableModelReference,
    through: str | type[Model] | SwappableModelReference | None = None,
    forward_key: str | None = None,
    backward_key: str = "",
    related_name: str = "",
    on_delete: OnDelete = CASCADE,
    db_constraint: bool = True,
    unique: bool = True,
    **kwargs: Any,
) -> ManyToManyRelation[TModel]
```

`db_index` and `lazy` are passed as keyword arguments.

```python
class Post(Model):
    tags = fields.ManyToManyField("models.Tag", related_name="posts")
```

| kwarg | Meaning |
|---|---|
| `through` | Through-table name (auto-generated by default), or a real `Model` class for a through-table with extra columns of its own (below). An auto-generated name already used by another M2M field's through-table or by a model's own table (e.g. two `ManyToManyField`s between the same pair of models) raises `ConfigurationError` - set `through=` explicitly on one of them. |
| `forward_key` / `backward_key` | Through-table column names (auto-generated by default). |
| `unique` | Creates a `UNIQUE` index on `(backward_key, forward_key)` — set `False` to allow duplicate rows through the same pair. |
| `db_index` | Indexes the key columns of the automatic through table that its `UNIQUE` index doesn't start with (default `True`): the `forward_key` column(s), and with `unique=False` the `backward_key` column(s) too. `db_index=False` turns it off. A `Model` as `through` indexes its own `ForeignKeyField`s [the usual way](#index-on-the-key-column). |
| `on_delete` | `CASCADE`/`RESTRICT`/`NO_ACTION`/`PROTECT`/`SET_NULL` are valid here. `SET_DEFAULT` isn't — through-table columns are always `NOT NULL` with no default, so there's nothing for it to fall back to — it raises `ConfigurationError`. With a `Model` as `through`, the field's `on_delete` is propagated onto the through model's own FK fields (when they left theirs at `CASCADE`), and `SET_DEFAULT` is accepted only if both of those FK fields satisfy the [`SET_DEFAULT` rule](#set-default-needs-db-default). |
| `lazy` | Only `.SELECT` or `None` — no `.JOINED`; a JOIN through a through-table would multiply rows. A full `refresh_from_db()` loads it again. |

### A `Model` as `through` {: #a-model-as-through }

Pass a real model instead of a bare table name when the relation itself needs extra columns (an
`added_at` timestamp, a `role` on the membership, ...):

```python
class Membership(Model):
    team = fields.ForeignKeyField("models.Team")
    user = fields.ForeignKeyField("models.User")
    role = fields.CharField(max_length=32, default="member")

    class Meta:
        constraints = [UniqueConstraint(fields=("team", "user"))]


class Team(Model):
    members = fields.ManyToManyField("models.User", through=Membership, related_name="teams")
```

The through model is a normal model — query it directly (`Membership.objects.filter(role="admin")`) the same
way you would any other table, on top of the usual `.add()`/`.remove()`/`.clear()` on the relation
itself. hare-orm manages the DDL and migrations for it like any auto-generated through-table. Both of
the through model's own FK fields must reference their model's primary key - a `to_field=` pointing
at any other column raises `ConfigurationError` at `Hare.init()`.

Set the through model's own extra fields on newly-inserted rows via `add(..., through_defaults=...)`:

```python
await team.members.add(user, through_defaults={"role": "admin"})
```

Applied only to rows this call actually inserts — an already-existing pair is left as-is.
`through_defaults` is meaningless (raises `QueryError`) without a real `through=Model`, for
a field name that doesn't exist on the through model, or for one of the through model's own FK
fields making up the relation itself.

When the through model has [`Meta.tenant_field`](../soft-delete-versions-tenants/multi-tenancy.md), `add()` fills it with the
value of the through model's `Tenancy.scope()` unless `through_defaults` gives it - under a scope of
several values `through_defaults` has to; a `through_defaults` tenant outside
that scope raises `QueryError`.

Operations on the relation:

```python
async def add(
    self, *instances: Model | Any, through_defaults: dict[str, Any] | None = None,
    using: str | DatabaseClient | None = None,
) -> None
async def remove(self, *instances: Model | Any, using: str | DatabaseClient | None = None) -> None
async def clear(self, using: str | DatabaseClient | None = None) -> None
async def set(
    self, *instances: Model | Any, through_defaults: dict[str, Any] | None = None,
    clear: bool = False, using: str | DatabaseClient | None = None,
) -> None
async def create(
    self, using: str | DatabaseClient | None = None,
    through_defaults: dict[str, Any] | None = None, **kwargs: Any,
) -> Model
async def get_or_create(
    self, defaults: dict[str, Any] | None = None, through_defaults: dict[str, Any] | None = None,
    using: str | DatabaseClient | None = None, **kwargs: Any,
) -> tuple[Model, bool]
async def update_or_create(...)  # get_or_create()'s parameters plus create_defaults=
```

```python
tag = await post.tags.create(name="new")   # creates the Tag and adds it, in one transaction
await post.tags.add(tag_a, tag_b)
await post.tags.remove(tag_a)
await post.tags.set(tag_b, tag_c)   # tag_a's row goes, tag_b's stays as-is, tag_c's is added
await post.tags.set([tag_b, 7])     # one iterable - instances or primary key values
await post.tags.set(Tag.objects.filter(name__startswith="py"))
await post.tags.clear()
tag, created = await post.tags.get_or_create(name="python")
```

Like Django, `add()`, `remove()` and `set()` take related instances or their primary key values (a
tuple for a composite key); `add()` fetches the rows of the values through the related model's
default scope and raises `IntegrityError` for a value no row has, `remove()` unlinks by value only
rows that scope shows. `set()` also takes one iterable or awaitable - a list, a set, a queryset, a
`values_list(..., flat=True)` of keys, another relation. `get_or_create()`/`update_or_create()` look
among the members only; a new object is created and added in the same transaction (an existing row
that isn't a member is created again, like Django - a unique key then raises `IntegrityError`).

`set()` makes `instances` the relation's members in one transaction: it removes the members missing
from `instances` (soft-deleting their through rows when the through model has
`Meta.soft_delete_field`) and adds the new ones - with `through_defaults`, as `add()` does - while the
through rows of members that stay are left untouched, extra fields included. `clear=True` clears the
whole relation first and adds every one of `instances` afresh. `set()` with no arguments clears the
relation, same as `clear()`.

`clear()` and `set()` only ever remove links to members the related model's default scope shows:
a link to a soft-deleted row, to another tenant's row (`Meta.tenant_field`) or to a row a custom
`Meta.manager` hides stays in place. `clear(all_tenants=True)` removes the links to every tenant's
rows, with no tenant scope needed (see [Multi-tenancy](../soft-delete-versions-tenants/multi-tenancy.md)). `remove()` names its
rows, so it also drops a link to a soft-deleted or `Meta.manager`-hidden one (another tenant's row is
still rejected).

`add()` checks against the database that the owner isn't soft-deleted and that every added row is
shown by the related model's default scope - a row soft-deleted meanwhile (a stale instance), or hidden
by its `Meta.manager`, raises `IntegrityError` and nothing is linked.

## Reverse access {: #reverse-access }

A `ForeignKeyField`/`OneToOneField` auto-generates a reverse accessor on the related model,
typed `BackwardFKRelation[TModel]`/`BackwardOneToOneRelation[TModel]`; a `ManyToManyField` gives you
`ManyToManyRelation[TModel]` on both sides.

A to-many accessor read off an instance - `author.books`, `book.tags` - is a **`RelatedQuerySet`**
(`ReverseRelation` for a backward foreign key, `ManyToManyRelation` for a many-to-many): the
queryset of the related model's manager, filtered to the rows of that instance. It is a queryset -
there is no separate "related manager" with a copy of some queryset methods:

```python
await author.books                                           # every book of the author
await author.books.filter(rating__gte=4).order_by("-rating").limit(5)
await author.books.count()
await author.books.values_list("title", flat=True)
await author.books.published()        # a method of the related model's own QuerySet subclass
async for book in author.books: ...
```

- Every [QuerySet method](../querying/queryset-methods.md) works, with the related model's default scopes
  (`Meta.soft_delete_field`, `Meta.tenant_field`, a custom `Meta.manager`). A method that changes
  which rows are returned (`.filter()`, `.order_by()`, ...) gives a plain queryset of the manager's
  class; the relation's own writes (`add()`, `remove()`, `set()`, `clear()`, `create()`, ...) stay on
  the relation itself.
- When the related model's manager has a `QuerySet` subclass of its own
  (`objects = Manager(BookQuerySet)`), the relation has that subclass's methods too.
- It runs on the connection the instance was loaded from or saved to, unless the router or
  `.using()` says otherwise.
- The relation also **holds its rows once they are loaded** - by `prefetch_related()`,
  [`prefetch_related_objects()`](#prefetch-related-objects), `lazy=`, or by iterating it with
  `async for`. Then the relation itself is iterated, measured and indexed like a list: `for book in
  author.books`, `book in author.books`, `len(author.books)`, `bool(author.books)`,
  `author.books[0]`. On a relation that isn't loaded these raise `NoValuesFetched`. A write through
  the relation drops what was loaded, so load it again (or iterate it with `async for`) to see the
  change.
- A relation read off an instance that was never saved can't be queried (`QueryError`): there is no
  key the related rows could reference yet.

A backward foreign key writes like Django's related manager:

```python
await author.books.create(title="New")                  # the foreign key points at author
book, created = await author.books.get_or_create(title="Old", defaults={"year": 1999})
await author.books.update_or_create(title="Old", defaults={"year": 2000})
await author.books.add(book_a, book_b)                   # one UPDATE; bulk=False saves each one
await author.books.remove(book_a)                        # sets the foreign key to NULL
await author.books.clear()                               # every book of the author
await author.books.set([book_b, book_c])                 # arguments, one iterable or a queryset
```

`add()` needs saved instances with `bulk=True` (the default) and raises `IntegrityError` for a row
the related model's default scope doesn't show; `bulk=False` saves each instance on its own
(`save()` hooks run, an unsaved one is created). `remove()` and `clear()` exist only for a nullable
foreign key (`QueryError` otherwise); `remove()` of a row that points elsewhere raises
`DoesNotExist`. `set()` removes the rows missing from its members and adds the new ones - for a
non-nullable foreign key it only adds, like Django; `clear=True` clears first. A `kwargs` value
pointing the foreign key at another row than the parent raises `QueryError` in `create()`,
`get_or_create()` and `update_or_create()`.

```python
class Book(Model):
    author: fields.ForeignKeyRelation["Author"] = fields.ForeignKeyField("models.Author", related_name="books")

class Author(Model):
    books: fields.ReverseRelation["Book"]  # not declared with a field — purely a type annotation
```

Type-annotation aliases exported from `hare.fields`:

```python
OneToOneNullableRelation = OneToOneFieldInstance[TModel] | None
OneToOneRelation = OneToOneFieldInstance[TModel]
ForeignKeyNullableRelation = ForeignKeyFieldInstance[TModel] | None
ForeignKeyRelation = ForeignKeyFieldInstance[TModel]
```

## Forward access {: #forward-access }

`book.author` gives the loaded `Author` once the relation is loaded (`select_related()`,
`prefetch_related()`, `prefetch_related_objects()`, `lazy=`, or an assignment), and an awaitable
query otherwise -
`await book.author` works either way for a related row that exists. A relation with no related row
(a `NULL` foreign key, a reverse `OneToOneField` nobody points at, or one a
`Select(extra_condition=...)` didn't match) reads differently depending on whether it was loaded:
never loaded, it gives the falsy `NoneAwaitable` (importable from `hare.models`), and
`await book.author` returns `None`; loaded, it is a plain `None` - test it with
`book.author is None` (or look at `book.author_id`), and don't `await` it.

Filtering or assigning by an instance reads its referenced field(s) (`to_field`, the primary key by
default); an instance loaded with `.only()`/`.defer()` without them raises `QueryError` - as do its
reverse accessors and `prefetch_related_objects()` of them.

## Loading relations {: #loading-relations }

Each way of loading a relation does one thing:

| How | When | What it runs |
|---|---|---|
| [`select_related("author")`](../querying/queryset-methods.md) | With the query, for single-valued relations (FK, O2O, reverse O2O). | A `JOIN` in the same statement. |
| [`prefetch_related("books", Prefetch(...))`](../querying/queryset-methods.md#prefetch) | With the query, for any relation. | One more query per relation for all the rows - a many-to-many relation's joined to its through table, or two with the through table read first (see [`prefetch_related()`](../querying/queryset-methods.md)). |
| `lazy=RelationLoadStrategy.JOINED` / `.SELECT` on the field | Always, for every query of the model - the declared default; `defer_related("author")` switches it off for one query. | The same `JOIN` / extra query. |
| `await book.author`, `await author.books` | On demand, for one instance. | One query for that instance. |
| `prefetch_related_objects(instances, ...)` | Later, for instances you already hold. | One query per relation for all of them. |

### `prefetch_related_objects()` {: #prefetch-related-objects }

```python
from hare import Prefetch, prefetch_related_objects

await prefetch_related_objects(
    instances: Iterable[Model], *lookups: str | Prefetch, using: str | DatabaseClient | None = None
) -> None
```

Loads relations of instances already in hand - exactly what `prefetch_related()` does for the rows
of a queryset, with the same lookups: a relation path (`"posts__comments"`) or a `Prefetch(...)`
with a custom queryset or `to_attr`. One query per relation loads it for all the instances, however
many there are; one instance is a list of one.

```python
users = await User.objects.filter(is_active=True)
...
await prefetch_related_objects(users, "emails", Prefetch("posts", Post.objects.filter(published=True)))
for user in users:
    print(len(user.emails), [post.title for post in user.posts])

await prefetch_related_objects([book], "author", "tags")   # one instance
```

- `instances` are instances of one model; an empty collection does nothing.
- The queries run on the connection the first instance came from, unless `using=` names another.
- A lookup naming no relation of the model raises `QueryError`.

## Targeting a composite primary key {: #targeting-a-composite-primary-key }

A `ForeignKeyField`/`OneToOneField` can target a model whose primary key is a
[`CompositePrimaryKey`](field-types.md#compositeprimarykey):

```python
class ArticleVersion(Model):
    id = fields.UUIDField()
    version = fields.IntField()
    pk = CompositePrimaryKey("id", "version")


class Tag(Model):
    # references the current (id, version) pair as a real, table-level composite FOREIGN KEY
    article_version = fields.ForeignKeyField("models.ArticleVersion", related_name="tags")
```

hare-orm generates one shadow column per component of the target's primary key, and a real
`FOREIGN KEY (col1, col2) REFERENCES table (col1, col2)` — not two independent, unconstrained
columns. JOINs, cascades (`CASCADE`/`SET_NULL`/`SET_DEFAULT`/`PROTECT`), and `prefetch_related()`
all work the same way they do for a single-column target.

You can also pass `to_field=` explicitly, but a composite `to_field` is only accepted when it
**exactly** matches the target's own composite primary key, in its declared order — an arbitrary
composite unique constraint as a target isn't supported (`ConfigurationError` otherwise).

A filter through a `ManyToManyField` whose target has a composite primary key works too: `=`,
`__not`, `__in` and `__not_in` compare the whole primary key as a row (`(col1, col2) = (v1, v2)`)
rather than one column.

## Swappable models {: #swappable-models }

A package can ship a default model a project replaces with its own - a package's user model, say,
that the project swaps for `accounts.User`. The package declares the default model with
`Meta.swappable` and points its relations at the setting with `swappable()` instead of at a fixed
model:

```python
from hare import fields
from hare.models import Model, swappable


class BaseUser(Model):
    id = fields.IntField(primary_key=True)
    email = fields.CharField(max_length=255, unique=True)

    class Meta:
        abstract = True


class User(BaseUser):  # the package's default model
    class Meta:
        swappable = "USER_MODEL"


class Consent(Model):
    id = fields.IntField(primary_key=True)
    user = fields.ForeignKeyField(swappable("USER_MODEL"), related_name="consents", null=True)
```

The project subclasses the base and names its own model in the `swappable` config section (see
[`Hare.init()`](../connections/configuration.md#the-config-dict)):

```python
class User(BaseUser):  # accounts/models.py
    phone = fields.CharField(max_length=20, default="")


await Hare.init(config={..., "swappable": {"USER_MODEL": "accounts.User"}})
```

- `swappable("USER_MODEL")` is accepted by `ForeignKeyField`, `OneToOneField` and `ManyToManyField`
  (by its `through=` too). At `init()` it resolves to the model the setting points at - the
  configured one, else the model declaring `Meta.swappable = "USER_MODEL"`. The field keeps the
  reference itself: `deconstruct()` returns it, so a migration file stores the setting, not the
  model it points at.
- A default model its setting points away from is **swapped**: `Model._meta.swapped` holds the
  label of the model used instead (`None` otherwise). It has no table - `Hare.generate_schemas()`
  and migrations skip it and drift ignores it - and every query or write through it
  (`User.objects.all()`, `User.objects.create(...)`) raises `ConfigurationError`: `"hare_ui.User" has been swapped
  for "accounts.User" by the USER_MODEL setting`. Its relations register no backward accessor, so the
  model swapped in can reuse the same `related_name`s.
- A relation naming a swapped model directly (`"hare_ui.User"`) raises `ConfigurationError` at
  `init()` - declare it with `swappable("USER_MODEL")`.
- The automatic through table of a `ManyToManyField(swappable(...))` is named after the field
  (`consent_witnesses`), not after the target, so it doesn't change with the setting.
- Choose the setting at the start of a project: the package's tables get foreign keys to whichever
  model it names when they are created - see
  [Swappable models in migrations](../migrations/migrations.md#swappable-models) for changing it later.
