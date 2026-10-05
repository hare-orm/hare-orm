# Настройка и `Hare.init()`

Как приложение запускает hare и останавливает его: `Hare.init()` и словарь `config`, который он
принимает, `HareContext`, в котором работает процесс или тест, запросы, созданные до `init()`,
привязка моделей без базы, закрытие подключений и `Hare.run_async()` для скрипта.

## <a id="hare-init"></a>`Hare.init()`

Приложение на hare-orm начинается с вызова `Hare.init()`. Он регистрирует модели, настраивает
подключения к базам и возвращает `HareContext`, который нужно хранить, пока работает приложение.
Само соединение (или пул соединений) открывается при первом запросе, а не в `init()`.

```python
@classmethod
async def init(
    cls,
    config: Mapping[str, Any] | HareConfig | str,
    *,
    connect: bool = True,
    _create_db: bool = False,
    use_timezone: bool = True,
    timezone: str = DEFAULT_TIMEZONE,
    routers: list[str | type] | None = None,
    table_name_generator: Callable[[type[Model]], str] | None = None,
    slow_query_threshold_ms: float = SLOW_QUERY_THRESHOLD_MS,
    _enable_global_fallback: bool = False,
) -> HareContext
```

Где лежат настройки, говорит один аргумент — `config`, — и он принимает любую форму (читает его
`HareConfig.load(source)` — и для `Hare.init()`, и для CLI):

```python
await Hare.init({"connections": {...}, "apps": {...}})            # словарь — формат описан ниже
await Hare.init(HareConfig(connections=..., apps=...))            # то же типизированным объектом
await Hare.init("config/hare.yml")                                # файл .json/.yml/.yaml в том же формате
await Hare.init("settings.HARE_ORM")                              # module.VARIABLE со словарём или HareConfig
await Hare.init(HareConfig.from_db_url("sqlite+aiosqlite://db.sqlite3", {"models": ["my_app.models"]}))  # один адрес
```

`HareConfig.from_db_url(db_url, modules)` строит настройки одного подключения (`"default"`) из
адреса базы и `{app_label: [пути к модулям]}`. Источник, который нельзя прочитать, — отсутствующий
файл, несуществующий модуль или переменная, переменная с чем-то другим — даёт `ConfigurationError`.

Остальные аргументы тоже проверяются: `use_timezone` должен быть `bool`, `table_name_generator` —
функцией, `slow_query_threshold_ms` — конечным числом от `0` до `86400000`. Общие для всего процесса
настройки (`slow_query_threshold_ms` и `table_name_generator`) меняются только после успешного
`init()`; если `init()` завершился ошибкой, они остаются прежними.

