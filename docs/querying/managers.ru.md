# Менеджеры и QuerySet

## Откуда начинается запрос: `Model.objects` {: #model-objects }

Любой запрос начинается с менеджера на классе модели: `Book.objects` — это `QuerySet` строк модели,
и каждый метод из раздела [Методы QuerySet](queryset-methods.ru.md) — его метод.

```python
await Book.objects.all()
await Book.objects.filter(rating__gte=4).order_by("-published_at").limit(10)
book = await Book.objects.get(pk=1)
book = await Book.objects.create(title="Rocannon's World", author=author)
```

- `Model.objects` читается с **класса**. `book.objects` даёт `AttributeError`, как в Django, — у
  экземпляра свои методы (`save()`, `delete()`, `restore()`, `refresh_from_db()`, ...).
- Это уже QuerySet: `Book.objects.filter(...)` и `Book.objects.all().filter(...)` — один и тот же
  запрос, и он полностью типизирован без плагина mypy (`QuerySet[Book]`).
- В нём действуют области видимости модели по умолчанию — фильтры
  [`Meta.soft_delete_field`](../soft-delete-versions-tenants/soft-delete.ru.md) и
  [`Meta.tenant_field`](../soft-delete-versions-tenants/multi-tenancy.ru.md), — которые `include_deleted()`, `only_deleted()` и
  `all_tenants()` выключают для одного запроса.

**Свои методы** пишутся в подклассе `QuerySet`, который передаётся менеджеру, — они собираются в
цепочку так же, как встроенные:

```python
from hare.query.manager import Manager
from hare.query.queryset import QuerySet


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

**Строки, которые не должен видеть ни один запрос,** убираются в `get_queryset()` подкласса
`Manager`. Фильтр менеджера по умолчанию действует и на каждый JOIN к модели (см.
[`Meta.manager`](../models/meta-options.ru.md)):

```python
class PublishedManager(Manager):
    def get_queryset(self):
        return super().get_queryset().filter(published_at__isnull=False)


class Book(Model):
    ...
    objects = PublishedManager()   # менеджер по умолчанию: Book.objects и JOIN к Book
    everything = Manager()         # дополнительных менеджеров может быть сколько угодно
```

Менеджер по умолчанию — это `objects`, объявленный в теле класса, иначе `Meta.manager`, иначе
`objects` базовой модели, иначе обычный `Manager()`. Если `objects` присвоено что-то кроме `Manager`,
будет `ConfigurationError`. Каждый конкретный подкласс получает свою копию менеджеров, объявленных
в базовых классах.

**Связи экземпляра — тоже QuerySet.** `author.books` и `book.tags` — это `RelatedQuerySet`:
QuerySet связанной модели, отфильтрованный по этому экземпляру, с методами `add()`/`remove()`/
`set()`/`clear()`/`create()` — см. [Связи](../models/relations.ru.md).

## Чтение и запись строк через `Model.objects` {: #queries }

Всё, что читает или пишет строки по условию, — метод QuerySet, который даёт
[`Model.objects`](#model-objects): второй копии методов QuerySet на классе
модели нет.

```python
book = await Book.objects.create(title="...", author=author)
book = await Book.objects.get(pk=1)                       # DoesNotExist / MultipleObjectsReturned
book = await Book.objects.get_or_none(title="...")
book, created = await Book.objects.get_or_create(title="...", defaults={"rating": 3})
book, created = await Book.objects.update_or_create(title="...", defaults={"rating": 4})
books = await Book.objects.filter(rating__gte=4).order_by("-rating")
await Book.objects.bulk_create([Book(title="a"), Book(title="b")])
rows = await Book.objects.raw("select * from book where title like %s", ["%test%"])
await Book.objects.using("replica").filter(...)           # на другом подключении
```

Каждый из них описан в разделе [Методы QuerySet](queryset-methods.ru.md). Несколько замечаний о тех,
что читают одну строку.

`exception` у `get()` заменяет ошибку, которая бросается, если ничего не найдено: класс исключения
(создаётся без аргументов, например `Http404`) или уже созданный объект
(`ValueError("нет такого виджета")`) бросается вместо `DoesNotExist`. На
`MultipleObjectsReturned` не влияет.

`get()`/`get_or_none()` по именованным фильтрам — самое дешёвое чтение. Если менеджер модели по
умолчанию ничего не добавляет к её запросам (нет `Meta.tenant_field`, `Meta.soft_delete_field` или
своего `get_queryset()`, нет связей с `lazy="joined"`/`"select"`), при `await` сразу выполняется
план этого запроса (см. [Кэш планов запросов](query-plan-cache.ru.md)): план находится по ключам фильтров и типам
их значений, значения привязываются, строка читается. У первого запроса такого вида плана ещё нет:
он собирается и выполняется обычным путём, который и записывает план. Так же выполняется запрос,
значения которого план привязать не может (`None`, выражение, подзапрос) или фильтры которого
QuerySet переписывает (`pk=` составного первичного ключа). В обоих случаях результат — обычный
QuerySet: любой его метод (`.values()`, `.only()`, `.prefetch_related()`, `.sql()`, ...) работает — с
теми же фильтрами, подключением и `exception`.

`update_or_create()` обновляет найденную строку значениями `defaults`; созданная строка получает
`create_defaults`, если они переданы, иначе `defaults` (как `create_defaults` в Django).

Если база подключения это поддерживает (`features.supports_select_for_update`),
`update_or_create()` блокирует найденную строку через `SELECT ... FOR UPDATE` в той же транзакции, в
которой обновляет её; в базе без такой возможности (например, SQLite) блокировка пропускается, а не
вызывает ошибку.
