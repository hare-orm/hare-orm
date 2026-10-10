# Запросы из параметров HTTP-запроса

`hare.contrib.request_query` один раз, в классе, описывает строки, которые запрашивает клиент API:
с какого набора строк начинать, по каким параметрам фильтровать, где искать, какие сортировки
разрешены и как делить результат на страницы. Класс — это модель pydantic для параметров запроса;
адаптер фреймворка ([Litestar](litestar.ru.md), [FastAPI](fastapi.ru.md), [Robyn](robyn.ru.md))
создаёт его из запроса и передаёт обработчику, а тот его выполняет:

```python
from typing import Annotated

from hare.contrib.request_query import Filter, OffsetPagination, OrderingConfig, RequestQuery, SearchConfig


class BookQuery(RequestQuery[Book]):
    author: int | None = None
    title__icontains: str | None = None
    genre_ids: Annotated[list[int] | None, Filter("genres", lookup="in")] = None

    class Meta:
        queryset = Book.objects.all().select_related("author")
        search = SearchConfig(fields=("title", "author__name"))
        ordering = OrderingConfig(fields=("title", "published_at"), default=("-published_at",))
        pagination = OffsetPagination(default_limit=50, max_limit=200)


page = await BookQuery(author=3, ordering="title").page()
```

Ставится вместе с дополнительным пакетом `request-query` (pydantic).

Фильтры, их операторы и значения, которые они принимают, берутся из самого ORM — из
[`get_lookup_info()`](../querying/describing-filters.ru.md), — поэтому оператор, который проект зарегистрировал через
`register_lookup()`, работает как встроенный, а объявление, которое не соответствует моделям, даёт
ошибку при запуске приложения, а не на запросе.

## <a id="parameters"></a>Параметры

Параметр без пометки фильтрует по своему имени: `author` — это `.filter(author=...)`,
`title__icontains` — `.filter(title__icontains=...)`. Параметр со значением `None` не фильтрует.
Работает любой ключ, который принимает `.filter()`:

| Параметр | Фильтр |
|---|---|
| <code>author: int &#124; None</code> | связь по ключу связанной строки (`author=3`) |
| <code>author&#95;&#95;in: list&#91;int&#93; &#124; None</code> | связь по списку ключей |
| <code>reservation&#95;&#95;work&#95;time&#95;slot&#95;&#95;project&#95;scheme&#95;&#95;in: list&#91;UUID&#93; &#124; None</code> | связь в конце цепочки |
| <code>reservation&#95;&#95;work&#95;time&#95;slot&#95;&#95;project&#95;scheme&#95;&#95;pk&#95;&#95;in: list&#91;UUID&#93; &#124; None</code> | то же через `pk` |
| <code>profile&#95;&#95;city: str &#124; None</code> | связь «один-к-одному», с любой стороны |
| <code>tags&#95;&#95;name: str &#124; None</code> | связь «многие-ко-многим» — каждая строка возвращается один раз |
| <code>published&#95;at&#95;&#95;year&#95;&#95;gte: int &#124; None</code> | часть даты |
| <code>pk: KeyColumns&#91;int, int&#93; &#124; None</code> | составной первичный ключ, `?pk=2,1` |
| <code>line: KeyColumns&#91;int, int&#93; &#124; None</code> | связь с моделью с составным ключом |
| <code>score&#95;&#95;within: tuple&#91;int, int&#93; &#124; None</code> | оператор, зарегистрированный через `register_lookup()` |

### <a id="meta-filters"></a>Из списка полей: `Meta.filters`

```python
from hare.contrib.request_query import FilterField
from hare.query.enums import Lookup


class BookQuery(RequestQuery[Book]):
    class Meta:
        queryset = Book.objects.all()
        filters = (
            FilterField("status", lookups=(Lookup.EXACT, Lookup.IN)),
            FilterField("author", lookups=(Lookup.EXACT, Lookup.IN)),
            FilterField("published_at", lookups=(Lookup.GTE, Lookup.LTE, Lookup.RANGE)),
            FilterField("author__profile", lookups=(Lookup.ISNULL,)),
            FilterField("author__profile__city", lookups=(Lookup.EXACT, Lookup.IN), parameter="city"),
        )
```