| Параметр | Что задаёт |
|---|---|
| `config` | Словарь, `HareConfig`, путь к файлу или `"module.VARIABLE"` — см. выше; формат описан ниже. |
| `connect` | `False` — настроить модели и подключения, не подключаясь ни к одной базе; имена подключений всё равно сверяются с настройками. Для инструментов, которым нужны модели и их SQL, но не база: создание миграций, вывод DDL. |
| `_create_db` | Сначала создать саму базу (удобно в тестах). |
| `use_timezone` / `timezone` | Как `DatetimeField` работает с часовыми поясами. |
| `routers` | Список классов маршрутизаторов или путей к ним через точку — см. [Маршрутизаторы](multiple-databases.ru.md#routers). |
| `table_name_generator` | Функция `Callable[[type[Model]], str]`, которая заменяет правило именования таблиц. Сгенерированное имя вычисляется заново при каждом `init()`: следующий `init()` с другой функцией или без неё переименует таблицу модели. Явно указанная `Meta.table` всегда важнее. Имена, которые выводятся из имени таблицы при первой настройке связей, — автоматически созданная промежуточная таблица связи «многие-ко-многим», её колонки, `related_name` по умолчанию — сохраняют первое значение. |
| `slow_query_threshold_ms` | Запросы, которые выполнялись столько миллисекунд или дольше, записываются в журнал на уровне DEBUG как «Slow query». По умолчанию 500 мс — см. [Журнал медленных запросов](../observability/observers.ru.md#slow-query-logging). |
| `_enable_global_fallback` | Позволяет коду, который выполняется не в той задаче asyncio, что вызвала `init()` (например, в фоновой задаче, запущенной при старте ASGI-приложения), найти текущий контекст. Интеграции с Litestar, FastAPI и Robyn включают это сами. |

## <a id="the-config-dict"></a>Словарь `config`

```python
config = {
    "connections": {
        "default": "postgresql://postgres:qwerty123@localhost:5432/my_db",  # просто строка-адрес...
        "reporting": {  # ...или подробный словарь
            "credentials": {
                "host": "localhost", "port": "5432",
                "user": "postgres", "password": "qwerty123", "database": "reporting",
            },
        },
    },
    "apps": {
        "my_app": {
            "models": ["my_app.models"],
            "default_connection": "default",   # необязательно, по умолчанию "default"
            "migrations": "my_app.migrations",  # необязательно, нужно командам CLI
        },
    },
    "routers": ["my_app.routers.ReportingRouter"],
    "use_timezone": True,
    "timezone": "UTC",
    "swappable": {"USER_MODEL": "my_app.User"},  # необязательно, см. ниже
    "migrations": {"lock_timeout": 5},  # необязательно, см. ниже
}

context = await Hare.init(config=config)
```

Тот же формат есть в виде классов, если настройки удобнее собирать в коде: `hare.core.config.HareConfig`
— `HareConfig(connections=..., apps=..., routers=None, read_your_writes_seconds=None, use_timezone=None,
timezone=None, cli=None, swappable=None, migrations=None)` (`read_your_writes_seconds` — см.
[Чтение своих записей](multiple-databases.ru.md#read-your-writes)), а также `ConnectionConfig(engine=None, credentials={}, db_url=None)`,
`DBUrlConfig(url)`, `AppConfig(models=[...], default_connection=None, migrations=None)` и
`CliConfig(commands=[...])` (раздел `cli`). Подключение можно задать и
словарём `{"db_url": "..."}` — в такой вид превращается `ConnectionConfig(db_url=...)`. `models` —
список или кортеж путей к модулям; одна строка вместо списка даёт `ConfigurationError`, а не
разбирается по буквам.

`migrations` принимает `lock_timeout`: сколько секунд команда миграции может ждать блокировку, взятую
другим сеансом, прежде чем `migrate` завершится ошибкой, — см.
[Ожидание блокировок](../migrations/migrations.ru.md#lock-timeout); и `safety` — настройки проверки
миграций: `{"large_table_rows": 100000}` — с какого числа строк `checkmigrations` считает таблицу
большой, целое число от 0 до 10<sup>12</sup>, — см.
[Проверка миграций](../migrations/zero-downtime.ru.md#checking-migrations). Классы:
`MigrationsConfig(lock_timeout=None, safety=None)` и `MigrationSafetyConfig(large_table_rows=100000)`.

Ключи словаря настроек — это поля тех же классов: на верхнем уровне поля `HareConfig`, в
подключении, заданном словарём, — поля `ConnectionConfig`, в приложении — `AppConfig`, в разделе
`cli` — `CliConfig`. Любой другой ключ даёт `ConfigurationError` с ближайшим известным именем:
иначе опечатка вроде `default_conection` молча оставила бы модели приложения на подключении по
умолчанию.

`repr()` этих объектов (и настройки подключений, которые hare-orm пишет в журнал на уровне DEBUG
при `init()`) заменяет каждое секретное значение на `***`: пароль в адресе базы, секретный параметр
адреса (`sslpassword=...`) и каждый параметр `credentials`, в имени которого есть `password`,
`passwd`, `pwd`, `secret` или `token`. Никакая часть секрета не показывается.

Неизвестный `engine` даёт `ConfigurationError`, как и параметр подключения, которого не знает
драйвер: опечатку в `credentials` (`comand_timeout`) отклоняют оба драйвера PostgreSQL. Драйвер на
Rust принимает только свои параметры, `asyncpg` — только то, что принимают
`asyncpg.connect()`/`create_pool()`.

Связь с `db_constraint=True` (так по умолчанию) между моделями, приложения которых работают через
разные подключения, даёт `ConfigurationError` при `init()`: внешний ключ базы не может ссылаться на
таблицу в другой базе. Объявите такую связь с `db_constraint=False` — тогда её `on_delete`
выполняет сам hare-orm.

`swappable` сопоставляет имени настройки модель в виде `"app_label.ModelName"` — ту, на которую
указывают связи, объявленные через `swappable("USER_MODEL")`. Так проект заменяет модель,
поставляемую пакетом, своей (см. [Заменяемые модели](../models/relations.ru.md#swappable-models)).
Имя настройки пишется заглавными буквами (`USER_MODEL`), значение называет одно из настроенных
приложений. Кроме того, `init()` проверяет, что модель зарегистрирована, не абстрактная и сама не
заменена другой моделью через свою настройку. Каждая из этих ошибок даёт свой `ConfigurationError`.
`Hare.swappable_label("USER_MODEL")` возвращает метку модели, на которую указывает настройка, а
`Hare.get_swappable_model("USER_MODEL")` — саму модель. Если настройки нет в конфигурации, она
указывает на модель, объявившую `Meta.swappable = "USER_MODEL"`. Значением настройки может быть и
словарь «имя ветки → метка» — `{"COMMENT_TARGETS": {"post": "blog.Post", "photo": "media.Photo"}}`,
цели [`GenericForeignKeyField(swappable("COMMENT_TARGETS"))`](../models/relations.ru.md#generic-foreign-key-swappable);
каждое имя ветки — идентификатор Python, каждая метка проверяется так же, как выше. Такая настройка
должна быть задана в конфигурации, и принимает её только обобщённая связь.

## <a id="hare-context"></a>`HareContext`

```python
context = await Hare.init(HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": ["my_app.models"]}))

# позже, при остановке приложения:
await context.close_connections()
```

`HareContext` можно использовать и в блоке `async with` — так обычно делают в тестах и скриптах:

```python
async with HareContext() as context:
    await context.init(HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": ["my_app.models"]}))
    await context.generate_schemas()
    ...
```

`Hare` — это контекст текущей задачи под постоянным именем: `Hare.init()` настраивает текущий
`HareContext` задачи (новый, если у задачи его нет), а все остальные методы `Hare` работают с
текущим. Всё определено один раз, в `HareContext`: `.init(config, ...)`,
`.generate_schemas(safe=True)`, `.close_connections()`, `.connections` (`ConnectionHandler`),
`.apps`, `.router`, `.routers`, `.inited`, `.default_connection`, `.use_timezone`, `.timezone`,
`.get_model(app_label, model_name)`, `.get_connection(connection_alias=None)`, `.bind_models(config, ...)`, `.swappable_label(setting)`,
`.get_swappable_model(setting)`, `.register_live_models(...)`, `.unregister_live_models(...)` и
`.observe(event_type, callback)`/`.unobserve(...)` (см.
[Наблюдение за ORM](../observability/observers.ru.md#where-observers-live)).
`HareContext.get_current()` возвращает текущий контекст или `None`, `HareContext.require_current()` —
текущий контекст или `ConfigurationError`.

Классы моделей общие для всех контекстов, загрузивших их модуль: контекст, настроенный внутри
другого, привязывает их (соединение, имена таблиц, связи) к своей конфигурации, а выход из него —
`with` или `async with` — привязывает их обратно к контексту, который снова стал текущим.

## <a id="querysets-before-init"></a>Запросы, построенные до `init()`

QuerySet можно построить до `Hare.init()` — при импорте, как константу модуля или атрибут класса:

```python
PUBLISHED = Book.objects.filter(published=True).order_by("-rating")   # init() ещё не было


async def list_published() -> list[Book]:
    return await PUBLISHED.limit(20)          # после init(): обычный QuerySet
```

Пока модели не привязаны, цепочку нельзя проверить по ним (связи ещё не настроены), поэтому каждый
такой QuerySet запоминается, а `init()` повторяет его вызовы сразу после привязки моделей. С этого
момента это обычный, полностью построенный QuerySet — ничего ленивого в нём нет. Поэтому ошибка в
такой цепочке (неизвестное поле, неверный оператор фильтра) поднимается из `Hare.init()` (или
`Hare.bind_models()`) — та же, что дал бы этот вызов после `init()`. Выполнение запроса до `init()`
даёт `ConfigurationError`.

## <a id="bind-models"></a>Привязка моделей без базы

```python
Hare.bind_models(config, table_name_generator=None)   # синхронный
```

Привязывает модели из настроек — связи, заменяемые модели, фильтры и сортировки, — не подключаясь
и не оставляя текущего контекста. Это для кода, которому модели нужны до запуска приложения:
веб-фреймворку, который при импорте строит сигнатуры обработчиков по
[`lookup_info`](../querying/describing-filters.ru.md). Подключения потом, как обычно, настраивает
`Hare.init()`. `await Hare.init(config, connect=False)` — асинхронная форма, которая оставляет
текущий контекст: с моделями, маршрутизаторами и настройками подключений, но без единого соединения.

## <a id="closing-connections"></a>Закрытие подключений

```python
await Hare.close_connections()
```

## <a id="other-hare-classmethods"></a>Другие методы класса `Hare`

- `Hare.generate_schemas(safe=True)` — сразу создать таблицы всех зарегистрированных моделей, без
  миграций. Удобно для тестов и прототипов, но не для рабочей базы.
- `connection.get_schema_sql(safe)` — вернуть те же команды создания таблиц строкой, не выполняя
  их, — чтобы посмотреть, записать в журнал или сравнить.
- `Hare.is_inited()`, `Hare.apps` (модели текущего контекста; `None`, если его нет),
  `Hare.get_context()` (текущий `HareContext`; без него — `ConfigurationError`).
- `Hare.swappable_label(setting)` / `Hare.get_swappable_model(setting)` — см.
  [Заменяемые модели](../models/relations.ru.md#swappable-models).
- `Hare.register_live_models(models, app_label, connection_alias="default", *, managed=None)` /
  `Hare.unregister_live_models(models)` — см.
  [Регистрация модели во время работы](../models/runtime-models.ru.md).

Чтобы описать модель — её поля, связи, фильтры и сортировки каждого поля, — читайте `Model._meta`
и [`lookup_info`](../querying/describing-filters.ru.md); аргументы конструктора поля даёт
`field.deconstruct()`.

> [!NOTE]
> Функция для тестов `hare_test_context(modules, db_url="sqlite+aiosqlite://:memory:", ...)` выполняет весь
> цикл — `init()`, создание таблиц, закрытие и удаление базы — как один асинхронный контекстный
> менеджер. Используйте её в своих фикстурах, а не повторяйте эти шаги вручную. Подробнее — в
> разделе [Тестирование](../testing/setup.ru.md), вместе с остальным `hare.contrib.test`.

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
