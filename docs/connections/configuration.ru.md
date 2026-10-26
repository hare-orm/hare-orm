# Настройка и `Hare.init()`

Как приложение запускает hare и останавливает его: `Hare.init()` и словарь `config`, который он
принимает, `HareContext`, в котором работает процесс или тест, запросы, созданные до `init()`, привязка
моделей без базы, закрытие подключений и `Hare.run_async()` для скрипта.

## <a id="hare-init"></a>`Hare.init()`

Приложение на hare начинается с вызова `Hare.init()`. Он регистрирует модели, настраивает подключения к
базам и возвращает `HareContext`, который нужно хранить, пока работает приложение. Само соединение (или
пул соединений) открывается при первом запросе, а не в `init()`.

```python
class Hare:
    @classmethod
    async def init(
        cls,
        config: Mapping[str, Any] | HareConfig | str,
        *,
        connect: bool = True,
        _create_db: bool = False,
        use_timezone: bool = True,
        timezone: str = "UTC",
        routers: list[str | type] | None = None,
        table_name_generator: Callable[[type[Model]], str] | None = None,
        slow_query_threshold_ms: float = 500.0,
        _enable_global_fallback: bool = False,
    ) -> HareContext: ...
```

Где лежат настройки, говорит один аргумент — `config`, — и он принимает любую форму (читает его
`HareConfig.load(source)` — и для `Hare.init()`, и для CLI):

```python
await Hare.init({"connections": {...}, "apps": {...}})            # словарь — формат описан ниже
await Hare.init(HareConfig(connections=..., apps=...))            # то же типизированным объектом
await Hare.init("config/hare.yml")                                # файл .json/.yml/.yaml в том же формате
await Hare.init("settings.HARE_ORM")                              # module.VARIABLE со словарём или HareConfig
await Hare.init(HareConfig.from_db_url("sqlite+aiosqlite://db.sqlite3", {"models": ["library.models"]}))  # один адрес
```

`HareConfig.from_db_url(db_url, modules)` строит настройки одного подключения (`"default"`) из адреса
базы и `{app_label: [module paths]}`. Источник, который нельзя прочитать, — отсутствующий файл,
несуществующий модуль или переменная — даёт `ConfigurationError`.

Общие для всего процесса настройки (`slow_query_threshold_ms` и `table_name_generator`) меняются только
после успешного `init()`; если `init()` завершился ошибкой, они остаются прежними.

