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
        routers: list[str | Callable[[], object]] | None = None,
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

`table_name_generator` принадлежит контексту и его реестру моделей. Контекст публикует его
и обновляет общую для процесса настройку `slow_query_threshold_ms` только после успешного `init()`.

Каждый реестр готовит отдельный граф привязок из объявлений Python-классов. Один класс модели
можно одновременно использовать в разных контекстах с разными таблицами, приложениями и
подключениями. Класс хранит объявление; подготовленные поля, связи и SQL-метаданные принадлежат
`ModelBinding` из `context.apps.get_binding(Book)`. Выход из вложенного контекста восстанавливает
только текущий контекст задачи: атрибуты классов не заменяются.

Запрос и экземпляр сохраняют исходные граф и контекст. Выполнение старого запроса под другим
текущим контекстом использует исходное приложение, включая его роутеры, шифрование и часовой пояс.
Смешивать явно привязанные запросы и связанные экземпляры разных графов или контекстов нельзя.
Шаблон без привязки внутри другого запроса получает граф внешнего запроса.

Передавать такому запросу или экземпляру клиент другого `HareContext` нельзя:
`ConfigurationError` возникает до SQL, даже если alias обоих клиентов совпадает.
Для работы с другой базой создайте запрос через реестр соответствующего контекста.
Транзакционные, независимые и tenant-клиенты сохраняют контекст исходного подключения.

Поля и настройки Meta фиксируются при создании Python-класса, включая вложенные изменяемые
объекты схемы. Последующие изменения исходного Meta или переданных настроек не перенастраивают
ни существующие, ни будущие графы. Callback сохраняет обычную идентичность и выполняется
с настройками выбранного приложения.

Методы чтения реестра не регистрируют модели. Подготовка добавляет узлы явно; неопубликованный
граф не может выполнять запросы, в том числе через объект, сохранённый неудачным extension hook.
Метод `save()` такого экземпляра отказывает до ожидания асинхронных значений по умолчанию
и подготовки полей. Любая подготовка реестра, включая удаление моделей, выполняет callback
в контексте этого реестра и восстанавливает текущий контекст вызывающего кода при любом исходе.
Кэши менеджеров и request-схем следуют версии своего реестра. Подготовка исторического реестра
не сбрасывает записи другого реестра.

Для компиляции SQL отвязанного объявления view или проекции нужен явный клиент базы.
Привязанный источник сохраняет свои настройки и отвергает клиент другого приложения.
Клиент без обработчика подключений позволяет компилировать отвязанные объявления без
контекста приложения.

Регистрация и удаление моделей публикуют новый полный граф атомарно. При ошибке прежний граф
остаётся действующим. Ранее созданные запросы и экземпляры сохраняют прежний граф; удаление
таблицы самой базой всё равно может сделать их SQL недействительным. Все цели связей должны
принадлежать выбранному реестру: ссылка на Python-класс не заимствует настройки другого приложения.

Один экземпляр `HareContext` инициализируется один раз. Для другой конфигурации создайте новый
контекст. После закрытия исходного контекста его объекты и запросы не выполняют обращения к БД.