`FilterField(path, lookups=(Lookup.EXACT,), parameter=None, description=None)` — поле, связь, путь
через связи или вычисляемое значение `Meta.queryset` — создаёт по параметру на каждый оператор:
`status`, `status__in`, `author`, `author__in`, `published_at__gte`, `published_at__lte`,
`published_at__range`, `author__profile__isnull`. `Lookup.EXACT` (или `"exact"`) — это сам путь,
любой другой оператор добавляет `__<оператор>`; оператор, зарегистрированный через `register_lookup()`,
называется своим именем. `parameter=` задаёт имя параметров вместо пути, чтобы API не показывал
устройство модели: `city` и `city__in` фильтруют по `author__profile__city`. `description=` заменяет
описание поля в схеме API. `FilterField` проверяется при объявлении: путь и `parameter` должны быть
идентификаторами, операторы — непустым кортежем, каждый по одному разу. Тип каждого параметра — это
значение, которое принимает его фильтр, как его описывает ORM:

| Фильтр принимает | Параметр |
|---|---|
| одно значение | <code>T &#124; None</code> |
| список (`in`, `not_in`) | <code>list&#91;T&#93; &#124; None</code> — параметр повторяется, `?status__in=draft&status__in=published` |
| диапазон (`range`) | <code>tuple&#91;T, T&#93; &#124; None</code> — два значения, «от» и «до» |
| составной ключ | `KeyColumns[...]` вместо `T` |
| поле с перечислением (`IntEnumField`, `CharEnumField`) и сравнение через `exact`, `not`, `in`, `not_in` | перечисление вместо `T` |

`T` — тип значения: тип поля, тип ключа связанной модели для связи, `int` для части даты, `bool` для
`isnull`. Фильтр, тип значения которого становится известен только при выполнении запроса
(вычисляемое значение из готового SQL, любое значение JSON), даёт `ConfigurationError` — объявите его
параметр вручную. Параметр, который создал бы `Meta.filters`, но который класс уже объявляет сам или
добавляет какая-то опция, — тоже `ConfigurationError`: псевдонимы, методы фильтров и особые типы
объявляются вручную рядом с `Meta.filters`.

Параметры добавляются, когда модели привязаны — через `Hare.bind_models()` (подключение
не нужно) или `Hare.init()`: когда адаптер фреймворка читает параметры для сигнатур обработчиков
(`HarePlugin` сам привязывает модели), когда запрос строится первый раз или при
`prepare_parameters()`. Подкласс получает параметры унаследованного `Meta.filters`.

### <a id="descriptions"></a>Описания

Параметр фильтра без собственного описания описывается `description` своего поля, а список или
диапазон — тем, как он записывается («Повторите параметр для каждого значения»); это показывает схема
API. Параметр с `Field(description=...)` сохраняет своё описание.

### <a id="filter"></a>Псевдонимы: `Filter`

`Filter` даёт параметру собственное имя, чтобы API не показывал устройство модели — и не менялся
вместе с ним:

```python
publisher_ids: Annotated[
    list[UUID] | None,
    Filter("author__publisher", lookup="in"),
] = None
```

`Filter(key)` принимает ключ целиком (`Filter("author__name__iexact")`); `lookup` добавляется к нему
через `__`.

### <a id="nofilter"></a>Параметры, которые не фильтруют: `NoFilter`

```python
mode: Annotated[Mode | None, NoFilter()] = None
```

Обработчик читает `query.mode`; запрос его не учитывает.

### <a id="inpath"></a>Параметры пути: `InPath`

```python
pk: Annotated[KeyColumns[int, int], InPath()]
```

Параметр, который читается из пути маршрута, а не из строки запроса, — так его читает и описывает
любой адаптер фреймворка (в приложении Litestar работает и собственный `FromPath` Litestar).

### <a id="filter-methods"></a>Методы фильтров

Метод `filter_<параметр>(self, value)` превращает значение параметра в собственный `Q` — синхронно
или асинхронно; `None` не добавляет условия:

```python
class BookQuery(RequestQuery[Book]):
    rated_at_least: int | None = None

    def filter_rated_at_least(self, value: int) -> Q:
        return Q(author__rating__gte=value)
```

