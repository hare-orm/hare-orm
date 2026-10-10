# Litestar

`hare.contrib.frameworks.litestar` подключает Hare к приложению [Litestar](https://litestar.dev).
Ставится вместе с дополнительным пакетом `litestar` (Litestar 2.23 или новее, pydantic).

```python
from litestar import Litestar, get
from litestar.di import NamedDependency

from hare.contrib.frameworks.litestar import HarePlugin, RequestQueryDIPlugin
from hare.contrib.request_query import Page

HARE_CONFIG = {
    "connections": {"default": "postgresql://app:secret@localhost:5432/app"},
    "apps": {"models": {"models": ["app.models"], "default_connection": "default"}},
}


@get("/books", dependencies={"books": RequestQueryDIPlugin.provide(BookQuery)})
async def list_books(books: NamedDependency[BookQuery]) -> Page[Book]:
    return await books.page()


app = Litestar([list_books], plugins=[HarePlugin(HARE_CONFIG, atomic_requests=True)])
```

## <a id="hareplugin"></a>`HarePlugin`

`HarePlugin(config, *, atomic_requests=False, health_routes=None)`:

- **Контекст Hare.** Открывается вместе с приложением и закрывается вместе с ним — первым среди
  обработчиков жизненного цикла, поэтому собственные обработчики приложения (брокер сообщений,
  доставщик исходящих событий) уже получают базу и останавливаются до её закрытия. Контекст —
  глобальный запасной, поэтому его видит каждый запрос. При остановке `Hare.close_connections()` ещё и
  дожидается фоновых наблюдателей, которые ещё работают.
- **Запросы из параметров проверяются при запуске.** `RequestQuery.check_declarations()` выполняется,
  как только модели настроены: ошибочный [запрос из параметров](request-queries.ru.md) не даёт приложению
  запуститься, и все ошибочные классы перечисляются.
- **Запросы из параметров как зависимости.** `HarePlugin` сам является `RequestQueryDIPlugin`: плагины
  приложения идут раньше тех, что добавляет Litestar, поэтому он забирает запрос из параметров раньше,
  чем собственный плагин зависимостей pydantic в Litestar принял бы его за обычную модель pydantic.
- **Модели hare в ответах и в данных запроса** — `HarePlugin` даёт обработчику, который их возвращает
  или принимает, [`HareDTO`](#haredto).
- **Ошибки ORM как ответы HTTP** в формате ошибок Litestar, если приложение не отвечает на ошибку само:

  | Ошибка | Ответ |
  |---|---|
  | `DoesNotExist` | `404 Not Found` |
  | `IntegrityError` (уникальность, внешний ключ, защищённая связь) | `409 Conflict`, без сообщения базы, в котором названы таблицы и ограничения |
  | `InvalidRequestQuery` | `400 Bad Request`, как Litestar сам отвечает на неверные параметры: `{"status_code", "detail", "extra"}`, каждая ошибка в `extra` в виде `{"message", "key", "source"}` (`query` или `path`); у ошибки всего запроса (от проверки модели) нет `key` |
  | `RequestQueryForbidden` (параметр просит то, что запросу видеть нельзя: удалённые строки без разрешения `may_see_deleted()`) | `403 Forbidden`, параметр — в `extra` |

- **Собственное состояние у каждого запроса.** Каждый запрос начинает с пустого
  счётчика `RepeatedQueryDetector`, поэтому тот сообщает о запросах к базе одного запроса к API.
  Только его собственные записи направляют его чтения в подключение, через которое он писал
  ([чтение своих записей](../connections/multiple-databases.ru.md#read-your-writes)), — не записи
  запросов, которые его задача обслужила раньше, и не записи при старте приложения.
- **Транзакция на каждый запрос** при `atomic_requests` — см. ниже.

`config` — то же, что принимает `Hare.init(config=...)`.

## <a id="request-queries-as-dependencies"></a>Запросы из параметров как зависимости

`RequestQueryDIPlugin.provide(QueryClass)` — зависимость, которая строит запрос из параметров
HTTP-запроса. Каждый параметр класса становится параметром обработчика, проверяется Litestar и
перечисляется в схеме OpenAPI с типом, значением по умолчанию, ограничениями (наибольшее значение
`limit` — это `max_limit` деления на страницы) и описанием. Параметр — параметр строки запроса, если
его аннотация не говорит иного:

```python
class ChapterQuery(RequestQuery[Chapter]):
    pk: FromPath[KeyColumns[int, int]]

    class Meta:
        queryset = Chapter.objects.all()
        pagination = None


@get("/chapters/{pk:str}", dependencies={"chapter": RequestQueryDIPlugin.provide(ChapterQuery)})
async def get_chapter(chapter: NamedDependency[ChapterQuery]) -> ChapterSchema: ...
```

`KeyColumns` и `CommaSeparated` приходят в запрос текстом параметра, который запрос разбирает сам.
Запрос получает сам HTTP-запрос: `query.request`, а также адреса `next` и `previous` для страницы.

Litestar строит сигнатуры обработчиков при создании приложения, до того как при запуске откроются
подключения. `HarePlugin` в этот момент привязывает модели своей конфигурации
(`Hare.bind_models()`), поэтому параметры [`Meta.filters`](request-queries.ru.md#meta-filters)
попадают в сигнатуры и в схему; если используется только `RequestQueryDIPlugin`, вызовите
`Hare.bind_models()` до создания приложения.

Без `HarePlugin` добавьте `RequestQueryDIPlugin()` в `plugins`.

## <a id="haredto"></a>Ответы: `HareDTO`

Обработчик возвращает модели hare — строку, список строк, страницу строк (`Page[Book]`), — а
`HarePlugin` даёт ему `HareDTO` модели — собственный способ Litestar оформлять ответ: строки кодируются
без отдельной схемы, а схема OpenAPI их описывает.

```python
@get("/books", dependencies={"books": RequestQueryDIPlugin.provide(BookQuery)})
async def list_books(books: NamedDependency[BookQuery]) -> Page[Book]:
    return await books.page()
```

Поля `HareDTO` — это поля модели:

- каждое значение с типом поля (перечисление — для поля-перечисления), `null` для поля, допускающего
  `NULL`;
- колонка ключа связи (`author_id`);
- каждая связь (`author`, `tags`, обратная связь) как связанные строки — `null`, если запрос не
  загрузил их через `select_related()`/`prefetch_related()` или опцию `include` запроса из параметров,
  поэтому кодирование ответа никогда не выполняет запросов к базе. Загруженные строки идут на один
  уровень вглубь (`DTOConfig.max_nested_depth`, по умолчанию 1): без их собственных связей.

Страница сохраняет свои поля (`count`, `limit`, `next` ...), а её строки лежат в `result`.

Плагин строит по одному DTO на модель и забывает его вместе с кэшами модели: модель, зарегистрированная
во время работы, снятая с регистрации и зарегистрированная снова (`Hare.register_live_models()`),
получает свой DTO, а снятый с регистрации класс не удерживается.

Собственный DTO оформляет ответ так же, как любой DTO Litestar:

```python
from litestar.dto import DTOConfig

from hare.contrib.frameworks.litestar import HareDTO


class BookDTO(HareDTO[Book]):
    config = DTOConfig(exclude={"tags"}, rename_strategy="camel")


@get("/books", return_dto=BookDTO, dependencies={"books": RequestQueryDIPlugin.provide(BookQuery)})
async def list_books(books: NamedDependency[BookQuery]) -> Page[Book]:
    return await books.page()
```

### <a id="related-rows"></a>Связанные строки

Обработчик загружает связи, которые возвращает в ответе; больше ничего не нужно:

```python
@get("/books/{book_id:int}")
async def get_book(book_id: FromPath[int]) -> Book:
    return await Book.objects.all().select_related("author").prefetch_related("tags").get(id=book_id)
# {"id": 1, "title": "...", "author_id": 3, "author": {"id": 3, "name": "Anna"},
#  "tags": [{"id": 1, "name": "fantasy"}], "lines": null}
```

Связь с одной строкой — это связанная строка, связь со многими строками (обратный внешний ключ,
«многие-ко-многим») — их список, а связь, которую запрос не загрузил, — `null`. Связанные строки идут
вглубь на `DTOConfig.max_nested_depth` уровней — по умолчанию 1, то есть связанные строки без их
собственных связей; для связей связанных строк нужен свой DTO и запрос, который их загружает:

```python
class BookDetailDTO(HareDTO[Book]):
    config = DTOConfig(max_nested_depth=2)


@get("/books/{book_id:int}/detail", return_dto=BookDetailDTO)
async def get_book_detail(book_id: FromPath[int]) -> Book:
    return await Book.objects.all().select_related("author__profile").prefetch_related("author__books").get(id=book_id)
```

Запрос из параметров загружает связи, которые называет клиент, через свою опцию
[`include`](request-queries.ru.md) (`?include=author,tags`).

`exclude`, `include` и `rename_fields` у `DTOConfig` называют поле связанных строк путём через связи —
`author.name`, `tags.id`, `author.books.title`:

```python
class BookDTO(HareDTO[Book]):
    config = DTOConfig(
        max_nested_depth=2,
        exclude={"author.books", "tags.id"},
        rename_fields={"author.name": "authorName"},
    )
```

Обработчик, принимающий модель (`data: Book`), тоже получает её `HareDTO`: из данных запроса
создаётся строка. Связи и значения, которые генерирует база (автоинкрементный ключ), только для
чтения — данные задают колонку ключа (`author_id`), а не саму связь.

## <a id="atomic-requests"></a>Транзакция на каждый запрос: `atomic_requests`

`atomic_requests=True` выполняет каждый HTTP-запрос в транзакции на подключении по умолчанию, как
`ATOMIC_REQUESTS` в Django; последовательность имён подключений открывает транзакцию на каждом из них.
Транзакция:

- фиксируется, когда ответ начинается со статусом меньше 500, — до отправки ответа, поэтому на
  неудачную фиксацию всё ещё отвечают ошибкой; тело потокового ответа отправляется после фиксации;
- откатывается при ответе со статусом 500 и выше и при исключении, обработанном или нет;
- как обычно, выполняет после фиксации функции `Transactions.on_commit()`.

Обработчик маршрута с `opt={SKIP_TRANSACTION_OPT_KEY: True}` (`from hare.contrib.frameworks.litestar import
SKIP_TRANSACTION_OPT_KEY`) выполняется без неё. Соединения
WebSocket никогда не выполняются в транзакции. С несколькими подключениями транзакции фиксируются по
очереди; неудачная фиксация одной откатывает ещё не зафиксированные, но не уже зафиксированные. Имя,
которого нет в конфигурации, не даёт приложению запуститься.

## <a id="health-routes"></a>Маршруты здоровья: `health_routes`

```python
from hare.contrib.frameworks.health_routes import HealthRoutes
from hare.health import HealthCheck

app = Litestar(route_handlers, plugins=[HarePlugin(HARE_CONFIG, health_routes=HealthRoutes(HealthCheck()))])
```

Добавляет маршрут готовности (`/health/ready`: подключения пингуются и оцениваются `HealthCheck` —
200, `degraded_status_code` при деградации, 503 при неработоспособности) и маршрут
работоспособности (`/health/live`: 200, пока процесс отвечает, база не трогается). Ни один не
работает в транзакции запроса и не попадает в схему OpenAPI. Пути, статус при деградации и подробности
ответа — аргументы `HealthRoutes`, см. [Метрики пула и здоровье](../observability/pool-health.ru.md#liveness-and-readiness).
