# Настройка и `Hare.init()`

## `Hare.init()` {: #hare-init }

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
    use_tz: bool = True,
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
await Hare.init(HareConfig.from_db_url("sqlite://db.sqlite3", {"models": ["my_app.models"]}))  # один адрес
```

`HareConfig.from_db_url(db_url, modules)` строит настройки одного подключения (`"default"`) из
адреса базы и `{app_label: [пути к модулям]}`. Источник, который нельзя прочитать, — отсутствующий
файл, несуществующий модуль или переменная, переменная с чем-то другим — даёт `ConfigurationError`.

Остальные аргументы тоже проверяются: `use_tz` должен быть `bool`, `table_name_generator` —
функцией, `slow_query_threshold_ms` — конечным числом от `0` до `86400000`. Общие для всего процесса
настройки (`slow_query_threshold_ms` и `table_name_generator`) меняются только после успешного
`init()`; если `init()` завершился ошибкой, они остаются прежними.

| Параметр | Что задаёт |
|---|---|
| `config` | Словарь, `HareConfig`, путь к файлу или `"module.VARIABLE"` — см. выше; формат описан ниже. |
| `connect` | `False` — настроить модели и подключения, не подключаясь ни к одной базе; имена подключений всё равно сверяются с настройками. Для инструментов, которым нужны модели и их SQL, но не база: создание миграций, вывод DDL. |
| `_create_db` | Сначала создать саму базу (удобно в тестах). |
| `use_tz` / `timezone` | Как `DatetimeField` работает с часовыми поясами. |
| `routers` | Список классов маршрутизаторов или путей к ним через точку — см. [Маршрутизаторы](multiple-databases.ru.md#routers). |
| `table_name_generator` | Функция `Callable[[type[Model]], str]`, которая заменяет правило именования таблиц. Сгенерированное имя вычисляется заново при каждом `init()`: следующий `init()` с другой функцией или без неё переименует таблицу модели. Явно указанная `Meta.table` всегда важнее. Имена, которые выводятся из имени таблицы при первой настройке связей, — автоматически созданная промежуточная таблица связи «многие-ко-многим», её колонки, `related_name` по умолчанию — сохраняют первое значение. |
| `slow_query_threshold_ms` | Запросы, которые выполнялись столько миллисекунд или дольше, записываются в журнал на уровне DEBUG как «Slow query». По умолчанию 500 мс — см. [Журнал медленных запросов](../observability/observers.ru.md#slow-query-logging). |
| `_enable_global_fallback` | Позволяет коду, который выполняется не в той задаче asyncio, что вызвала `init()` (например, в фоновой задаче, запущенной при старте ASGI-приложения), найти текущий контекст. Интеграции с Litestar, FastAPI и Robyn включают это сами. |

## Словарь `config` {: #the-config-dict }

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
    "use_tz": True,
    "timezone": "UTC",
    "swappable": {"USER_MODEL": "my_app.User"},  # необязательно, см. ниже
}

ctx = await Hare.init(config=config)
```

Тот же формат есть в виде классов, если настройки удобнее собирать в коде: `hare.core.config.HareConfig`
— `HareConfig(connections=..., apps=..., routers=None, use_tz=None, timezone=None, cli=None,
swappable=None)`, а также `ConnectionConfig(engine=None, credentials={}, db_url=None)`,
`DBUrlConfig(url)`, `AppConfig(models=[...], default_connection=None, migrations=None)` и
`CliConfig(commands=[...])` (секция `cli`). Подключение можно задать и
словарём `{"db_url": "..."}` — в такой вид превращается `ConnectionConfig(db_url=...)`. `models` —
список или кортеж путей к модулям; одна строка вместо списка даёт `ConfigurationError`, а не
разбирается по буквам.

Ключи словаря настроек — это поля тех же классов: на верхнем уровне поля `HareConfig`, в
подключении, заданном словарём, — поля `ConnectionConfig`, в приложении — `AppConfig`, в секции
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
указывает на модель, объявившую `Meta.swappable = "USER_MODEL"`.

## `HareContext` {: #hare-context }

```python
ctx = await Hare.init(HareConfig.from_db_url("sqlite://:memory:", {"models": ["my_app.models"]}))

# позже, при остановке приложения:
await ctx.close_connections()
```

`HareContext` можно использовать и в блоке `async with` — так обычно делают в тестах и скриптах:

```python
async with HareContext() as ctx:
    await ctx.init(HareConfig.from_db_url("sqlite://:memory:", {"models": ["my_app.models"]}))
    await ctx.generate_schemas()
    ...
```

