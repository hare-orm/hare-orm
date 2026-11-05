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

### <a id="custom-queryset-state"></a>State of a custom queryset

A queryset method creates a branch without calling its constructor again. ORM settings and mutable
prefetch containers are copied independently. Custom slots and dictionary attributes are copied
shallowly by default. For mutable custom state, override `copy_extension_state(target)`, call
`super().copy_extension_state(target)`, then copy that state explicitly, for example
`target.tags = list(self.tags)`.

The hook also runs when a custom queryset is bound to an instance's related queryset. The target
already has its ORM settings. Preparing a SQL statement transfers only the ORM declaration:
custom methods must express their SQL behavior through the queryset's filters, annotations and options.

## <a id="default-filter"></a>Row visibility: `RowScopeProvider`

Declare a predicate through `Manager(scope=...)`. The provider's
`get_condition(model, visibility)` returns `Q`, a boolean `Expression`, or `None`.
It performs no database IO. The default manager's provider also scopes JOINs and
related loading (see [`Meta.manager`](../models/meta-options.md#manager)).

Expression predicates follow [`Q.from_expression`](expressions.md#q): the result must be a SQL
criterion or have explicit `BooleanField` metadata. `ConditionValue(Q(...))`, `Exists(...)`, and
`F("is_active")` for a boolean field are supported. A text or numeric `F(...)` is not a truthiness
test and raises `QueryError` during preparation. Tenant and soft-delete restrictions still apply
to expression predicates on root queries, JOINs and prefetches.

```python
from hare import Manager, RowScopeProvider
from hare.query.expressions import Q


class PublishedRows(RowScopeProvider):
    def get_condition(self, model, visibility):
        return Q(published_at__isnull=False)


class Book(Model):
    ...
    objects = Manager(BookQuerySet, scope=PublishedRows())
    everything = Manager(BookQuerySet)
```

The provider is evaluated during execution preparation, rather than when `Book.objects`
is read. Reusing a queryset in a different context obtains fresh values. One execution
shares its prepared predicates with JOINs and prefetch queries. `visibility` carries the
resolved tenant and `all_tenants`/`include_deleted`/`only_deleted` flags. Tenant and
soft-delete conditions are applied independently; omitting a custom provider does not
disable them.

`get_queryset()` is a factory for the queryset class and its local options. Its filters,
annotations and ordering are not inspected to infer a JOIN policy. Use a provider for
predicates that must apply across relations. A JOIN predicate must use the joined
model's direct fields; a condition requiring another JOIN raises `ConfigurationError`.

The default manager is the `objects` declared on the class, else `Meta.manager`, else
an inherited `objects`, else `Manager()`. Additional managers apply their own providers
to their root queries; relation traversal uses the related model's default provider.
Managers inherited from bases are copied and bound to each concrete model.
`refresh_from_db()`, version allocation and referential-integrity/cascade operations
use their operation-specific policies. Raw SQL does not receive these ORM filters.
This mechanism is not database row-level security.

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
