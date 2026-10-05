# Factories

A factory makes objects of a model for tests — each field from a declaration, a value given to the
call winning over it. The model is the factory's type argument:

```python
from hare.contrib.factories import LazyAttribute, ManyToMany, ModelFactory, RelatedFactory, Sequence, SubFactory, Trait


class TeamFactory(ModelFactory[Team]):
    name = Sequence(lambda number: f"team{number}")


class UserFactory(ModelFactory[User]):
    name = Sequence(lambda number: f"user{number}")
    email = LazyAttribute(lambda user: f"{user.name}@{user.domain}")
    team = SubFactory(TeamFactory)
    groups = ManyToMany(GroupFactory, size=2)
    posts = RelatedFactory("myapp.factories.PostFactory", "author", size=3)

    class Params:
        domain = "example.com"
        admin = Trait(is_staff=True)


user = await UserFactory.create(name="ann")      # saved, with its team, groups and posts
admin = await UserFactory.create(admin=True)     # the trait's values
draft = UserFactory.build(team=team)             # not saved; a related object only given
users = await UserFactory.create_batch(10)
drafts = UserFactory.build_batch(3)
```

| Declaration | The value of each object |
|---|---|
| a plain value | The value itself. |
| `Sequence(function)` | `function(number)` — the object's number in its factory's sequence, from 0 (`reset_sequence()` starts it again). |
| `LazyFunction(function)` | `function()`. |
| `LazyAttribute(function)` | `function(values)` — the object's other values as attributes, the parameters of `Params` among them. Made after every other value, in the order declared. |
| `Iterator(values, *, cycle=True)` | The next of the values, from the start again once they end. |
| `Faker(provider, *, locale=None, **arguments)` | A value of the [faker](https://faker.readthedocs.io/) library's provider (`pip install faker`; without it `ConfigurationError`). |
| `SubFactory(factory, **values)` | An object of another factory, created with the object — a built object gets none: a model takes only a saved related object, given to `build()`. The target of a `GenericForeignKeyField` too. |
| `RelatedFactory(factory, related_name, *, size=1, **values)` | After the object is created, `size` rows of another factory whose `related_name` points at it; a number given under the declaration's name replaces `size`. |
| `ManyToMany(factory=None, *, size=0, **values)` | After the object is created, links to `size` objects of another factory; objects given under its name are linked instead. |
| `Trait(**values)` in `Params` | The values, when the parameter is given true. |

A factory may be named by its dotted path, for two factories naming each other. A factory inherits
the declarations of the factory it subclasses. A model's `Meta.tenant_field` gets the tenant of the
active `Tenancy.scope()` when not given; a composite primary key is declared field by field.

`create_batch()` inserts the objects with one `bulk_create()` when none of them makes a related
object or acts once created (`SubFactory`, `RelatedFactory`, `ManyToMany`), else creates them one by
one. `build_batch()` builds unsaved objects the same way as `build()`. A built object gets no
`RelatedFactory`/`ManyToMany` — it isn't saved.
