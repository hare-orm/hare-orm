# Factories

A factory makes objects of a model for tests — each field from a declaration, a value given to the call
winning over it. The model is the factory's type argument:

```python
from hare.contrib.factories import LazyAttribute, ManyToMany, ModelFactory, RelatedFactory, Sequence, SubFactory, Trait


class PublisherFactory(ModelFactory[Publisher]):
    name = Sequence(lambda number: f"publisher{number}")


class AuthorFactory(ModelFactory[Author]):
    name = Sequence(lambda number: f"author{number}")
    email = LazyAttribute(lambda author: f"{author.name}@{author.domain}")
    publisher = SubFactory(PublisherFactory)
    tags = ManyToMany(TagFactory, size=2)
    books = RelatedFactory("library.factories.BookFactory", "author", size=3)

    class Params:
        domain = "example.com"
        featured = Trait(is_featured=True)


author = await AuthorFactory.create(name="ann")        # saved, with its publisher, tags and books
featured = await AuthorFactory.create(featured=True)   # the trait's values
draft = AuthorFactory.build(publisher=publisher)       # not saved; a related object only given
authors = await AuthorFactory.create_batch(10)
drafts = AuthorFactory.build_batch(3)
```

## <a id="declarations"></a>Declarations

| Declaration | The value of each object |
|---|---|
| a plain value | The value itself — a list, a dict or a set copied for each object, so a change to one object's leaves the others'. |
| `Sequence(function)` | `function(number)` — the object's number in its factory's sequence, from 0 (`reset_sequence()` starts it again). A factory subclassing another one of the same model numbers its objects on in that sequence — they are rows of one table. |
| `LazyFunction(function)` | `function()`. |
| `LazyAttribute(function)` | `function(values)` — the object's other values as attributes, the parameters of `Params` among them. Made after every other value, in the order declared. |
| `Iterator(values, *, cycle=True)` | The next of the values, from the start again once they end; with `cycle=False` an object made after the last value raises `ConfigurationError`. |
| `Faker(provider, *, locale=None, **arguments)` | A value of the [faker](https://faker.readthedocs.io/) library's provider (`pip install faker`; without it `ConfigurationError`). |
| `SubFactory(factory, **values)` | An object of another factory, created with the object — a built object gets none: a model takes only a saved related object, given to `build()`. The target of a `GenericForeignKeyField` too. |
| `RelatedFactory(factory, related_name, *, size=1, **values)` | After the object is created, `size` rows of another factory whose `related_name` points at it; a number given under the declaration's name replaces `size`. |
| `ManyToMany(factory=None, *, size=0, **values)` | After the object is created, links to `size` objects of another factory; objects given under its name are linked instead. |
| `Trait(**values)` in `Params` | The values, when the parameter is given true. |

## <a id="factory-rules"></a>Naming, inheritance and tenants

A factory may be named by its dotted path, for two factories naming each other. A factory inherits the
declarations of the factory it subclasses. A model's `Meta.tenant_field` gets the tenant of the active
`Tenancy.scope()` when not given; a composite primary key is declared field by field.

## <a id="batches"></a>Batches

`create_batch()` inserts the objects with one `bulk_create()` when none of them makes a related object or
acts once created (`SubFactory`, `RelatedFactory`, `ManyToMany`) and the database gives back the primary
keys it generates in insert order (`RETURNING`, PostgreSQL), else creates them one by one — every object
it returns has its primary key. `build_batch()` builds unsaved objects the same way as `build()`. A built
object gets no `RelatedFactory`/`ManyToMany` — it isn't saved.
