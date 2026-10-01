# Your first model

This walks through defining a model, creating its table, and running basic queries — the full loop,
end to end.

## 1. Define the model

```python
# blog/models.py
from hare import fields
from hare.models import Model


class Author(Model):
    id = fields.UUIDField(primary_key=True)
    name = fields.CharField(max_length=120)
    email = fields.CharField(max_length=254, unique=True, null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table_description = "Book authors"
        ordering = ("-created_at",)


class Book(Model):
    id = fields.UUIDField(primary_key=True)
    author = fields.ForeignKeyField("models.Author", related_name="books", on_delete=fields.CASCADE)
    title = fields.CharField(max_length=300)
    published_at = fields.DateField(null=True)
```

A model is a plain class inheriting from `hare.models.Model`. Fields are class attributes; database
behavior beyond individual fields — table name, ordering, constraints — lives in a nested `Meta`
class. See [Field types](../models/field-types.md) and
[Meta options](../models/meta-options.md) for the full reference.

## 2. Initialize hare-orm

```python
# blog/db.py
from hare import Hare, HareConfig

async def init_db() -> None:
    await Hare.init(HareConfig.from_db_url("sqlite://db.sqlite3", {"models": ["blog.models"]}))
    await Hare.generate_schemas()
```

`generate_schemas()` creates tables directly from the model definitions — fine for a quick start or
a test suite. For anything you'll deploy, use real migrations instead — see
[Making and applying migrations](../migrations/migrations.md).

## 3. Create and query rows

```python
from blog.models import Author, Book

async def run() -> None:
    author = await Author.objects.create(name="Ursula K. Le Guin", email="ukl@example.com")
    await Book.objects.create(author=author, title="The Left Hand of Darkness")
    await Book.objects.create(author=author, title="The Dispossessed")

    # every book by an author whose name contains "le guin", with its author in the same query
    async for book in Book.objects.filter(author__name__icontains="le guin").select_related("author"):
        print(book.title, "—", book.author.name)

    # a single row, or None
    maybe = await Author.objects.get_or_none(email="ukl@example.com")

    # count, update, delete — the usual QuerySet toolkit
    total = await Book.objects.filter(author=author).count()
    await Book.objects.filter(title__istartswith="the").update(published_at=None)
    await author.delete()  # cascades to Book via on_delete=CASCADE
```

This is the tip of the iceberg — the full method list (`filter`, `annotate`, `values`,
`bulk_create`, `prefetch_related`, and everything else `QuerySet` offers) is in the
[QuerySet methods](../querying/queryset-methods.md) reference.

## Next

- [Field types](../models/field-types.md) — every field class and its kwargs.
- [Relations](../models/relations.md) — foreign keys, one-to-ones, many-to-manys, and
  composite-primary-key targets.
- [Transactions](../connections/transactions.md) — `atomic()`, `atomic()`, multiple
  connections, routers.