`Hare` — это контекст текущей задачи под постоянным именем: `Hare.init()` настраивает текущий
`HareContext` задачи (новый, если у задачи его нет), а все остальные методы `Hare` работают с
текущим. Всё определено один раз, в `HareContext`: `.init(config, ...)`,
`.generate_schemas(safe=True)`, `.close_connections()`, `.connections` (`ConnectionHandler`),
`.apps`, `.router`, `.inited`, `.default_connection`, `.use_tz`, `.timezone`,
`.get_model(app_label, model_name)`, `.db(connection_name=None)`, `.swappable_label(setting)`,
`.get_swappable_model(setting)`, `.register_live_models(...)`, `.unregister_live_models(...)` и
`.observe(event_type, callback)`/`.unobserve(...)` (см.
[Наблюдение за ORM](../observability/observers.ru.md#where-observers-live)).
`HareContext.get_current()` возвращает текущий контекст или `None`.

## Запросы, построенные до `init()` {: #querysets-before-init }

QuerySet можно построить до `Hare.init()` — при импорте, как константу модуля или атрибут класса:

```python
PUBLISHED = Book.objects.filter(published=True).order_by("-rating")   # init() ещё не было


async def list_published() -> list[Book]:
    return await PUBLISHED.limit(20)          # после init(): обычный QuerySet
```

Пока модели не привязаны, цепочку нельзя проверить по ним (связи ещё не настроены), поэтому каждый
такой QuerySet запоминается, а `init()` повторяет его вызовы сразу после привязки моделей. С этого
момента это обычный, полностью построенный QuerySet — ничего ленивого в нём нет. Поэтому ошибка в
такой цепочке (неизвестное поле, неверный поиск) поднимается из `Hare.init()` (или
`Hare.bind_models()`) — та же, что дал бы этот вызов после `init()`. Выполнение запроса до `init()`
даёт `ConfigurationError`.

## Привязка моделей без базы {: #bind-models }

```python
Hare.bind_models(config, table_name_generator=None)   # синхронный
```

Привязывает модели из настроек — связи, заменяемые модели, фильтры и сортировки, — не подключаясь
и не оставляя текущего контекста. Это для кода, которому модели нужны до запуска приложения:
веб-фреймворку, который при импорте строит сигнатуры обработчиков по
[`lookup_info`](../querying/describing-filters.ru.md). Подключения потом, как обычно, настраивает
`Hare.init()`. `await Hare.init(config, connect=False)` — асинхронная форма, которая оставляет
текущий контекст: с моделями, маршрутизаторами и настройками подключений, но без единого соединения.

## Закрытие соединений {: #closing-connections }

```python
await Hare.close_connections()
```

## Другие методы класса `Hare` {: #other-hare-classmethods }

- `Hare.generate_schemas(safe=True)` — сразу создать таблицы всех зарегистрированных моделей, без
  миграций. Удобно для тестов и прототипов, но не для рабочей базы.
- `connection.get_schema_sql(safe)` — вернуть те же команды создания таблиц строкой, не выполняя
  их, — чтобы посмотреть, записать в журнал или сравнить.
- `Hare.is_inited()`, `Hare.apps` (модели текущего контекста; `None`, если его нет),
  `Hare.get_context()` (текущий `HareContext`; без него — `ConfigurationError`).
- `Hare.swappable_label(setting)` / `Hare.get_swappable_model(setting)` — см.
  [Заменяемые модели](../models/relations.ru.md#swappable-models).
- `Hare.register_live_models(models, app_label, connection_alias="default", managed=None)` /
  `Hare.unregister_live_models(models)` — см.
  [Регистрация модели во время работы](../models/runtime-models.ru.md).

Чтобы описать модель — её поля, связи, фильтры и сортировки каждого поля, — читайте `Model._meta`
и [`lookup_info`](../querying/describing-filters.ru.md); аргументы конструктора поля даёт
`field.deconstruct()`.

!!! note
    Функция для тестов `hare_test_context(modules, db_url="sqlite://:memory:", ...)` выполняет весь
    цикл — `init()`, создание таблиц, закрытие и удаление базы — как один асинхронный контекстный
    менеджер. Используйте её в своих фикстурах, а не повторяйте эти шаги вручную. Подробнее — в
    разделе [Тестирование](../testing.ru.md), вместе с остальным `hare.contrib.test`.

## Скрипты: `run_async()` {: #run-async }

Для разового скрипта (не долго работающего приложения и не теста) `run_async` вызывает
`asyncio.run` и сам закрывает соединения в конце:

```python
from hare import Hare, HareConfig, run_async


async def main() -> None:
    await Hare.init(HareConfig.from_db_url("sqlite://db.sqlite3", {"models": ["app.models"]}))
    ...


run_async(main())
```
