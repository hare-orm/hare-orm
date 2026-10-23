# Managers and querysets

Where every query starts — the model's manager, `Model.objects` — how a project adds methods of its own
to querysets and rows every query leaves out, and the reads and writes the manager's queryset offers.

## <a id="model-objects"></a>Where a query starts: `Model.objects`

Every query starts from a manager on the model class: `Book.objects` is a `QuerySet` of the model's rows,
and every method on [QuerySet methods](queryset-methods.md) is a method of it.

```python
await Book.objects.all()
await Book.objects.filter(rating__gte=4).order_by("-published_at").limit(10)
book = await Book.objects.get(pk=1)
book = await Book.objects.create(title="Rocannon's World", author=author)
```

- `Model.objects` is read off the **class**. `book.objects` raises `AttributeError` — an instance has its
  own methods (`save()`, `delete()`, `restore()`, `refresh_from_db()`, ...).
- It is a queryset already: `Book.objects.filter(...)` and `Book.objects.all().filter(...)` are the same
  query, and it is fully typed without a mypy plugin (`QuerySet[Book]`).
- It carries the model's default scopes — the
  [`Meta.soft_delete_field`](../soft-delete-versions-tenants/soft-delete.md) and
  [`Meta.tenant_field`](../soft-delete-versions-tenants/multi-tenancy.md) filters — which
  `include_deleted()`, `only_deleted()` and `all_tenants()` switch off for one query.

## <a id="custom-queryset"></a>Methods of a project's queryset

Methods a project adds go on a `QuerySet` subclass, handed to the manager — they chain like the built-in
ones:

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

## <a id="default-filter"></a>Rows every query leaves out: `get_queryset()`

Rows no query should see are left out in `get_queryset()` of a `Manager` subclass. The default manager's
filter also scopes every JOIN to the model (see [`Meta.manager`](../models/meta-options.md#manager)):

```python
class PublishedManager(Manager):
    def get_queryset(self):
        return super().get_queryset().filter(published_at__isnull=False)


class Book(Model):
    ...
    objects = PublishedManager()   # the default manager: Book.objects, and JOINs to Book
    everything = Manager()         # any number of further managers
```

The default manager is the `objects` the class body declares, else `Meta.manager`, else the `objects` of
a base model, else a plain `Manager()`. `objects` assigned anything but a `Manager` is a
`ConfigurationError`. Each concrete subclass gets its own copy of the managers its bases declare.

## <a id="related-querysets"></a>Relations of an instance

`author.books` and `book.tags` are a `RelatedQuerySet` — the related model's queryset filtered to that
instance, with `add()`/`remove()`/`set()`/`clear()`/`create()` on top — see
[Reverse access](../models/relations.md#reverse-access).

## <a id="queries"></a>Reading and writing rows through `Model.objects`

Everything that reads or writes rows by a condition is a method of the queryset
[`Model.objects`](#model-objects) gives — there is no second copy of the queryset's methods on the model
class:

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

Each of them is described in [QuerySet methods](queryset-methods.md): [`get()`](queryset-methods.md#get)
with what it raises, [`get_or_create()`](queryset-methods.md#get-or-create) and
[`update_or_create()`](queryset-methods.md#update-or-create) with their locks. A `get()` by keyword
filters runs on the plan of its call — see [Query plan cache](query-plan-cache.md#shape-key).
