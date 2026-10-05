# Managers and querysets

Where every query starts — the model's manager, `Model.objects` — how a project adds methods of its own
to querysets and rows every query leaves out, and the reads and writes the manager's queryset offers.

## <a id="model-objects"></a>Where a query starts: `Model.objects`

Every query starts from a manager on the model class: `Book.objects` is a `QuerySet` of the model's
rows, and every method on [QuerySet methods](queryset-methods.md) is a method of it.

```python
await Book.objects.all()
await Book.objects.filter(rating__gte=4).order_by("-published_at").limit(10)
book = await Book.objects.get(pk=1)
book = await Book.objects.create(title="Rocannon's World", author=author)
```

- `Model.objects` is read off the **class**. `book.objects` raises `AttributeError`, like Django —
  an instance has its own methods (`save()`, `delete()`, `restore()`, `refresh_from_db()`, ...).
- It is a queryset already: `Book.objects.filter(...)` and `Book.objects.all().filter(...)` are the
  same query, and it is fully typed without a mypy plugin (`QuerySet[Book]`).
- It carries the model's default scopes — the [`Meta.soft_delete_field`](../soft-delete-versions-tenants/soft-delete.md)
  and [`Meta.tenant_field`](../soft-delete-versions-tenants/multi-tenancy.md) filters — which `include_deleted()`,
  `only_deleted()` and `all_tenants()` switch off for one query.

**Methods of your own** go on a `QuerySet` subclass, handed to the manager — they chain like the
built-in ones:

```python
from hare import Manager, QuerySet


class BookQuerySet(QuerySet["Book"]):
    def published(self) -> "BookQuerySet":
        return self.filter(published_at__isnull=False)

    def by(self, author: Author) -> "BookQuerySet":
        return self.filter(author=author)


class Book(Model):
    ...
    objects = Manager(BookQuerySet)


await Book.objects.published().by(author).order_by("title")
```

**Rows every query leaves out** go into `get_queryset()` of a `Manager` subclass. The default
manager's filter also scopes every JOIN to the model (see [`Meta.manager`](../models/meta-options.md)):

```python
class PublishedManager(Manager):
    def get_queryset(self):
        return super().get_queryset().filter(published_at__isnull=False)


class Book(Model):
    ...
    objects = PublishedManager()   # the default manager: Book.objects, and JOINs to Book
    everything = Manager()         # any number of further managers
```

The default manager is the `objects` the class body declares, else `Meta.manager`, else the
`objects` of a base model, else a plain `Manager()`. `objects` assigned anything but a `Manager` is
a `ConfigurationError`. Each concrete subclass gets its own copy of the managers its bases declare.

**Relations of an instance are querysets too.** `author.books` and `book.tags` are a
`RelatedQuerySet` — the related model's queryset filtered to that instance, with `add()`/
`remove()`/`set()`/`clear()`/`create()` on top — see [Relations](../models/relations.md).

## <a id="queries"></a>Reading and writing rows through `Model.objects`

Everything that reads or writes rows by a condition is a method of the queryset
[`Model.objects`](#model-objects) gives — there is no second copy of the
queryset's methods on the model class:

```python
book = await Book.objects.create(title="...", author=author)
book = await Book.objects.get(pk=1)                       # DoesNotExist / MultipleObjectsReturned
book = await Book.objects.get(title="...", does_not_exist_exception=None)   # None when nothing matches
book, created = await Book.objects.get_or_create(title="...", defaults={"rating": 3})
book, created = await Book.objects.update_or_create(title="...", defaults={"rating": 4})
books = await Book.objects.filter(rating__gte=4).order_by("-rating")
await Book.objects.bulk_create([Book(title="a"), Book(title="b")])
rows = await Book.objects.raw("select * from book where title like %s", ["%test%"])
await Book.objects.using("replica").filter(...)           # on another connection
```

See the [QuerySet methods](queryset-methods.md) for each of them. A few notes on the ones that read one
row:

`get()`'s `does_not_exist_exception` decides what happens when nothing matches and
`multiple_objects_returned_exception` when more than one row does. Left as they are, `DoesNotExist`
and `MultipleObjectsReturned` are raised. An exception class (instantiated with no arguments, e.g.
`Http404`) or an already-constructed instance (`ValueError("no such widget")`) is raised instead.
`None` turns the check off: `get(..., does_not_exist_exception=None)` gives `None` when nothing
matches — typed `Book | None` -, and `get(..., multiple_objects_returned_exception=None)` reads one
of the matching rows with `LIMIT 1`, without counting the rest — which one is not defined (`first()`
takes a defined one).

```python
book = await Book.objects.get(isbn=isbn, does_not_exist_exception=Http404)
book = await Book.objects.get(author=author, multiple_objects_returned_exception=None)   # any one of them
```

`get()` by keyword filters is the cheapest read there is. When the model's default
manager adds nothing to its queries (no `Meta.tenant_field`, `Meta.soft_delete_field` or custom
`get_queryset()`, no relation declared `lazy=RelationLoadStrategy.JOINED`/`.SELECT`), awaiting it runs the statement
plan of that query directly (see [Query plan cache](query-plan-cache.md)): the plan is found by the filter keys
and the types of their values, the values are bound and the row is read. The first query of a
shape has no plan yet: it is built and run the ordinary way, which records the plan. So is a query
whose values the plan can't bind (`None`, an expression, a subquery) or whose filters the queryset
rewrites (`pk=` of a composite primary key). The result is an ordinary queryset either way — every
method used on it (`.values()`, `.only()`, `.prefetch_related()`, `.sql()`, ...) works, with the
same filters, connection and `*_exception` arguments.

`update_or_create()` updates a matched row with `defaults`; a created row gets `create_defaults` when
given, `defaults` otherwise (as Django's `create_defaults`).

When the connection's database supports it (`features.supports_select_for_update`),
`update_or_create()` locks the matched row via `SELECT ... FOR UPDATE` inside the same transaction
it uses for the update — on a database with no such support (e.g. SQLite) it skips the lock rather
than raising.
