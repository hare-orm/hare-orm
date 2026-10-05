# Фабрики

Фабрика создаёт объекты модели для тестов: каждое поле — из объявления, а значение, переданное в
вызов, сильнее объявления. Модель — аргумент типа фабрики:

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


user = await UserFactory.create(name="ann")      # сохранён вместе с командой, группами и записями
admin = await UserFactory.create(admin=True)     # значения признака
draft = UserFactory.build(team=team)             # не сохранён; связанный объект только переданный
users = await UserFactory.create_batch(10)
drafts = UserFactory.build_batch(3)
```

| Объявление | Значение каждого объекта |
|---|---|
| обычное значение | Само значение. |
| `Sequence(function)` | `function(number)` — номер объекта в последовательности его фабрики, с 0 (`reset_sequence()` начинает её заново). |
| `LazyFunction(function)` | `function()`. |
| `LazyAttribute(function)` | `function(values)` — остальные значения объекта как атрибуты, параметры из `Params` среди них. Вычисляется после всех остальных значений, в порядке объявления. |
| `Iterator(values, *, cycle=True)` | Следующее из значений; когда они кончаются — снова с начала. |
| `Faker(provider, *, locale=None, **arguments)` | Значение провайдера библиотеки [faker](https://faker.readthedocs.io/) (`pip install faker`; без неё — `ConfigurationError`). |
| `SubFactory(factory, **values)` | Объект другой фабрики, создаётся вместе с объектом; несохранённый объект его не получает: модель принимает только сохранённый связанный объект, переданный в `build()`. Подходит и для цели `GenericForeignKeyField`. |
| `RelatedFactory(factory, related_name, *, size=1, **values)` | После создания объекта — `size` строк другой фабрики, чьё поле `related_name` указывает на него; число, переданное под именем объявления, заменяет `size`. |
| `ManyToMany(factory=None, *, size=0, **values)` | После создания объекта — связи с `size` объектами другой фабрики; объекты, переданные под его именем, связываются вместо них. |
| `Trait(**values)` в `Params` | Значения, когда параметр передан истинным. |

Фабрику можно назвать путём через точку — для двух фабрик, ссылающихся друг на друга. Фабрика
наследует объявления фабрики, от которой унаследована. Поле `Meta.tenant_field` модели получает
арендатора активного `Tenancy.scope()`, если оно не передано; составной первичный ключ объявляется
поле за полем.

`create_batch()` вставляет объекты одним `bulk_create()`, если ни один из них не создаёт связанный
объект и ничего не делает после создания (`SubFactory`, `RelatedFactory`, `ManyToMany`); иначе
создаёт их по одному. `build_batch()` собирает несохранённые объекты так же, как `build()`.
Несохранённый объект не получает `RelatedFactory`/`ManyToMany`.
