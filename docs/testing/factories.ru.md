# Фабрики

Фабрика создаёт объекты модели для тестов: каждое поле — из объявления, а значение, переданное в вызов,
сильнее объявления. Модель — аргумент типа фабрики:

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


author = await AuthorFactory.create(name="ann")        # сохранён вместе с издательством, тегами и книгами
featured = await AuthorFactory.create(featured=True)   # значения признака
draft = AuthorFactory.build(publisher=publisher)       # не сохранён; связанный объект только переданный
authors = await AuthorFactory.create_batch(10)
drafts = AuthorFactory.build_batch(3)
```

## <a id="declarations"></a>Объявления

| Объявление | Значение каждого объекта |
|---|---|
| обычное значение | Само значение; список, словарь или множество копируется для каждого объекта, так что изменение у одного объекта не меняет другие. |
| `Sequence(function)` | `function(number)` — номер объекта в последовательности его фабрики, с 0 (`reset_sequence()` начинает её заново). Фабрика-наследник другой фабрики той же модели продолжает нумерацию в этой последовательности — это строки одной таблицы. |
| `LazyFunction(function)` | `function()`. |
| `LazyAttribute(function)` | `function(values)` — остальные значения объекта как атрибуты, параметры из `Params` среди них. Вычисляется после всех остальных значений, в порядке объявления. |
| `Iterator(values, *, cycle=True)` | Следующее из значений; когда они кончаются — снова с начала; с `cycle=False` объект, созданный после последнего значения, даёт `ConfigurationError`. |
| `Faker(provider, *, locale=None, **arguments)` | Значение провайдера библиотеки [faker](https://faker.readthedocs.io/) (`pip install faker`; без неё — `ConfigurationError`). |
| `SubFactory(factory, **values)` | Объект другой фабрики, создаётся вместе с объектом; несохранённый объект его не получает: модель принимает только сохранённый связанный объект, переданный в `build()`. Подходит и для цели `GenericForeignKeyField`. |
| `RelatedFactory(factory, related_name, *, size=1, **values)` | После создания объекта — `size` строк другой фабрики, чьё поле `related_name` указывает на него; число, переданное под именем объявления, заменяет `size`. |
| `ManyToMany(factory=None, *, size=0, **values)` | После создания объекта — связи с `size` объектами другой фабрики; объекты, переданные под его именем, связываются вместо них. |
| `Trait(**values)` в `Params` | Значения, когда параметр передан истинным. |

## <a id="factory-rules"></a>Имена, наследование и арендаторы

Фабрику можно назвать путём через точку — для двух фабрик, ссылающихся друг на друга. Фабрика наследует
объявления фабрики, от которой унаследована. Поле `Meta.tenant_field` модели получает арендатора
активного `Tenancy.scope()`, если оно не передано; составной первичный ключ объявляется поле за полем.

## <a id="batches"></a>Пакеты объектов

`create_batch()` вставляет объекты одним `bulk_create()`, если ни один из них не создаёт связанный объект
и ничего не делает после создания (`SubFactory`, `RelatedFactory`, `ManyToMany`), а база возвращает
сгенерированные первичные ключи в порядке вставки (`RETURNING`, PostgreSQL); иначе создаёт их по одному —
у каждого возвращённого объекта есть первичный ключ. `build_batch()` собирает несохранённые объекты так
же, как `build()`. Несохранённый объект не получает `RelatedFactory`/`ManyToMany`.
