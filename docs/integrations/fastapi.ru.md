# FastAPI

`hare.contrib.frameworks.fastapi` подключает hare к приложению [FastAPI](https://fastapi.tiangolo.com).
Ставится вместе с дополнительным пакетом `fastapi` (FastAPI 0.135 или новее, pydantic).

```python
from fastapi import Depends
from pydantic import BaseModel, ConfigDict

from hare.contrib.frameworks import PageSchema
from hare.contrib.frameworks.fastapi import HareFastAPI, RequestQueryDependency
from hare.contrib.request_query import Page

HARE_CONFIG = {
    "connections": {"default": "postgresql://library:secret@localhost:5432/library"},
    "apps": {"library": {"models": ["library.models"], "default_connection": "default"}},
}

app = HareFastAPI(hare_config=HARE_CONFIG, atomic_requests=True, title="Books")


class BookSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str


@app.get("/books", response_model=PageSchema[BookSchema])
async def list_books(books: BookQuery = RequestQueryDependency.provide(BookQuery)) -> Page[Book]:
    return await books.page()
```

## <a id="harefastapi"></a>`HareFastAPI`

```python
class HareFastAPI(FastAPI):
    def __init__(
        self,
        *,
        hare_config: Mapping[str, Any] | HareConfig | str,
        atomic_requests: bool | Sequence[str] = False,
        lifespan: Lifespan | None = None,
        health_routes: HealthRoutes | None = None,
        **kwargs: Any,
    ) -> None: ...
```

`HareFastAPI` — это приложение `FastAPI`; `hare_config` — то, что принимает `Hare.init(config=...)`,
`kwargs` — то, что принимает `FastAPI()`.

- **Модели привязываются при создании приложения** (`Hare.bind_models()`, без подключения). FastAPI
  читает параметры маршрута при его объявлении, а параметры запроса из параметров —
  [`Meta.filters`](request-queries.ru.md#meta-filters), описания его фильтров — читаются из моделей.
  Маршрутизатору, объявленному в модуле, который импортируется до создания приложения, нужен вызов
  `Hare.bind_models(HARE_CONFIG)` до этого импорта.
- **`HareContext`.** Открывается при запуске приложения, до его собственного `lifespan` — у которого база
  уже есть, — и закрывается при остановке. Контекст — глобальный запасной, поэтому его видит каждый
  запрос. При остановке `Hare.close_connections()` ещё и дожидается фоновых наблюдателей, которые ещё
  работают.
- **Запросы из параметров проверяются при запуске.** `RequestQuery.check_declarations()` выполняется, как
  только модели настроены: ошибочный [запрос из параметров](request-queries.ru.md) не даёт приложению
  запуститься.
- **Ошибки ORM как ответы HTTP** в собственном формате ошибок FastAPI, если приложение не отвечает на
  ошибку само (`exception_handlers=`):

  | Ошибка | Ответ |
  |---|---|
  | `DoesNotExist` | `404 Not Found`, `{"detail": "Not Found"}` |
  | `IntegrityError` (уникальность, внешний ключ, защищённая связь) | `409 Conflict`, `{"detail": "Conflict"}` — без сообщения базы, в котором названы таблицы и ограничения |
  | `TransactionRetryError` (сбой сериализации или взаимная блокировка — запрос можно отправить снова) | `409 Conflict`, `{"detail": "Conflict"}` |
  | `ValidationError` (значение, которое поле не принимает: вне диапазона, слишком длинное, нулевой байт, который отвергает PostgreSQL) | `422 Unprocessable Content`, сообщение поля в `detail` |
  | `InvalidRequestQuery` | `422 Unprocessable Content`, как FastAPI сам отвечает на неверные параметры: `{"detail": [{"type", "loc": ["query", name], "msg", "input"}]}`; у ошибки всего запроса (от проверки модели) `"loc": ["query"]`, а в `input` — параметры запроса |
  | `RequestQueryForbidden` (параметр просит то, что запросу видеть нельзя: удалённые строки без разрешения `may_see_deleted()`) | `403 Forbidden`, причина — в `detail` |
  | `ApiGetterError` (не загрузилось значение другого сервиса, которое требует `Meta.api_getter` запроса) | `502 Bad Gateway` без текста ошибки — он называет сервис и запрошенные значения |

  Смонтированное в него приложение (`app.mount("/v1", sub_app)`) отвечает на свои ошибки само: те же
  ответы ему дают `FastAPI(exception_handlers=HareExceptionHandlers.get_handlers())`.

- **Собственное состояние у каждого запроса.** Каждый запрос начинает с пустого счётчика
  `RepeatedQueryDetector`. Только его собственные записи направляют его чтения в подключение, через
  которое он писал ([чтение своих записей](../connections/multiple-databases.ru.md#read-your-writes)), —
  не записи запросов, которые его задача обслужила раньше, и не записи при старте приложения.
- **Транзакция на каждый запрос** при `atomic_requests` — см. ниже.

## <a id="request-queries-as-dependencies"></a>Запросы из параметров как зависимости

`RequestQueryDependency.provide(QueryClass)` — это `Depends()`, который строит запрос из параметров
HTTP-запроса. Каждый параметр класса становится параметром маршрута, проверяется FastAPI и перечисляется
в схеме OpenAPI с типом, значением по умолчанию, ограничениями, перечислением и описанием:

- параметр строки запроса — повторяемый для списка, `?status__in=draft&status__in=published`;
- параметр пути — для параметра с пометкой `InPath()`:

```python
class ChapterQuery(RequestQuery[Chapter]):
    pk: Annotated[KeyColumns[int, int], InPath()]

    class Meta:
        queryset = Chapter.objects.all()
        pagination = None


@app.get("/chapters/{pk}", response_model=ChapterSchema)
async def get_chapter(chapter: ChapterQuery = RequestQueryDependency.provide(ChapterQuery)) -> Chapter:
    return await chapter.get()
```

`KeyColumns` и `CommaSeparated` приходят в запрос текстом параметра, который запрос разбирает сам. Запрос
получает сам HTTP-запрос: `query.request`, а также адреса `next` и `previous` для страницы. На значение,
которое запрос отклоняет, — сортировку, которую он не разрешает, испорченный курсор, — отвечают так же,
как FastAPI отвечает на неверный параметр.

## <a id="responses"></a>Ответы

FastAPI проверяет то, что маршрут возвращает со своим `response_model`, по его атрибутам и описывает это
в схеме; схемы строки, списка и страницы — см. [Веб-фреймворки](web-frameworks.ru.md#response-schemas).

## <a id="atomic-requests"></a>Транзакция на каждый запрос: `atomic_requests`

`HareFastAPI(..., atomic_requests=True)` выполняет каждый HTTP-запрос в транзакции; маршрут, обработчик
которого помечен `RequestTransaction.skip`, выполняется без неё:

```python
from hare.contrib.frameworks import RequestTransaction


@app.post("/imports")
@RequestTransaction.skip
async def import_books(request: Request) -> None: ...
```

Когда транзакция фиксируется или откатывается и что ещё выполняется без неё — см.
[Веб-фреймворки](web-frameworks.ru.md#atomic-requests).

## <a id="health-routes"></a>Маршруты здоровья: `health_routes`

```python
from hare.contrib.frameworks.health_routes import HealthRoutes
from hare.health import HealthCheck

app = HareFastAPI(hare_config=HARE_CONFIG, health_routes=HealthRoutes(HealthCheck()))
```

Какие маршруты готовности и работоспособности он добавляет — см.
[Веб-фреймворки](web-frameworks.ru.md#health-routes).

## <a id="tests"></a>Тесты

С pytest-плагином hare тестируемое приложение работает на тестовых базах: запущенное в цикле событий теста
(его lifespan, с клиентом httpx и `ASGITransport`), под `hare_app_db` оно берёт контекст теста, и его
записи откатываются вместе с записями теста, — см. [Тесты приложения](../testing/setup.ru.md#application-tests).
`TestClient` из Starlette запускает приложение в своём потоке — с ним берите `hare_app_transactional_db`.
