# Менеджеры и QuerySet

С чего начинается любой запрос — менеджер модели, `Model.objects`, — как проект добавляет в QuerySet свои
методы и строки, которые не видит ни один запрос, и какие чтения и записи даёт QuerySet менеджера.

## <a id="model-objects"></a>С чего начинается запрос: `Model.objects`

Любой запрос начинается с менеджера на классе модели: `Book.objects` — это `QuerySet` строк модели, и
каждый метод из раздела [Методы QuerySet](queryset-methods.ru.md) — его метод.

```python
await Book.objects.all()
await Book.objects.filter(rating__gte=4).order_by("-published_at").limit(10)
book = await Book.objects.get(pk=1)
book = await Book.objects.create(title="Rocannon's World", author=author)
```

- `Model.objects` читается с **класса**. `book.objects` даёт `AttributeError` — у экземпляра свои методы
  (`save()`, `delete()`, `restore()`, `refresh_from_db()`, ...).
- Это уже QuerySet: `Book.objects.filter(...)` и `Book.objects.all().filter(...)` — один и тот же запрос,
  и он полностью типизирован без плагина mypy (`QuerySet[Book]`).
- Он несёт области видимости модели по умолчанию — фильтры
  [`Meta.soft_delete_field`](../soft-delete-versions-tenants/soft-delete.ru.md) и
  [`Meta.tenant_field`](../soft-delete-versions-tenants/multi-tenancy.ru.md), — которые
  `include_deleted()`, `only_deleted()` и `all_tenants()` выключают для одного запроса.

## <a id="custom-queryset"></a>Методы QuerySet проекта

Методы, которые добавляет проект, пишутся в подклассе `QuerySet`, который передаётся менеджеру, — они
собираются в цепочку так же, как встроенные:

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

## <a id="default-filter"></a>Строки, которые не видит ни один запрос: `get_queryset()`

Строки, которые не должен видеть ни один запрос, убираются в `get_queryset()` подкласса `Manager`. Фильтр
менеджера по умолчанию действует и на каждый JOIN к модели (см.
[`Meta.manager`](../models/meta-options.ru.md#manager)):

```python
class PublishedManager(Manager):
    def get_queryset(self):
        return super().get_queryset().filter(published_at__isnull=False)


class Book(Model):
    ...
    objects = PublishedManager()   # менеджер по умолчанию: Book.objects и JOIN к Book
    everything = Manager()         # дополнительных менеджеров может быть сколько угодно
```

Менеджер по умолчанию — это `objects`, объявленный в теле класса, иначе `Meta.manager`, иначе `objects`
базовой модели, иначе обычный `Manager()`. Если `objects` присвоено что-то кроме `Manager`, будет
`ConfigurationError`. Каждый конкретный подкласс получает свою копию менеджеров, объявленных в базовых
классах.

## <a id="related-querysets"></a>Связи экземпляра

`author.books` и `book.tags` — это `RelatedQuerySet`: QuerySet связанной модели, отфильтрованный по этому
экземпляру, с методами `add()`/`remove()`/`set()`/`clear()`/`create()` — см.
[Обратные ссылки](../models/relations.ru.md#reverse-access).

## <a id="queries"></a>Чтение и запись строк через `Model.objects`

Всё, что читает или пишет строки по условию, — метод QuerySet, который даёт
[`Model.objects`](#model-objects): второй копии методов QuerySet на классе модели нет.

```python
book = await Book.objects.create(title="...", author=author)
book = await Book.objects.get(pk=1)                       # DoesNotExist / MultipleObjectsReturned
book = await Book.objects.get(title="...", does_not_exist_exception=None)   # None, если ничего не найдено
book, created = await Book.objects.get_or_create(title="...", defaults={"rating": 3})
book, created = await Book.objects.update_or_create(title="...", defaults={"rating": 4})
books = await Book.objects.filter(rating__gte=4).order_by("-rating")
await Book.objects.bulk_create([Book(title="a"), Book(title="b")])
rows = await Book.objects.raw("select * from book where title like %s", ["%test%"])
await Book.objects.using("replica").filter(...)           # на другом подключении
```

Каждый из них описан в разделе [Методы QuerySet](queryset-methods.ru.md):
[`get()`](queryset-methods.ru.md#get) с тем, что он бросает,
[`get_or_create()`](queryset-methods.ru.md#get-or-create) и
[`update_or_create()`](queryset-methods.ru.md#update-or-create) с их блокировками. `get()` по именованным
фильтрам выполняется по плану своего вызова — см.
[Кэш планов запросов](query-plan-cache.ru.md#shape-key).
