# Robyn

`hare.contrib.frameworks.robyn` подключает hare к приложению [Robyn](https://robyn.tech). Ставится вместе
с дополнительным пакетом `robyn` (Robyn 0.88, pydantic).

```python
from pydantic import BaseModel, ConfigDict

from hare.contrib.frameworks import PageSchema
from hare.contrib.frameworks.robyn import HareRobyn
from hare.contrib.request_query import Page

HARE_CONFIG = {
    "connections": {"default": "postgresql://library:secret@localhost:5432/library"},
    "apps": {"library": {"models": ["library.models"], "default_connection": "default"}},
}

app = HareRobyn(__file__, hare_config=HARE_CONFIG, atomic_requests=True)


class BookSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str


@app.get("/books", response_model=PageSchema[BookSchema])
async def list_books(books: BookQuery) -> Page[Book]:
    return await books.page()
```

## <a id="harerobyn"></a>`HareRobyn`

```python
class HareRobyn(Robyn):
    def __init__(
        self,
        file_object: str,
        *,
        hare_config: Mapping[str, Any] | HareConfig | str,
        atomic_requests: bool | Sequence[str] = False,
        openapi: OpenAPI | None = None,
        health_routes: HealthRoutes | None = None,
        **kwargs: Any,
    ) -> None: ...
```

`HareRobyn` — это приложение `Robyn`; `hare_config` — то, что принимает `Hare.init(config=...)`,
`file_object` и `kwargs` — то, что принимает `Robyn()`.

- **`HareContext`.** Модели привязываются при создании приложения; контекст открывается при запуске
  рабочего процесса — до собственного `startup_handler` приложения — и закрывается при его остановке,
  после собственного `shutdown_handler`. Контекст — глобальный запасной, поэтому его видит каждый запрос.
  При остановке `Hare.close_connections()` ещё и дожидается фоновых наблюдателей, которые ещё работают.
- **Запросы из параметров проверяются при запуске.** `RequestQuery.check_declarations()` выполняется, как
  только модели настроены: ошибочный [запрос из параметров](request-queries.ru.md) не даёт процессу
  запуститься.
- **Запросы из параметров для обработчиков** и их параметры в схеме OpenAPI — см. ниже.
- **Ответы, проверенные `response_model` маршрута** — см. ниже.
- **Ошибки ORM как ответы HTTP** (`HareExceptionHandlers`):

  | Ошибка | Ответ |
  |---|---|
  | `InvalidRequestQuery` | `422 Unprocessable Content`, как Robyn сам отвечает на неверное тело pydantic: `{"error": "Validation Error", "detail": [{"type", "loc": ["query", name], "msg", "input"}]}`; у ошибки всего запроса (от проверки модели) `"loc": ["query"]`, а в `input` — параметры запроса |
  | `DoesNotExist` | `404 Not Found`, `{"detail": "Not Found"}` |
  | `IntegrityError` (уникальность, внешний ключ, защищённая связь) | `409 Conflict`, `{"detail": "Conflict"}` — без сообщения базы, в котором названы таблицы и ограничения |
  | `ValidationError` (значение, которое поле не принимает: вне диапазона, слишком длинное, с нулевым байтом) | `422 Unprocessable Content`, сообщение поля в `detail` |
  | `RequestQueryForbidden` (параметр просит то, что запросу видеть нельзя: удалённые строки без разрешения `may_see_deleted()`) | `403 Forbidden`, причина — в `detail` |

  Любую другую ошибку обрабатывает Robyn (`500`). Robyn хранит один обработчик исключений на приложение и
  передаёт его каждому маршруту при объявлении маршрута: приложение, которое отвечает на ошибки само,
  задаёт свой обработчик через `exception()` до объявления маршрутов и первым делом вызывает в нём
  `HareExceptionHandlers.get_response(error)`.
- **Собственное состояние у каждого запроса.** Каждый запрос начинает с пустого счётчика
  `RepeatedQueryDetector` (обработчик `before_request`). Только его собственные записи направляют его
  чтения в подключение, через которое он писал ([чтение своих
  записей](../connections/multiple-databases.ru.md#read-your-writes)), — не записи запросов, которые его
  задача обслужила раньше, и не записи при старте приложения.
- **Транзакция на каждый запрос** при `atomic_requests` — см. ниже.

Маршрутизатор приложения — это `HareSubRouter`, то есть `SubRouter`, маршруты которого получают то же
самое, а транзакции — когда приложение его подключит:

```python
from hare.contrib.frameworks.robyn import HareSubRouter

chapters = HareSubRouter(prefix="/chapters")


@chapters.get("/:pk", response_model=ChapterSchema)
async def get_chapter(chapter: ChapterQuery) -> Chapter:
    return await chapter.get()


app.include_router(chapters)
```

## <a id="request-queries-for-handlers"></a>Запросы из параметров для обработчиков

Параметр обработчика с аннотацией класса запроса из параметров (`books: BookQuery`) получает запрос,
построенный из HTTP-запроса: из строки параметров — повторяемое имя для каждого значения списка,
`?status__in=draft&status__in=published` — и из параметров пути для параметров с пометкой `InPath()`
(`pk: Annotated[KeyColumns[int, int], InPath()]` с `/chapters/:pk`). Сам Robyn читал бы модель pydantic
из тела запроса. Запрос получает сам HTTP-запрос (`query.request`) и полный адрес запроса для `next` и
`previous` страницы. На значение, которое запрос отклоняет, отвечают `422`.

Схема OpenAPI (`HareOpenAPI`, по умолчанию — схема приложения) перечисляет каждый параметр запроса —
параметр строки запроса или параметр пути для отмеченного `InPath()` — с типом, ограничениями,
перечислением и описанием.

## <a id="responses"></a>Ответы

Ответ маршрута — строка, список, страница — проверяется по `response_model` по его атрибутам и отдаётся
как JSON со `status_code` маршрута (по умолчанию 200); сам Robyn проверяет только возвращённый словарь.
Возвращённый `Response` отправляется как есть. Схемы строки, списка и страницы — см.
[Веб-фреймворки](web-frameworks.ru.md#response-schemas).

## <a id="atomic-requests"></a>Транзакция на каждый запрос: `atomic_requests`

`HareRobyn(..., atomic_requests=True)` выполняет каждый обработчик в транзакции, которая фиксируется до
того, как Robyn отправит ответ; обработчик, помеченный `RequestTransaction.skip`, выполняется без неё,
как и константный маршрут:

```python
from hare.contrib.frameworks import RequestTransaction


@app.post("/imports")
@RequestTransaction.skip
async def import_books(request: Request) -> None: ...
```

Когда транзакция фиксируется или откатывается — см.
[Веб-фреймворки](web-frameworks.ru.md#atomic-requests).

## <a id="tenancy"></a>Арендатор на каждый запрос

Robyn выполняет хуки `before_request` отдельно от обработчика запроса: область, которую задаёт синхронный
хук через `Tenancy.set()`, доходит до обработчика, а заданная `async`-хуком — нет. `HareRobyn` начинает
каждый запрос без области арендатора и без тегов запросов — раньше собственных хуков приложения, —
поэтому запрос без арендатора никогда не читает строки предыдущего запроса:

```python
@app.before_request()
def set_tenant(request: Request) -> Request:
    company = request.headers.get("x-company")
    if company:
        Tenancy.set(int(company))
    return request
```

Запрос, которому хук не задал арендатора, обращается к модели с арендатором без области и получает
`QueryError`. Обработчик может открыть область и сам — `with Tenancy.scope(company_id): ...`.

## <a id="health-routes"></a>Маршруты здоровья: `health_routes`

```python
from hare.contrib.frameworks.health_routes import HealthRoutes
from hare.health import HealthCheck

app = HareRobyn(__file__, hare_config=HARE_CONFIG, health_routes=HealthRoutes(HealthCheck()))
```

Какие маршруты готовности и работоспособности он добавляет — см.
[Веб-фреймворки](web-frameworks.ru.md#health-routes).