| Аргумент | Что задаёт |
|---|---|
| `config` | Словарь, `HareConfig`, путь к файлу или `"module.VARIABLE"` — см. выше; формат описан ниже. |
| `connect` | `False` — настроить модели и подключения, не подключаясь ни к одной базе; имена подключений всё равно сверяются с настройками. Для инструментов, которым нужны модели и их SQL, но не база: создание миграций, вывод DDL. |
| `_create_db` | Сначала создать саму базу (удобно в тестах). |
| `use_timezone` / `timezone` | Как `DatetimeField` работает с часовыми поясами. |
| `routers` | Список классов маршрутизаторов или путей к ним через точку — см. [Маршрутизаторы](multiple-databases.ru.md#routers). |
| `table_name_generator` | Функция `Callable[[type[Model]], str]`, которая заменяет правило именования таблиц — см. [`table_name_generator`](#table-name-generator) |
| `slow_query_threshold_ms` | Запросы, которые выполнялись столько миллисекунд или дольше, записываются в журнал на уровне DEBUG как «Slow query». По умолчанию 500 мс — см. [Журнал медленных запросов](../observability/observers.ru.md#slow-query-logging). |
| `_enable_global_fallback` | Позволяет коду, который выполняется не в той задаче asyncio, что вызвала `init()` (например, в фоновой задаче, запущенной при старте ASGI-приложения), найти текущий контекст. Интеграции с Litestar, FastAPI и Robyn включают это сами. |

### <a id="table-name-generator"></a>`table_name_generator`

Функция `Callable[[type[Model]], str]`, которая заменяет правило именования таблиц. Сгенерированное имя
вычисляется заново при каждом `init()`: следующий `init()` с другой функцией или без неё переименует
таблицу модели. Явно указанная `Meta.table` всегда важнее. Имена, которые выводятся из имени таблицы при
первой настройке связей, — автоматически созданная промежуточная таблица связи «многие-ко-многим», её
колонки, `related_name` по умолчанию — сохраняют первое значение.

## <a id="the-config-dict"></a>Словарь `config`

Настройки, которые `Hare.init()` принимает словарём, — подключения, приложения и настройки всего
контекста:

```python
config = {
    "connections": {
        "default": "postgresql://postgres:qwerty123@localhost:5432/library",  # просто строка-адрес...
        "reporting": {  # ...или подробный словарь - engine это схема, которая была бы в адресе
            "engine": "postgresql",
            "credentials": {
                "host": "localhost", "port": "5432",
                "user": "postgres", "password": "qwerty123", "database": "reporting",
            },
        },
    },
    "apps": {
        "library": {
            "models": ["library.models"],
            "default_connection": "default",   # необязательно, по умолчанию "default"
            "migrations": "library.migrations",  # необязательно, нужно командам CLI
        },
    },
    "routers": ["library.routers.ReportingRouter"],
    "use_timezone": True,
    "timezone": "UTC",
    "swappable": {"USER_MODEL": "library.Reader"},  # необязательно, см. ниже
    "migrations": {"lock_timeout": 5},  # необязательно, см. ниже
}

context = await Hare.init(config=config)
```

### <a id="config-classes"></a>`HareConfig` и другие классы настроек

```python
class HareConfig:
    def __init__(
        self,
        connections: dict[str, ConnectionConfig | DBUrlConfig],
        apps: dict[str, AppConfig],
        routers: list[str | type] | None = None,
        read_your_writes_seconds: float | None = None,
        use_timezone: bool | None = None,
        timezone: str | None = None,
        cli: CliConfig | None = None,
        swappable: dict[str, str | dict[str, str]] | None = None,
        migrations: MigrationsConfig | None = None,
    ) -> None: ...

    @classmethod
    def from_db_url(cls, db_url: str, modules: Mapping[str, Iterable[str | ModuleType]]) -> Self: ...

    @classmethod
    def load(cls, source: Mapping[str, Any] | Self | str) -> Self: ...


class ConnectionConfig:
    def __init__(
        self, engine: str | None = None, credentials: dict[str, Any] = {}, db_url: str | None = None
    ) -> None: ...


class DBUrlConfig:
    def __init__(self, url: str) -> None: ...


class AppConfig:
    def __init__(
        self,
        models: Iterable[str | ModuleType],
        default_connection: str | None = None,
        migrations: str | None = None,
    ) -> None: ...


class CliConfig:
    def __init__(self, commands: list[str] = []) -> None: ...


class MigrationsConfig:
    def __init__(self, lock_timeout: float | None = None, safety: MigrationSafetyConfig | None = None) -> None: ...


class MigrationSafetyConfig:
    def __init__(self, large_table_rows: int = 100000) -> None: ...
```

Настройки можно собрать в коде из этих классов `hare.core.config`, а не словарём.
`read_your_writes_seconds` — см. [Чтение своих записей](multiple-databases.ru.md#read-your-writes); `cli`
задаёт дополнительные подкоманды `hare`. Подключение можно задать и словарём `{"db_url": "..."}` — в
такой вид превращается `ConnectionConfig(db_url=...)`. `models` — список или кортеж путей к модулям или
самих модулей.

### <a id="migrations-settings"></a>Раздел `migrations`

`migrations` принимает `lock_timeout`: сколько секунд команда миграции может ждать блокировку, взятую
другим сеансом, прежде чем `migrate` завершится ошибкой, — см.
[Ожидание блокировок](../migrations/migrations.ru.md#lock-timeout); и `safety` — настройки проверки
миграций: `{"large_table_rows": 100000}` — с какого числа строк `checkmigrations` считает таблицу
большой, целое число от 0 до 10<sup>12</sup>, — см. [Проверка
миграций](../migrations/zero-downtime.ru.md#checking-migrations).

### <a id="swappable-settings"></a>Раздел `swappable`

`swappable` сопоставляет имени настройки модель в виде `"app_label.ModelName"` — ту, на которую указывают
связи, объявленные через `swappable("USER_MODEL")`. Так проект заменяет модель, поставляемую пакетом,
своей (см. [Заменяемые модели](../models/relations.ru.md#swappable-models)). Имя настройки пишется
заглавными буквами (`USER_MODEL`), значение называет одно из настроенных приложений. Кроме того, `init()`
проверяет, что модель зарегистрирована, не абстрактная и сама не заменена другой моделью через свою
настройку. Каждая из этих ошибок даёт свой `ConfigurationError`. `Hare.swappable_label("USER_MODEL")`
возвращает метку модели, на которую указывает настройка, а `Hare.get_swappable_model("USER_MODEL")` —
саму модель. Если настройки нет в конфигурации, она указывает на модель, объявившую
`Meta.swappable = "USER_MODEL"`. Значением настройки может быть и словарь «имя ветки → метка» —
`{"COMMENT_TARGETS": {"post": "blog.Post", "photo": "media.Photo"}}`, цели
[`GenericForeignKeyField(swappable("COMMENT_TARGETS"))`](../models/relations.ru.md#generic-foreign-key-swappable);
каждое имя ветки — идентификатор Python, каждая метка проверяется так же, как выше. Такая настройка
должна быть задана в конфигурации, и принимает её только обобщённая связь.

### <a id="config-checks"></a>Проверки при `init()`

Ключи словаря настроек — это поля тех же классов: на верхнем уровне поля `HareConfig`, в подключении,
заданном словарём, — поля `ConnectionConfig`, в приложении — `AppConfig`, в разделе `cli` — `CliConfig`.
Любой другой ключ даёт `ConfigurationError` с ближайшим известным именем: иначе опечатка вроде
`default_conection` молча оставила бы модели приложения на подключении по умолчанию.

Неизвестный `engine` даёт `ConfigurationError`, как и параметр подключения, которого не знает драйвер:
опечатку в `credentials` (`comand_timeout`) отклоняют оба драйвера PostgreSQL. `rust_pg` принимает только
свои параметры, `asyncpg` — только то, что принимают `asyncpg.connect()`/`create_pool()`.

Связь с `db_constraint=True` (так по умолчанию) между моделями, приложения которых работают через разные
подключения, даёт `ConfigurationError` при `init()`: внешний ключ базы не может ссылаться на таблицу в
другой базе. Объявите такую связь с `db_constraint=False` — тогда её `on_delete` выполняет сам hare.

### <a id="masked-secrets"></a>Секреты в `repr()` и журнале

`repr()` этих объектов (и настройки подключений, которые hare пишет в журнал на уровне DEBUG при
`init()`) заменяет каждое секретное значение на `***`: пароль в адресе базы, секретный параметр адреса
(`sslpassword=...`) и каждый параметр `credentials`, в имени которого есть `password`, `passwd`, `pwd`,
`secret` или `token`. Никакая часть секрета не показывается.

## <a id="hare-context"></a>`HareContext`

```python
context = await Hare.init(HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": ["library.models"]}))

# позже, при остановке приложения:
await context.close_connections()
```

`HareContext` можно использовать и в блоке `async with` — так обычно делают в тестах и скриптах:

```python
async with HareContext() as context:
    await context.init(HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": ["library.models"]}))
    await context.generate_schemas()
    ...
```

`Hare` — это контекст текущей задачи под постоянным именем: `Hare.init()` настраивает текущий
`HareContext` задачи (новый, если у задачи его нет), а все остальные методы `Hare` работают с текущим.
Всё определено один раз, в `HareContext`: `.init(config, ...)`, `.generate_schemas(safe=True)`,
`.close_connections()`, `.connections` (`ConnectionHandler`), `.apps`, `.router`, `.routers`, `.inited`,
`.default_connection`, `.use_timezone`, `.timezone`, `.get_model("app.Model")` (метка, класс модели или
настройка `swappable()`),
`.get_connection(connection_alias=None)`, `.bind_models(config, ...)`, `.swappable_label(setting)`,
`.get_swappable_model(setting)`, `.register_live_models(...)`, `.unregister_live_models(...)` и
`.observe(event_type, callback)`/`.unobserve(...)` (см. [Наблюдение за
ORM](../observability/observers.ru.md#where-observers-live)). `HareContext.get_current()` возвращает
текущий контекст или `None`, `HareContext.require_current()` — текущий контекст или `ConfigurationError`.

Классы моделей общие для всех контекстов, загрузивших их модуль: контекст, настроенный внутри другого,
привязывает их (соединение, имена таблиц, связи) к своей конфигурации, а выход из него — `with` или
`async with` — привязывает их обратно к контексту, который снова стал текущим.

## <a id="querysets-before-init"></a>Запросы, построенные до `init()`

QuerySet можно построить до `Hare.init()` — при импорте, как константу модуля или атрибут класса:

```python
PUBLISHED = Book.objects.filter(published=True).order_by("-rating")   # init() ещё не было


async def list_published() -> list[Book]:
    return await PUBLISHED.limit(20)          # после init(): обычный QuerySet
```

Пока модели не привязаны, цепочку нельзя проверить по ним (связи ещё не настроены), поэтому каждый такой
QuerySet запоминается, а `init()` повторяет его вызовы сразу после привязки моделей. С этого момента это
обычный, полностью построенный QuerySet — ничего ленивого в нём нет. Поэтому ошибка в такой цепочке
(неизвестное поле, неверный оператор фильтра) даёт `ConfigurationError` из `Hare.init()` (или
`Hare.bind_models()`) с текстом ошибки, которую дал бы этот вызов после `init()`. Выполнение запроса до
`init()` даёт `ConfigurationError`.

## <a id="bind-models"></a>Привязка моделей без базы

```python
Hare.bind_models(config, table_name_generator=None)   # синхронный
```

Привязывает модели из настроек — связи, заменяемые модели, фильтры и сортировки, — не подключаясь и не
оставляя текущего контекста. Это для кода, которому модели нужны до запуска приложения: веб-фреймворку,
который при импорте строит сигнатуры обработчиков по
[`lookup_info`](../querying/describing-filters.ru.md). Подключения потом, как обычно, настраивает
`Hare.init()`. `await Hare.init(config, connect=False)` — асинхронная форма, которая оставляет текущий
контекст: с моделями, маршрутизаторами и настройками подключений, но без единого соединения.

## <a id="closing-connections"></a>Закрытие подключений

`Hare.close_connections()` закрывает все подключения текущего контекста и выбрасывает их; без контекста
ничего не делает. Сколько он ждёт ещё идущие запросы, зависит от базы:

| База данных | Поведение |
|---|---|
| PostgreSQL | Каждый пул (и `asyncpg`, и `rust_pg`) до 10 секунд ждёт соединения, которые держат или открывают его запросы и транзакции, затем разрывает их: сервер откатывает ещё открытые транзакции и закрывает их сессии |
| SQLite | Подключение до 10 секунд ждёт транзакцию или запрос, которые его используют, затем закрывается всё равно |
| ClickHouse | `clickhouse_driver` до 10 секунд ждёт уже переданные команды; команда, которая ещё идёт, заканчивается сама. `clickhouse_connect` закрывает свой HTTP-клиент, когда на уже отправленные запросы пришли ответы, — они завершаются со своими результатами |

Задача, которая ещё держит выброшенный объект подключения, на следующем запросе получает `DBConnectionError`,
а не открывает новый пул; запрос через модель снова ищет своё подключение — и внутри транзакции тоже — и
получает `ConfigurationError`, как до `Hare.init()`. Контекст забывает свои подключения, даже если закрытие не
удалось, а первая ошибка поднимается после; наблюдателям, которые ещё отправляют метрику или трассировку,
даётся до 10 секунд. `close_all()` у `ConnectionHandler` — то же закрытие для одного контекста — см.
[Подключения](connections.ru.md#connections).

```python
await Hare.close_connections()
```

## <a id="other-hare-classmethods"></a>Другие методы класса `Hare`

- `Hare.generate_schemas(safe=True)` — сразу создать таблицы всех зарегистрированных моделей, без
  миграций. Удобно для тестов и прототипов, но не для рабочей базы. Таблицы создаются в порядке, которого
  требуют их внешние ключи; если внешние ключи моделей ссылаются друг на друга по кругу, одна таблица
  круга создаётся без внешнего ключа на таблицу, создаваемую позже, а ключ добавляется через
  `ALTER TABLE ... ADD CONSTRAINT`, когда та таблица уже есть (с `safe=True` — только если его нет).
  SQLite проверяет ссылку только при записи строки, поэтому там каждая таблица сохраняет свой
  `REFERENCES`.
- `connection.get_schema_sql(safe)` — вернуть те же команды создания таблиц строкой, не выполняя их, —
  чтобы посмотреть, записать в журнал или сравнить.
- `Hare.is_inited()`, `Hare.apps` (модели текущего контекста; `None`, если его нет), `Hare.get_context()`
  (текущий `HareContext`; без него — `ConfigurationError`).
- `Hare.swappable_label(setting)` / `Hare.get_swappable_model(setting)` — см.
  [Заменяемые модели](../models/relations.ru.md#swappable-models).
- `Hare.register_live_models(models, app_label, connection_alias="default", *, managed=None)` /
  `Hare.unregister_live_models(models)` — см.
  [Регистрация модели во время работы](../models/runtime-models.ru.md).

Чтобы описать модель — её поля, связи, фильтры и сортировки каждого поля, — читайте `Model._meta` и
[`lookup_info`](../querying/describing-filters.ru.md); аргументы конструктора поля даёт
`field.deconstruct()`.

> [!NOTE]
> Функция для тестов `hare_test_context(modules, db_url="sqlite+aiosqlite://:memory:", ...)` выполняет
> весь цикл — `init()`, создание таблиц, закрытие и удаление базы — как один асинхронный контекстный
> менеджер. Используйте её в своих фикстурах, а не повторяйте эти шаги вручную. Подробнее — в разделе
> [Тестирование](../testing/setup.ru.md), вместе с остальным `hare.contrib.test`.

## <a id="run-async"></a>Скрипты: `Hare.run_async()`

Для разового скрипта (не долго работающего приложения и не теста) `Hare.run_async()` вызывает
`asyncio.run` и сам закрывает соединения в конце:

```python
from hare import Hare, HareConfig


async def main() -> None:
    await Hare.init(HareConfig.from_db_url("sqlite+aiosqlite://db.sqlite3", {"models": ["app.models"]}))
    ...


Hare.run_async(main())
```
