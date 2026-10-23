# Coming from Django

hare's models, querysets and migrations have the shape Django's have, so most of what a Django developer
knows carries over. This page lists what is named differently, what behaves the same, and where hare
gives another result. The rest of the documentation describes hare on its own.

## <a id="names"></a>What is named differently

| Django | hare |
|---|---|
| `from django.db import models`, `models.Model` | `from hare import fields`, `from hare.models import Model` |
| `models.ForeignKey`, `models.OneToOneField`, `models.ManyToManyField` | `fields.ForeignKeyField`, `fields.OneToOneField`, `fields.ManyToManyField` — see [Relations](../models/relations.md) |
| `GenericForeignKey` of `contenttypes` | `fields.GenericForeignKeyField` |
| `Meta.db_table`, `Meta.app_label`, `Meta.db_table_comment` | `Meta.table`, `Meta.app`, `Meta.table_description` — see [Meta options](../models/meta-options.md) |
| `Meta.unique_together` | `UniqueConstraint(fields=(...))` in `Meta.constraints` |
| `settings.DATABASES`, `DATABASE_ROUTERS` | the `connections` and `routers` of [`Hare.init()`](../connections/configuration.md#hare-init) |
| `transaction.atomic()`, `transaction.on_commit()` | `Transactions.atomic()`, `Transactions.on_commit()` — see [Transactions](../connections/transactions.md) |
| `ATOMIC_REQUESTS` | `atomic_requests` of the web framework integration — see [Web frameworks](../integrations/web-frameworks.md#atomic-requests) |
| `manage.py makemigrations`, `migrate`, `sqlmigrate`, `squashmigrations`, `inspectdb` | `hare makemigrations`, `hare migrate`, `hare sqlmigrate`, `hare squashmigrations`, `hare inspectdb` — see [The `hare` command](../migrations/cli.md) |
| `django.utils.timezone.override()` | `Timezone.override()` (`from hare.time import Timezone`) |

Every query and every write is a coroutine: `await Book.objects.filter(...)`, `async for book in ...`,
`await book.save()`. `on_delete` defaults to `CASCADE`.

## <a id="same-behavior"></a>What behaves the same

- A router chain: the first router returning an alias wins, and a read that decides a write
  (`get_or_create()`, `update_or_create()`, `select_for_update()`) runs on the write connection.
- `on_commit()` called after the transaction ended runs the callback at once.
- A field path may end on a relation or on `pk`; `relation=None` on a to-many relation is
  `relation__isnull=True`; a lookup on a relation's own name compares its key.
- The implicit `GROUP BY` of `values().annotate()`, and an aggregate annotated before `values()` being
  computed per model row.
- `update_fields=["author"]` writes `author_id`; a `ForeignKeyField` indexes its key column; `add()`,
  `remove()` and `set()` of a many-to-many relation take instances or key values.
- `Index(fields=("-created_at",))` is a descending key; a constraint condition is a `Q`.
- A sliced queryset can't be filtered, reordered or made `distinct()`; it can be aggregated.
- `distinct()` ordered by a column it doesn't select selects that column too, so a selected value can
  come several times — see [`values()` and `values_list()`](../querying/values.md#distinct-ordered).

## <a id="different-results"></a>Where hare gives another result

- **`exclude()` across a to-many relation.** The conditions of one `exclude()` call refer to the same
  related row, as in `filter()`: `exclude(books__name="a", books__rating=5)` keeps an author with one
  book named "a" and another rated 5. Django drops that author too; in hare that takes two calls —
  `.exclude(books__name="a").exclude(books__rating=5)`. See
  [`exclude()`](../querying/queryset-methods.md#exclude).
- **`filter()` and `annotate()` in any order.** `annotate(n=Count("books")).filter(books__price__gt=6)`
  reuses the aggregate's `JOIN` and counts only the matching books — the same as
  `filter(...).annotate(...)`. In Django the order changes the count. See [Aggregates and to-many
  filters](../querying/aggregation.md#aggregates-to-many).
- **Aggregates over two to-many relations.** A sum or count that a second to-many `JOIN` would inflate
  raises `QueryError` instead of returning the inflated number Django returns.
- **`get()` on a `union()`.** It takes conditions and applies them to every branch; Django refuses them.
