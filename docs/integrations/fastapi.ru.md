# FastAPI

`hare.contrib.frameworks.fastapi` подключает Hare к приложению [FastAPI](https://fastapi.tiangolo.com).
Ставится вместе с дополнительным пакетом `fastapi` (FastAPI 0.135 или новее, pydantic).

```python
from fastapi import Depends
from pydantic import BaseModel, ConfigDict

from hare.contrib.frameworks import PageSchema
from hare.contrib.frameworks.fastapi import HareFastAPI, RequestQueryDependency
from hare.contrib.request_query import Page

HARE_CONFIG = {
    "connections": {"default": "postgresql://app:secret@localhost:5432/app"},
    "apps": {"models": {"models": ["app.models"], "default_connection": "default"}},
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

`HareFastAPI(*, hare_config, atomic_requests=False, lifespan=None, health_routes=None, **kwargs)` — это приложение
`FastAPI`; `hare_config` — то, что принимает `Hare.init(config=...)`, `kwargs` — то, что принимает `FastAPI()`.

- **Модели привязываются при создании приложения** (`Hare.bind_models()`, без
  подключения). FastAPI читает параметры маршрута при его объявлении, а параметры запроса из
  параметров — [`Meta.filters`](request-queries.ru.md#meta-filters), описания его фильтров — читаются из
  моделей. Маршрутизатору, объявленному в модуле, который импортируется до создания приложения, нужен
  вызов `Hare.bind_models(HARE_CONFIG)` до этого импорта.
- **Контекст Hare.** Открывается при запуске приложения, до его собственного `lifespan` — у которого
  база уже есть, — и закрывается при остановке. Контекст — глобальный запасной, поэтому его видит
  каждый запрос. При остановке `Hare.close_connections()` ещё и дожидается фоновых наблюдателей,
  которые ещё работают.
- **Запросы из параметров проверяются при запуске.** `RequestQuery.check_declarations()` выполняется,
  как только модели настроены: ошибочный [запрос из параметров](request-queries.ru.md) не даёт
  приложению запуститься.
- **Ошибки ORM как ответы HTTP** в собственном формате ошибок FastAPI, если приложение не отвечает на
  ошибку само (`exception_handlers=`):

  | Ошибка | Ответ |
  |---|---|
  | `DoesNotExist` | `404 Not Found`, `{"detail": "Not Found"}` |
  | `IntegrityError` (уникальность, внешний ключ, защищённая связь) | `409 Conflict`, `{"detail": "Conflict"}` — без сообщения базы, в котором названы таблицы и ограничения |
  | `InvalidRequestQuery` | `422 Unprocessable Content`, как FastAPI сам отвечает на неверные параметры: `{"detail": [{"type", "loc": ["query", name], "msg", "input"}]}`; у ошибки всего запроса (от проверки модели) `"loc": ["query"]`, а в `input` — параметры запроса |
  | `RequestQueryForbidden` (параметр просит то, что запросу видеть нельзя: удалённые строки без разрешения `may_see_deleted()`) | `403 Forbidden`, причина — в `detail` |

- **Собственное состояние у каждого запроса.** Каждый запрос начинает с пустого
  счётчика `RepeatedQueryDetector`.
  Только его собственные записи направляют его чтения в подключение, через которое он писал
  ([чтение своих записей](../connections/multiple-databases.ru.md#read-your-writes)), — не записи
  запросов, которые его задача обслужила раньше, и не записи при старте приложения.
- **Транзакция на каждый запрос** при `atomic_requests` — см. ниже.

## <a id="request-queries-as-dependencies"></a>Запросы из параметров как зависимости

`RequestQueryDependency.provide(QueryClass)` — это `Depends()`, который строит запрос из параметров
HTTP-запроса. Каждый параметр класса становится параметром маршрута, проверяется FastAPI и
перечисляется в схеме OpenAPI с типом, значением по умолчанию, ограничениями, перечислением и
описанием:

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

`KeyColumns` и `CommaSeparated` приходят в запрос текстом параметра, который запрос разбирает сам.
Запрос получает сам HTTP-запрос: `query.request`, а также адреса `next` и `previous` для страницы.
На значение, которое запрос отклоняет, — сортировку, которую он не разрешает, испорченный курсор, —
отвечают так же, как FastAPI отвечает на неверный параметр.

## <a id="responses"></a>Ответы

Маршрут возвращает модели hare со своим `response_model`: для строки или списка (`list[BookSchema]`) —
схемой одной строки с `from_attributes=True`, а для страницы строк — схемой страницы:
`PageSchema[BookSchema]` (`ScrollPageSchema`, `CursorPageSchema` для других видов страниц). FastAPI
проверяет то, что возвращает маршрут, по его атрибутам и описывает это в схеме. Строки страницы лежат
в `result`.

## <a id="atomic-requests"></a>Транзакция на каждый запрос: `atomic_requests`

`atomic_requests=True` выполняет каждый HTTP-запрос в транзакции на подключении по умолчанию, как
`ATOMIC_REQUESTS` в Django; последовательность имён подключений открывает транзакцию на каждом из них.
Транзакция:

- фиксируется, когда ответ начинается со статусом меньше 500, — до отправки ответа, поэтому на
  неудачную фиксацию всё ещё отвечают ошибкой; тело потокового ответа отправляется после фиксации;
- откатывается при ответе со статусом 500 и выше и при исключении, обработанном или нет;
- как обычно, выполняет после фиксации функции `Transactions.on_commit()`.

Маршрут, обработчик которого помечен `RequestTransaction.skip`, выполняется без неё:

```python
from hare.contrib.frameworks import RequestTransaction


@app.post("/imports")
@RequestTransaction.skip
async def import_books(...): ...
```

Соединения WebSocket никогда не выполняются в транзакции. С несколькими подключениями транзакции
фиксируются по очереди; неудачная фиксация одной откатывает ещё не зафиксированные, но не уже
зафиксированные. Имя, которого нет в конфигурации, не даёт приложению запуститься.

## <a id="health-routes"></a>Маршруты здоровья: `health_routes`

```python
from hare.contrib.frameworks.health_routes import HealthRoutes
from hare.health import HealthCheck

app = HareFastAPI(hare_config=HARE_CONFIG, health_routes=HealthRoutes(HealthCheck()))
```

Добавляет маршрут готовности (`/health/ready`: подключения пингуются и оцениваются `HealthCheck` —
200, `degraded_status_code` при деградации, 503 при неработоспособности) и маршрут
работоспособности (`/health/live`: 200, пока процесс отвечает, база не трогается). Ни один не
работает в транзакции запроса и не попадает в схему OpenAPI. Пути, статус при деградации и подробности
ответа — аргументы `HealthRoutes`, см. [Метрики пула и здоровье](../observability/pool-health.ru.md#liveness-and-readiness).