Параметр с методом фильтра не сверяется с ORM — что он делает, решает его метод.

Асинхронные методы фильтров у параметров со значением выполняются одновременно (`asyncio.gather`):
медленный запрос в одном не ждёт другой. Условия идут в порядке объявления параметров. Внутри
транзакции их запросы по-прежнему выполняются на её соединении по очереди.

### <a id="types"></a>Типы

Аннотация параметра должна принимать значение, которое принимает его фильтр, как его описывает
`get_lookup_info()`:

- одно значение (`LookupValueShape.VALUE`) — тип значения, его подкласс, перечисление или `Literal`
  таких значений (`str` для текстового оператора, `int` для связи с целочисленным ключом, `bool` для
  `isnull`, `tuple[UUID, int]` для составного ключа);
- список (`LIST`, `__in`) — `list[...]`, `set[...]`, `frozenset[...]` или `tuple[..., ...]` из него;
- диапазон (`RANGE`, `__range`) — `tuple[T, T]` или `list[T]`.

`bool` не считается `int`. Несоответствие даёт `ConfigurationError` с именем класса, параметром, тем,
что принимает фильтр, и тем, какая у параметра аннотация.

Три типа читают одно текстовое значение:

- `KeyColumns[int, int]` — значения составного ключа через запятую (`?pk=2,1`); значение с запятой
  записывается как `%2C`. Список ключей — `list[KeyColumns[int, int]]`, по параметру на ключ
  (`?pk__in=2,1&pk__in=3,1`).
- `CommaSeparated[int]` — список в одном параметре (`?ids=1,2,3`); повторённые параметры
  объединяются, пустые элементы пропускаются.