| Аргумент | Что задаёт |
|---|---|
| `config` | Словарь, `HareConfig`, путь к файлу или `"module.VARIABLE"` — см. выше; формат описан ниже. |
| `connect` | `False` — настроить модели и подключения, не подключаясь ни к одной базе; имена подключений всё равно сверяются с настройками. Для инструментов, которым нужны модели и их SQL, но не база: создание миграций, вывод DDL. |
| `_create_db` | Сначала создать саму базу (удобно в тестах). |
| `use_timezone` / `timezone` | Как `DatetimeField` работает с часовыми поясами. |
| `routers` | Классы или фабрики маршрутизаторов без аргументов, напрямую или по пути через точку — см. [Маршрутизаторы](multiple-databases.ru.md#routers). |
| `table_name_generator` | Функция `Callable[[type[Model]], str]`, которая заменяет правило именования таблиц — см. [`table_name_generator`](#table-name-generator) |
| `slow_query_threshold_ms` | Запросы, которые выполнялись столько миллисекунд или дольше, записываются в журнал на уровне DEBUG как «Slow query». По умолчанию 500 мс — см. [Журнал медленных запросов](../observability/observers.ru.md#slow-query-logging). |
| `_enable_global_fallback` | Позволяет коду, который выполняется не в той задаче asyncio, что вызвала `init()` (например, в фоновой задаче, запущенной при старте ASGI-приложения), найти текущий контекст. Интеграции с Litestar, FastAPI и Robyn включают это сами. |

### <a id="table-name-generator"></a>`table_name_generator`

`Callable[[type[Model]], str]` задаёт имена таблиц для одного реестра. Явное `Meta.table`
имеет приоритет. Другой контекст может использовать другой генератор для тех же классов;
имена таблиц и автоматически образованные имена связей вычисляются независимо в каждом графе.

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
        routers: list[str | Callable[[], object]] | None = None,
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
такой вид превращается `ConnectionConfig(db_url=...)`. `models` принимает итерируемый объект с путями
к модулям или самими модулями, например список или кортеж; он перебирается один раз.

### <a id="migrations-settings"></a>Раздел `migrations`

`migrations` принимает `lock_timeout`: сколько секунд команда миграции может ждать блокировку, взятую
другим сеансом, прежде чем `migrate` завершится ошибкой, — см.
[Ожидание блокировок](../migrations/migrations.ru.md#lock-timeout); и `safety` — настройки проверки
миграций: `{"large_table_rows": 100000}` — с какого числа строк `checkmigrations` считает таблицу
большой, — см. [Проверка миграций](../migrations/zero-downtime.ru.md#checking-migrations).

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
`default_conection` молча оставила бы модели приложения на подключении по умолчанию. Настройки, разделы
`connections` и `apps` и каждое приложение используют операции отображения; подключение — отображение
или адрес. Совместимые объекты принимаются без проверки класса. Неверная структура Python может дать
естественный `TypeError` или `AttributeError`.

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

Контексты, загружающие один модуль моделей, используют общие классы. У каждого контекста свои
привязки соединений, имён таблиц и связей. После выхода из вложенного контекста текущим снова
становится внешний. Уже привязанные запросы и экземпляры моделей сохраняют свои привязки.

`Model.objects` выбирает активный реестр при создании запроса; `apps.get_queryset(Model)`
выбирает реестр явно. Низкоуровневый конструктор `QuerySet(Model)` создаёт непривязанное
объявление даже при активном приложении. Чтобы сохранить конкретное приложение до выполнения
запроса, используйте менеджер модели или фабрику реестра.

## <a id="querysets-before-init"></a>Запросы до инициализации

Запрос, созданный до подготовки реестра, остаётся неизменяемым шаблоном:

```python
PUBLISHED = Book.objects.filter(published=True).order_by("-rating")

async def list_published() -> list[Book]:
    return await PUBLISHED.limit(20)
```

При выполнении цепочка воспроизводится в локальную копию на графе текущего приложения.
Сам `PUBLISHED` не меняется и может использоваться другим приложением независимо. Проверка
отложенных вызовов происходит при такой привязке, а не во время `init()`. Для ранней проверки
получите запрос через `apps.get_queryset(Book)` из явно подготовленного реестра.
Без приложения выполнение шаблона вызывает `ConfigurationError`.

## <a id="bind-models"></a>Подготовка реестра без БД

```python
apps = Hare.bind_models(CONFIG)
binding = apps.get_binding(Book)
info = binding.get_lookup_info("author__name__icontains")
queryset = apps.get_queryset(Book).order_by("title")
book = apps.new(Book, title="Example")
trusted = apps.construct(Book, id=1, title="Example")
```

`bind_models()` синхронно готовит поля, связи и описания запросов; подключения не открываются,
текущий контекст не меняется. Используйте возвращённый `Apps` явно. `apps.new()` вызывает обычный
конструктор, включая пользовательский `__init__`; `apps.construct()` использует обычный доверенный
конструктор без валидации. Такие объекты не имеют приложения для IO. Для работы с БД используйте
реестр инициализированного контекста либо явно вызовите `context.attach(book)`.

Модули моделей можно передавать объектами модулей, в том числе отсутствующими в `sys.modules`.
Неудачная подготовка не меняет опубликованные графы или отложенные шаблоны.

## <a id="closing-connections"></a>Закрытие подключений

`Hare.close_connections()` закрывает все подключения текущего контекста и выбрасывает их; без контекста
ничего не делает. Сколько он ждёт ещё идущие запросы, зависит от базы:

| База данных | Поведение |
|---|---|
| PostgreSQL | Каждый пул (и `asyncpg`, и `rust_pg`) до 10 секунд ждёт соединения, которые держат или открывают его запросы и транзакции, затем разрывает их: сервер откатывает ещё открытые транзакции и закрывает их сессии |
| SQLite | Подключение до 10 секунд ждёт транзакцию или запрос, которые его используют, затем закрывается всё равно |
| ClickHouse | `clickhouse_driver` до 10 секунд ждёт уже переданные команды; команда, которая ещё идёт, заканчивается сама. `clickhouse_connect` закрывает свой HTTP-клиент, когда на уже отправленные запросы пришли ответы, — они завершаются со своими результатами |

Закрытие пула PostgreSQL дожидается остановки задач его активных соединений. После возврата из закрытия
сохранённая вызывающим кодом транзакция уже не может выполнить коммит через эти соединения.

Задача, которая ещё держит выброшенный объект подключения, на следующем запросе получает `DBConnectionError`,
а не открывает новый пул; запрос через модель снова ищет своё подключение — и внутри транзакции тоже — и
получает `ConfigurationError`, как до `Hare.init()`. Контекст забывает свои подключения, даже если закрытие не
удалось. Одиночная ошибка поднимается после; несколько ошибок сохраняются в `BaseExceptionGroup`.
Наблюдателям, которые ещё отправляют метрику или трассировку, даётся до 10 секунд.
`close_all()` у `ConnectionHandler` — то же закрытие для одного контекста — см.
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

Чтобы описать модель — её поля, связи, фильтры и сортировки каждого поля, — читайте `apps.get_binding(Model)`
и
[`lookup_info`](../querying/describing-filters.ru.md); аргументы конструктора поля даёт
`field.deconstruct()`.

> [!NOTE]
> Функция для тестов `hare_test_context(modules, db_url="sqlite+aiosqlite://:memory:", ...)` выполняет
> весь цикл — `init()`, создание таблиц, закрытие и удаление базы — как один асинхронный контекстный
> менеджер. Используйте её в своих фикстурах, а не повторяйте эти шаги вручную. Подробнее — в разделе
> [Тестирование](../testing/setup.ru.md), вместе с остальным `hare.contrib.test`.

## <a id="run-async"></a>Скрипты: `Hare.run_async()`

Для разового скрипта `Hare.run_async()` выполняет корутину через блокирующий портал AnyIO в отдельном
потоке с собственным циклом событий и закрывает подключения её контекста при завершении:

```python
from hare import Hare, HareConfig


async def main() -> None:
    await Hare.init(HareConfig.from_db_url("sqlite+aiosqlite://db.sqlite3", {"models": ["app.models"]}))
    ...


Hare.run_async(main())
```
