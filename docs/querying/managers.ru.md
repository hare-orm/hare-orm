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

### <a id="custom-queryset-state"></a>Состояние пользовательского QuerySet

Метод QuerySet создаёт новую ветку без повторного вызова конструктора. Настройки ORM и изменяемые
контейнеры prefetch копируются независимо. Пользовательские slots и атрибуты словаря по умолчанию
копируются поверхностно. Для изменяемого пользовательского состояния переопределите
`copy_extension_state(target)`, вызовите `super().copy_extension_state(target)`, затем явно
скопируйте нужные значения, например `target.tags = list(self.tags)`.

Этот метод вызывается и при привязке пользовательского QuerySet к связи экземпляра. Настройки ORM
у получателя уже заполнены. Подготовка SQL-запроса переносит только декларацию ORM: пользовательские
методы выражают своё влияние на SQL через фильтры, аннотации и параметры QuerySet.

## <a id="default-filter"></a>Видимость строк: `RowScopeProvider`

Объявляйте условие через `Manager(scope=...)`. Метод провайдера
`get_condition(model, visibility)` возвращает `Q`, логическое `Expression` или `None`
и не выполняет запросов к БД. Провайдер менеджера по умолчанию также ограничивает JOIN
и загрузку связей (см. [`Meta.manager`](../models/meta-options.ru.md#manager)).

Условия-выражения следуют контракту [`Q.from_expression`](expressions.ru.md#q): результат должен
быть SQL-условием или иметь явно заданный тип `BooleanField`. Поддерживаются
`ConditionValue(Q(...))`, `Exists(...)` и `F("is_active")` для логического поля. Строковый или числовой
`F(...)` не проверяется на истинность и вызывает `QueryError` при подготовке запроса. Ограничения
tenant и soft-delete сохраняются для выражений в корневых запросах, JOIN и prefetch.

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

Провайдер вычисляется при подготовке выполнения, а не при чтении `Book.objects`.
Повторное выполнение queryset в другом контексте получает свежие значения.
В пределах одного выполнения подготовленные условия общие для JOIN и prefetch.
В `visibility` передаются выбранный tenant и флаги
`all_tenants`/`include_deleted`/`only_deleted`. Условия tenant и soft-delete применяются
независимо: отсутствие пользовательского провайдера их не отключает.

`get_queryset()` создаёт queryset и задаёт его локальные настройки. Из его фильтров,
аннотаций и сортировки ORM не извлекает политику JOIN. Условия, которые должны действовать
при переходе по связям, задавайте провайдером. Условие JOIN должно использовать прямые поля
присоединяемой модели; условие, требующее ещё одного JOIN, даёт `ConfigurationError`.

Менеджер по умолчанию — объявленный в классе `objects`, иначе `Meta.manager`, иначе
унаследованный `objects`, иначе `Manager()`. Дополнительные менеджеры применяют свои
провайдеры к корневым запросам; переход по связям использует провайдер менеджера связанной
модели по умолчанию. Унаследованные менеджеры копируются и привязываются к конкретной модели.
`refresh_from_db()`, выделение версии, каскады и проверки ссылочной целостности используют
политику соответствующей операции. Raw SQL не получает эти фильтры ORM.
Этот механизм не является row-level security базы данных.

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