- `GenericTarget` — цель [`GenericForeignKeyField`](../models/relations.ru.md#genericforeignkeyfield) в
  виде `<ветка>:<ключ>` (`?target=post:1`, значения составного ключа через запятую:
  `article_version:7,2`), читается как `{"type": "post", "id": 1}`. `Meta.filters` типизирует им
  `FilterField("target")`, а `FilterField("target__type")` — `Literal` из имён веток.

Значение, которое не подходит — неверного типа, вне диапазона, сортировка, которую запрос не
разрешает, — даёт `InvalidRequestQuery` с одной ошибкой на параметр (`loc`, `msg`, `type`); адаптер
фреймворка отвечает на неё так же, как сам фреймворк отвечает на неверные параметры: `400 Bad Request`
в Litestar, `422 Unprocessable Content` в FastAPI и Robyn.

### <a id="bounds"></a>Границы

Границы, которые запрос задаёт одному полю, должны оставлять между собой хотя бы одно значение. Каждая
нижняя граница (`gt`, `gte`) сопоставляется с каждой верхней (`lt`, `lte`) того же пути поля — и
объявленной, и из `Meta.filters`, в том числе под псевдонимом (`published_since` с
`Filter("published_at", lookup="gte")`) и через часть даты (`published_at__year__gte`):
`?published_at__gte=2026-02-01&published_at__lte=2026-01-01` — это `InvalidRequestQuery` на параметре
верхней границы (`type` `range`). Равные границы допустимы, если ни одна из них не строгая (`gt`,
`lt`). Параметр `range`, который начинается позже, чем заканчивается, отклоняется так же. Значения,
которые нельзя сравнить (дата-время с поясом и без), оставляются базе.

### <a id="order-of-the-parameters"></a>Порядок параметров

Сначала идут параметры, которые объявляет сам класс, затем параметры `Meta.filters`, затем параметры
опций (поиск, сортировка, поля, связи, удалённые строки, страницы) — и в полях модели, и в схеме API.

## <a id="options"></a>Опции: `Meta`

| Опция | Что задаёт |
|---|---|
| `queryset` | Строки, с которых начинается запрос: `Book.objects.all().select_related("author")`. Класс без него служит только основой для других. |
| `filters` | Кортеж `FilterField` — поля, по которым запрос может фильтровать, и операторы каждого; см. [`Meta.filters`](#meta-filters). |
| `search` | `SearchConfig`, `None` — без поиска. |
| `ordering` | `OrderingConfig`, `None` — запрос не может выбрать сортировку. |
| `pagination` | `OffsetPagination`, `ScrollPagination` или `CursorPagination`, `None` — без страниц. По умолчанию `OffsetPagination()`. |
| `join_type` | Как объединяются условия параметров — `Connector.AND` (по умолчанию) или `Connector.OR`. |
| `fields` | `FieldsConfig`, `None` — запрос не может выбрать загружаемые поля. |
| `include` | `IncludeConfig`, `None` — запрос не может попросить связи. |
| `deleted` | `DeletedConfig`, `None` — запрос не может видеть мягко удалённые строки. |
| `versions` | `LatestVersions()` — оставлять только последнюю версию каждой записи `VersionedModel`; `None` — все версии. |

Подкласс наследует каждую опцию, которую не задаёт сам, от ближайшего базового класса, который её
задаёт.

`queryset` строится при импорте модуля, до `Hare.init()`; каждый запрос работает со своей копией.
`get_queryset()` можно переопределить для строк, зависящих от запроса, — фильтры всё равно сверяются с
`Meta.queryset`, поэтому фильтру по вычисляемому значению нужно это значение там. Модели с
`Meta.tenant_field` больше ничего не нужно: область из `Tenancy.scope()` применяется при выполнении
запроса, а не при построении `Meta.queryset`.

Каждая опция, добавляющая параметры (`search`, `ordering`, `fields`, `include`, `deleted`,
`pagination`), — это `RequestOption`; `parameter=` (у пагинации — `limit_parameter=`, `offset_parameter=`,
`cursor_parameter=`) переименовывает её параметр запроса. `RequestOption` называет свои параметры (`get_parameter_fields()`), проверяет
значения, которые им даёт запрос (`check_request()`), и применяет их; своя опция наследуется от неё
так же.

### <a id="searchconfig"></a>Поиск: `SearchConfig`

```python
search = SearchConfig(fields=("title", "author__name"), lookup="icontains", parameter="search", join_type=Connector.OR)
```

`?search=tolk` находит строки, где подходит любое поле (`Connector.OR`) или каждое поле
(`Connector.AND`). Оператор каждого поля должен принимать текст. Пустой поиск ничего не фильтрует.

`split_words=True` ищет по словам: каждое слово текста должно найтись, каждое — в полях, объединённых
через `join_type`; `?search=ursula wizard` найдёт книгу Урсулы с названием «Wizard».

### <a id="orderingconfig"></a>Сортировка: `OrderingConfig`

```python
ordering = OrderingConfig(fields=("title", "published_at", "author__name"), default=("-published_at",))
```

Запрос называет одно или несколько полей из `fields` через запятую, `-` — по убыванию:
`?ordering=-published_at,title`. Имя вне `fields` или названное дважды отклоняется. Строки
сортируются по `order_by()` обработчика, иначе по сортировке из запроса, иначе по `default`, иначе по
собственной сортировке набора строк, иначе по `Meta.ordering` модели — и затем по полям первичного
ключа, по которым ещё нет сортировки, чтобы строки с равными значениями сохраняли один порядок от
страницы к странице (все поля составного ключа); модель без первичного ключа
(`Meta.primary_key = None`) таких полей не получает. Сортировка через связь «ко многим» отклоняется —
каждая строка повторялась бы.

### <a id="pagination"></a>Страницы

Каждый способ деления на страницы наследуется от `Pagination` — размер страницы, который может
запросить клиент (`default_limit`, `max_limit`), и его параметр (`limit_parameter`); каждый добавляет
то, как запрос указывает страницу, и строит свой вид страницы (`get_page()`), поэтому свой способ —
тоже подкласс.

`OffsetPagination(default_limit=100, max_limit=1000, limit_parameter="limit", offset_parameter="offset")`
делит на страницы через `?limit=` и `?offset=`. `page()` возвращает `Page`: `result`, `count` (все
подходящие строки), `limit`, `offset`, `next` и `previous` — адрес запроса с заменённым `offset`, где
все остальные параметры сохранены как были, в том числе повторённые; `previous` при смещении меньше
размера страницы ведёт на первую страницу.

`ScrollPagination(default_limit=100, max_limit=1000, limit_parameter="limit", offset_parameter="offset")`
тоже делит через `?limit=` и `?offset=`, но не считает подходящие строки — для ленты или длинного
списка, где показываются только «дальше» и «назад»: не нужен `COUNT` большой таблицы, а есть ли
следующая страница, определяется по одной строке, прочитанной сверх страницы. `page()` возвращает
`ScrollPage`: `result`, `limit`, `offset`, `next`, `previous` — счётчика нет вовсе. Оба способа со
смещением наследуются от `BaseOffsetPagination`.

`CursorPagination(default_limit=100, max_limit=1000, limit_parameter="limit", cursor_parameter="cursor")`
делит по курсору: значения сортировки строки, после которой начинается или до которой заканчивается
страница (`after_cursor()`/`before_cursor()`). Страница стоит одинаково, где бы она ни была, строки,
добавленные между запросами, не сдвигают страницы, счётчика нет. `page()` возвращает `CursorPage`:
`result`, `limit`, `next_cursor`, `previous_cursor`, `next`, `previous`. Курсор помнит сортировку,
для которой создан; курсор другой сортировки или текст, который не является курсором, — это
`InvalidRequestQuery`. Сортировка при делении по курсору не может использовать вычисляемое значение;
поля через прямые связи загружаются вместе со строками (`select_related()`), потому что курсор читает
их из последней строки. У модели без первичного ключа деления по курсору быть не может: строки,
равные по сортировке, пропускались бы.

Размеры страниц проверяются при создании опции: `1 <= default_limit <= max_limit <= 100000`.

### <a id="fields-and-include"></a>Поля и связи: `FieldsConfig`, `IncludeConfig`

```python
fields = FieldsConfig(fields=("title", "status", "published_at"))
include = IncludeConfig(relations=("author", "author__profile", "tags"))
```

`?fields=title,status` загружает только названные поля (`QuerySet.only()`), первичный ключ и поля,
которые читает сортировка; обращение к другому полю такой строки даёт `AttributeError`. Имена должны
быть собственными полями модели — связи загружаются через `include`. `?include=author,tags`
загружает названные связи вместе со строками: связь с одной строкой — через соединение таблиц
(`select_related()`), связь со многими строками — отдельным запросом (`prefetch_related()`). Имя,
которого нет в опции, — это `InvalidRequestQuery`. Обе опции действуют в `fetch()`, `page()` и `get()`.

### <a id="deletedconfig"></a>Удалённые строки: `DeletedConfig`

```python
deleted = DeletedConfig(parameter="deleted")
```

Для модели с `Meta.soft_delete_field`: `?deleted=include` — все строки, `?deleted=only` — только
удалённые, `?deleted=exclude` или без значения — остальные, как без опции. Чтобы запросить удалённые
строки, это должен разрешить `may_see_deleted()` — иначе запрос даёт `RequestQueryForbidden`, и
адаптер фреймворка отвечает `403 Forbidden`:

```python
class ProjectBookQuery(BookQuery):
    async def may_see_deleted(self) -> bool:
        return self.request.user.is_admin
```

`may_see_deleted()` по умолчанию возвращает `True`: объявление опции позволяет видеть удалённые строки
любому запросу. Модель без `Meta.soft_delete_field` даёт `ConfigurationError`.

### <a id="latestversions"></a>Последние версии: `LatestVersions`

```python
class ArticleQuery(RequestQuery[Article]):
    class Meta:
        queryset = Article.objects.filter(status="published")
        versions = LatestVersions()
```

Для `VersionedModel`: только последняя версия каждой записи — строки, для которых нет более новой
версии с тем же `id`; это один связанный подзапрос (`NOT EXISTS`), а не список идентификаторов.
Сравниваются версии из строк `Meta.queryset` (выше — последняя опубликованная версия) с учётом того,
какие удалённые строки просит запрос, и условия доступа: более новая версия, которую запросу видеть
нельзя, не скрывает ту, которую можно. Фильтры запроса применяются к последним версиям: фильтр,
подходящий только под более старую версию, ничего не находит. Модель, которая не является
`VersionedModel`, даёт `ConfigurationError`.

## <a id="running-a-query"></a>Выполнение запроса

| Метод | Возвращает |
|---|---|
| `await query.page()` | `Page`, `ScrollPage` или `CursorPage` — по `Meta.pagination`. |
| `await query.fetch()` | Все подходящие строки, отсортированные, без деления на страницы. |
| `await query.get()` | Единственную подходящую строку; иначе `DoesNotExist` / `MultipleObjectsReturned`. Принимает `does_not_exist_exception` и `multiple_objects_returned_exception`, как [`QuerySet.get()`](../querying/queryset-methods.ru.md), — `get(does_not_exist_exception=None)` возвращает `None`, если подходящей строки нет. |
| `await query.count()` / `exists()` | Сколько строк подходит / подходит ли хотя бы одна. |
| `await query.delete()` | Удаляет подходящие строки и возвращает их число. |
| `await query.update(**values)` | Обновляет подходящие строки и возвращает их число. |
| `await query.get_for_update()` | Единственную подходящую строку, заблокированную до конца транзакции. |
| `await query.count_by(*names, limit=None)` | Сколько подходящих строк имеет каждое значение каждого поля. |
| `await query.get_filtered_queryset()` | Отфильтрованный набор строк без сортировки — для всего остального. |

Условие через связь «ко многим» (обратный внешний ключ, «многие-ко-многим») делает запрос `DISTINCT`,
чтобы каждая строка возвращалась и считалась один раз, — любое условие: параметр, `Q` метода фильтра,
поиск, `where()` обработчика, `get_access_condition()`.

`delete()` и `update()` требуют фильтра из запроса или из `where()` — запрос, который ничего не
фильтрует, даёт `QueryError`, а не меняет все строки, которые видит пользователь.

`get_for_update()` нужен обработчику, который меняет строку: вызывайте его внутри транзакции; он
блокирует строку через `SELECT ... FOR UPDATE` там, где база блокирует строки (SQLite вместо этого
выполняет запись по очереди). Параметры полей и связей на него не действуют.

### <a id="count-by"></a>Количество по значениям: `count_by()`

```python
counts = await query.count_by("status", "author", "tags")
# {"status": {"published": 3, "draft": 2}, "author": {1: 2, 2: 2, 3: 1}, "tags": {1: 3, 2: 2, None: 1}}
```

Для фильтров списка, которые показывают, сколько строк оставит каждый вариант. Каждое поле считается
при всех фильтрах запроса, кроме его собственного, — `?status=draft` всё равно считает все статусы, — с
поиском, `where()` и условием доступа. Каждый ключ связи — свой фильтр (`author`, `author__in`,
`author_id__in`), как и часть даты поля (`published_at__year`). Связь считается по ключу связанной
строки (для составного ключа — кортеж); связь «ко многим» считает строку по разу на каждую связанную
строку; `None` считает строки без значения. Самые частые значения идут первыми; `limit=` оставляет
столько. Фильтр через связь «ко многим» считает каждую строку один раз — для этого нужен первичный
ключ: на модели без него это даёт `QueryError`.

### <a id="the-handlers-part"></a>Что добавляет обработчик

- `query.where(*conditions, **filters)` добавляет собственные условия обработчика через `AND`.
- `query.order_by(*names)` заменяет сортировку — имена или выражения сортировки
  (`Ordering("rating", Order.DESC_NULLS_LAST)`).
- `query.select_related(...)`, `prefetch_related(...)`, `annotate(...)` меняют набор строк.
- `query.request` — запрос, из которого построен объект; `query.cache_key()` называет класс и значения
  его параметров (одинаково для одинакового запроса) — чтобы кэшировать результат вместе со всем,
  от чего он ещё зависит.

### <a id="hooks"></a>Точки расширения

```python
class ProjectBookQuery(BookQuery):
    async def get_access_condition(self) -> Q | None:
        return Q(owner=self.request.user.id)

    async def after_fetch(self, items: list[Book]) -> list[Book]:
        await attach_prices(items)
        return items
```

`get_access_condition()` возвращает строки, которые запросу вообще можно видеть, — это условие
добавляется к каждому запросу, в том числе к `delete()`. `after_fetch()` вызывается для строк `fetch()`,
`page()` и `get()`. `may_see_deleted()` решает, может ли запрос просить удалённые строки (см.
`DeletedConfig`).

## <a id="without-a-web-framework"></a>Без веб-фреймворка

```python
query = BookQuery.from_query_string("status=draft&tag_ids=1&tag_ids=2&ordering=-title")
query = BookQuery.from_query_parameters([("status", "draft"), ("tag_ids", "1")], id=3)
```

Строит запрос из строки параметров адреса или из пар `(имя, значение)` — для тестов, командной строки,
сообщения WebSocket или фреймворка без адаптера. Параметр, принимающий список, получает все значения
повторённого имени, любой другой — последнее; имя, которого у запроса нет, не учитывается; значения из
именованных аргументов (параметры пути) важнее строки параметров; `request=` передаёт сам запрос.
`query.set_request_url(url)` задаёт полный адрес запроса для ссылок страницы, если `url` запроса им не
является.

## <a id="dialects"></a>Диалекты

`RequestQuery` проверяет каждый оператор фильтра и поиска на каждом диалекте, к которому подключается
драйвер, поэтому не может использовать оператор, который выполняет только один из них.
Собственный запрос диалекта — `PostgresqlRequestQuery` из `hare.dialects.postgresql.request_query`,
`SqliteRequestQuery` из `hare.dialects.sqlite.request_query` — проверяет только на своём диалекте и
требует, чтобы подключение его модели было этого диалекта:

```python
from hare.contrib.request_query import SearchConfig
from hare.dialects.postgresql.request_query import PostgresqlRequestQuery


class ArticleQuery(PostgresqlRequestQuery[Article]):
    tags__overlap: list[str] | None = None

    class Meta:
        queryset = Article.objects.all()
        search = SearchConfig(fields=("title", "body"), lookup="search")
```

Запрос для PostgreSQL может использовать полнотекстовый `search`, операторы `trigram_*`, операторы
вложенности для массивов, диапазонов и JSON и операторы, зарегистрированные только для PostgreSQL.
Оператору, которому нужно расширение (`pg_trgm`), нужно, чтобы оно было установлено в базе.

## <a id="checking-declarations"></a>Проверка объявлений

`RequestQuery.check_declarations()` проверяет каждый класс запроса, модель набора строк которого
зарегистрирована в текущем контексте Hare, и даёт одну `ConfigurationError` со списком всех ошибочных:
параметр, не называющий ни одного фильтра модели; фильтр, который не выполняет диалект; вычисляемое
значение, не принимающее значения фильтра; поле поиска, не принимающее текст; сортировка, которой нет
у модели или которая идёт через связь «ко многим»; сортировка курсора по вычисляемому значению; опция
`Meta` неверного типа. Класс модели, которой нет в контексте, принадлежит другому приложению и
пропускается. Каждый адаптер фреймворка ([Litestar](litestar.ru.md), [FastAPI](fastapi.ru.md),
[Robyn](robyn.ru.md)) вызывает это при запуске; без адаптера класс проверяется при первом использовании (`get_declaration()`). Объявление хранит описания ORM для фильтров и
сортировок класса — сортировка, которую класс встречает позже (собственная сортировка набора строк,
`order_by()` обработчика), описывается при первой встрече, — поэтому запрос ни одно из них не читает
заново.

## <a id="live-models"></a>Модели, зарегистрированные во время работы

Модель, зарегистрированная через `Hare.register_live_models()` — таблица типа содержимого, таблица,
открытая в обозревателе базы, — получает запросы так же. `RequestQuery.for_model()` строит для неё
класс и сразу его проверяет:

```python
from hare.contrib.request_query import FilterField, OrderingConfig, RequestQuery
from hare.query.enums import Lookup

ContentQuery = RequestQuery.for_model(
    content_model,
    filters=(FilterField("status", lookups=(Lookup.EXACT, Lookup.IN)),),
    ordering=OrderingConfig(fields=("title",), default=("title",)),
)
page = await ContentQuery.from_query_string("status__in=draft&status__in=published").page()
```

Он принимает каждую опцию `Meta` именованным аргументом — `queryset` (по умолчанию `model.objects`),
`filters`, `search`, `ordering`, `pagination` (по умолчанию `OffsetPagination()`), `fields`, `include`,
`deleted`, `versions`, `join_type`, — а также `name`, имя класса (по умолчанию `<Model>RequestQuery`).
Ошибочное объявление сразу даёт `ConfigurationError`, как его нашёл бы `check_declarations()`. При вызове
у запроса диалекта (`PostgresqlRequestQuery.for_model()`) класс получается этого диалекта. Класс
работает везде, где работает объявленный вручную, — `RequestQueryDIPlugin.provide()`,
`RequestQueryDependency.provide()`, параметр обработчика Robyn, `PageSchema[...]`, `HareDTO` — и
хранится по слабой ссылке: когда на него никто не ссылается, он исчезает из объявлений и из
`get_concrete_subclasses()`.

Класс запроса забывает своё объявление и параметры вместе с каждой моделью, которую они читают: моделью
своего набора строк и каждой моделью, через которую проходят его фильтры, поиск, сортировки, `fields`
или `include`. Это происходит, когда такая модель снимается с регистрации или регистрируется заново
(`Hare.unregister_live_models()`, `register_live_models()`), когда к ней добавляется связь, и после любой
регистрации в `Registries` (оператора фильтра, способа вывода выражения, записи типа, метода `QuerySet`,
драйвера). При следующем использовании всё строится заново по моделям в их нынешнем виде: параметр
`Meta.filters` получает тип, который у его поля сейчас, а исчезнувшее поле — это `ConfigurationError`.
Класс, у которого `Meta.queryset` относится к снятой с регистрации модели, отказывается работать с
`ConfigurationError` — постройте класс заново для модели, зарегистрированной на её месте.
`check_declaration()` перестраивает и проверяет один класс.

## <a id="describe-parameters"></a>Описание параметров: `describe_parameters()`

`describe_parameters()` сообщает, что представляет собой каждый параметр, — то, что нужно панели
фильтров, выбору связанной записи или меню сортировки сверх схемы JSON. Ему нужны привязанные модели,
а не подключения, поэтому он работает после `Hare.bind_models()`:

```python
for parameter in BookQuery.describe_parameters():
    print(parameter.name, parameter.parameter_type, parameter.filter_key, parameter.relation)
```

У каждого `ParameterDescription` есть:

| Атрибут | Что это |
|---|---|
| `name`, `parameter_type` | Параметр и что он делает — `ParameterType.FILTER`, `FILTER_METHOD` (метод `filter_<имя>`), `NO_FILTER`, `SEARCH`, `ORDERING`, `LIMIT`, `OFFSET`, `CURSOR`, `FIELDS`, `INCLUDE`, `DELETED`, `OPTION` (ваша собственная опция). |
| `annotation`, `description`, `default`, `required` | Его тип, описание, значение по умолчанию и обязательность. |
| `takes_many_values` | Принимает все значения повторённого параметра или список `CommaSeparated` — множественный выбор. |
| `in_path` | Читается из пути маршрута (`InPath()`). |
| `choices` | Элементы перечисления, которое он принимает, в виде `ParameterChoice(value, label)` — подпись — это имя элемента. |
| `allowed_values` | Для параметра опции — имена, которые может передать запрос: сортировки, поля `fields`, связи `include`, режимы `deleted`. |
| `filter_key`, `path`, `lookup` | Ключ `.filter()` у фильтра, ключ без оператора и сам оператор. |
| `value_shape`, `value_type` | Принимает ли фильтр одно значение, список или диапазон, и тип значения — кортеж типов для составного ключа. |
| `nullable` | Сравниваемого значения может не быть — поле с `NULL`, связь с `NULL` или связь «ко многим», — поэтому вариант «пусто» имеет смысл. |
| `relation` | Последняя связь, через которую проходит фильтр, в виде `RelationDescription(path, model, key_fields, to_many, compares_key)`: `compares_key`, если значение — ключ связанной строки (`author`, `tags__in`), то есть это выбор связанной записи; `key_fields` содержит все поля составного ключа. |
| `bound`, `paired_parameters` | Какую границу своего пути задаёт фильтр (`BoundSide.LOWER`, `UPPER`, `RANGE`), и параметры, задающие другую (`published_at__gte` в паре с `published_at__lte`). |

Класс, забывший описание вместе со своими моделями, описывает их в нынешнем виде. Своих вариантов
выбора, кроме перечислений, у полей нет: поле `CharEnumField`/`IntEnumField` даёт элементы своего
перечисления.
