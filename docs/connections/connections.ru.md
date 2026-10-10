# Подключения

Что выбирают адрес и настройки подключения: драйвер PostgreSQL, как подключение переживает
перезапуск или переключение сервера, меняющиеся учётные данные, TLS и другие параметры адреса,
параметры SQLite и `Connections`, где лежат открытые подключения контекста.

## <a id="choosing-a-postgres-driver"></a>Выбор драйвера PostgreSQL

Подключение выбирает драйвер через `engine` — это то же слово, что схема в адресе базы:

| `engine` / схема адреса | Драйвер |
|---|---|
| `sqlite+aiosqlite` | SQLite через `aiosqlite` поверх стандартного модуля `sqlite3` |
| `postgresql` | PostgreSQL через драйвер hare на Rust |
| `postgresql+asyncpg` | PostgreSQL через `asyncpg` (нужен `hare-orm[asyncpg]`) |
| `clickhouse+clickhouse-connect` | ClickHouse через clickhouse-connect, по HTTP (нужен `hare-orm[clickhouse]`) |
| `clickhouse+clickhouse-driver` | ClickHouse через clickhouse-driver, по родному протоколу поверх TCP (нужен `hare-orm[clickhouse-driver]`) |

Схема драйвера — `<диалект>+<драйвер>`. Простая схема диалекта принадлежит собственному движку
hare для этого диалекта: `postgresql://` — драйвер hare на Rust; `sqlite://` и `clickhouse://`
не выбирают никакой драйвер.

```python
"reporting": {"engine": "postgresql+asyncpg", "credentials": {"host": "...", ...}},
"analytics": "postgresql://user:pass@host:5432/db",
```

Если модуля на Rust нет — стоит универсальный wheel на платформе, для которой готовый wheel не
собирается, или рабочая копия репозитория не собрана, — подключение `postgresql` даёт `ConfigurationError` с командой
`maturin develop`, которая его собирает; для `postgresql+asyncpg` он не нужен.

Драйвер на Rust работает со своими соединениями в собственных рабочих потоках — до четырёх, но не
больше, чем ядер у машины: запросы запускает один поток с циклом событий, и нескольких рабочих
потоков ему хватает — пока один разбирает большой результат, остальные обслуживают другие
соединения; лишние ждали бы работы на ядрах, которые нужны циклу событий и базе. Их число задаёт
собственная переменная окружения tokio до импорта hare:

```bash
TOKIO_WORKER_THREADS=4 python app.py
```

## <a id="connection-resilience"></a>Повторные попытки и лимиты подключения (только PostgreSQL)

Оба драйвера PostgreSQL принимают эти настройки как параметры адреса
(`?connect_max_retries=3&...`) или как ключи словаря `credentials` — так же, как передаётся
`max_size`. Число повторов — целое от `0` до `100`, начальная пауза — конечное число секунд от `0` до
`3600`; всё остальное (`"inf"`, `"nan"`, отрицательное число) даёт `ConfigurationError` ещё при
настройке:

| Параметр | По умолчанию | Что задаёт |
|---|---|---|
| `connect_max_retries` | `0` | Сколько раз повторить *первое* подключение пула; пауза перед каждой следующей попыткой вдвое длиннее предыдущей (`connect_retry_backoff_base_seconds * 2**номер_попытки`). При `0` ошибка первой попытки передаётся без изменений. |
| `connect_retry_backoff_base_seconds` | `0.1` | Начальная пауза для повторов выше. |
| `port` | `5432` | Целое число от `1` до `65535`, иначе `ConfigurationError`. |
| `statement_cache_size` | как у драйвера | Целое число от `0` до `1000000`. `0` выключает кэш: каждая команда готовится для одного своего выполнения — с `rust_pg` команда, у всех значений которой свой тип (UUID, дата, время, `datetime` с часовым поясом, `timedelta`, `Decimal`, `float`, `bytes`, `bool`, `None`), уходит вместе с ними за один сетевой круг. |
| `transaction_pooling` | `false` | Соединение идёт через пулер в режиме транзакций (PgBouncer), см. [Пулеры соединений](connection-poolers.ru.md). |
| `direct_host` / `direct_port` | нет / `port` | Адрес самого сервера в обход пулера — для LISTEN и настроек сессии. Только вместе с `transaction_pooling`. |
| `read_retry_max_retries` | `0` | Сколько раз повторить запрос, прерванный обрывом соединения **посреди запроса** (переключение сервера RDS на резервный, кратковременный сбой сети), а не только при подключении. Паузы растут так же, число повторов считается отдельно. Повторяется только запрос, который по своему виду лишь читает данные (`AwaitableQuery.is_read_only`), и только **вне** явной транзакции: запись вслепую не повторяется никогда (она могла уже выполниться), запрос внутри `Transactions.atomic()` — тоже (повторять пришлось бы всю транзакцию, а не одну команду). Повтор вызывает только `DBConnectionError`, но не `OperationalError`: например, после отмены команды соединение остаётся рабочим, и повтор ничего бы не дал. |
| `read_retry_backoff_base_seconds` | `0.1` | Начальная пауза для повторов выше. |
| `min_size` / `max_size` | `1` / `16` | Наименьшее и наибольшее число соединений в пуле: целые числа, `0 <= min_size <= max_size`, `1 <= max_size <= 10000`. Неверный тип или значение вне диапазона — `ConfigurationError`. |
| `pool_acquire_timeout` | нет (ждать сколько угодно) | Сколько секунд ждать свободного соединения из пула, прежде чем выдать `PoolTimeoutError` — это `DBConnectionError` (`0 < значение <= 86400`, иначе `ConfigurationError`). Значения «не ждать» нет: `0` отклоняется. Проверка и смысл одинаковы на обоих драйверах. |
| `command_timeout` | нет (выключено) | Наибольшее время в секундах, которое одна команда может ждать ответа базы (`0 < значение <= 86400`, иначе `ConfigurationError`). Без него запрос к серверу, который перестал отвечать, но не закрыл соединение (сеть разделилась, процесс завис), ждёт бесконечно. Когда время выходит, вызов бросает `TimeoutError`, а запрос отменяется. По умолчанию выключено, чтобы не прерывать долгие миграции и массовые операции. Время отсчитывает сам hare на обоих драйверах (собственный `command_timeout` у `asyncpg` не срабатывает, если сервер перестал отвечать, а у `rust_pg` такой настройки нет) — для каждого вызова, выполняющего запрос, и для массовой загрузки `COPY`, но не для `begin`/`commit`/`rollback`/`savepoint` и не для `stream()`. См. [Проверка связи и неотвечающий сервер](#health-checks). |

## <a id="rotating-credentials"></a>Меняющийся пароль (PostgreSQL)

Пароль, который меняется, — токен облачного IAM, динамический пароль хранилища секретов — берётся не
из настройки `password`, а из функции. Её задаёт `password_provider`: обычная или асинхронная
функция, возвращающая пароль, или её путь через точку в DB_URL или файле настроек:

```python
async def get_database_password() -> str:
    return await secrets.get("orders-db")

await Hare.init(
    config={
        "connections": {
            "default": {
                "engine": "postgresql",
                "credentials": {
                    "host": "db.internal",
                    "user": "orders",
                    "database": "orders",
                    "password_provider": get_database_password,
                    "password_refresh_seconds": 300,
                },
            }
        },
        ...
    }
)
# или: postgresql://orders@db.internal/orders?password_provider=myapp.secrets.get_database_password
```

| Параметр | По умолчанию | Что задаёт |
|---|---|---|
| `password_provider` | нет | Функция, дающая пароль. Вместе с `password` — `ConfigurationError`. Она должна вернуть непустую строку, иначе `ConfigurationError`. |
| `password_refresh_seconds` | `600` | Сколько секунд пароль используется, прежде чем функцию спросят снова (`0 < значение <= 86400`, иначе `ConfigurationError`). Без `password_provider` — `ConfigurationError`. |

Пул открывает новые соединения с паролем, который функция дала последним; уже открытые соединения
остаются. `asyncpg` спрашивает пароль, открывая соединение, — когда тот старше
`password_refresh_seconds`; `rust_pg` открывает соединения сам, поэтому hare спрашивает в фоне каждые
`password_refresh_seconds` и передаёт пулу новый пароль. Если сервер отказал в пароле — его сменили до
обновления, — функцию спрашивают сразу, а команда выполняется ещё раз: отказ во входе случился до
того, как команда дошла до сервера, поэтому это верно и для записи. Внутри транзакции её соединение
уже открыто, и ничего не повторяется. Диалект без смены пароля (SQLite, ClickHouse) отвергает
`password_provider` как неизвестную настройку.

## <a id="url-parameters-and-tls"></a>Параметры адреса и TLS (PostgreSQL)

Шифрование соединения (TLS) задаётся одним из параметров: `?sslmode=` (значения libpq `disable`,
`allow`, `prefer`, `require`, `verify-ca`, `verify-full`), `?ssl=` (те же значения или `true`/`false`:
`true` — это `require`, `false` — `disable`) или `?ssl_mode=` — в одном адресе только один из них.
Значение передаётся в настройку конкретного драйвера: `ssl_mode` у драйвера на Rust (режима `allow`
у него нет — `UnSupportedError`), `ssl` у `asyncpg`. Любое другое значение даёт `ConfigurationError`.

С драйвером на Rust (`postgresql://`) каждый параметр адреса должен быть понятен драйверу:
`min_size`/`max_size`, `statement_cache_size`, `ssl_root_cert`,
`application_name`, `schema`, `tenant_schema_template`, `tenant_row_level_security`, `host`, `port`, параметры TLS,
`password_provider`/`password_refresh_seconds` и параметры из таблицы выше. Любой другой (например,
`max_queries` из `asyncpg`) даёт `ConfigurationError`, а не отбрасывается молча. С `asyncpg`
(`postgresql+asyncpg://`) параметр должен быть одним из тех, что принимают
`asyncpg.connect()`/`create_pool()`, а числовые параметры `asyncpg` проверяются по диапазону:
`max_queries` — от `1`, `timeout` — больше `0`, `max_inactive_connection_lifetime` и
`max_cached_statement_lifetime` — от `0` до `86400` секунд, `max_cacheable_statement_size` — от `0`.
Значение неподходящего типа или вне диапазона (`?min_size=abc`, `?max_queries=0`) даёт
`ConfigurationError` при настройке подключения, а не на первом запросе. Значение, заданное дважды —
в самом адресе и параметром (`...@host:5432/db?port=6432`), — тоже даёт `ConfigurationError`, а не
тихо теряет одно из двух.

Если подключиться не удалось, в сообщении остаётся причина, которую назвал драйвер. Неверный
пароль, неизвестная роль или несуществующая база дают `ConfigurationError` на обоих драйверах:
повтор тут не поможет, и `connect_max_retries` их не повторяет. Недоступный сервер даёт
`DBConnectionError`. Если `user`, `password`, `host` или `database` не заданы, оба драйвера берут их
из переменных окружения libpq `PGUSER`, `PGPASSWORD`, `PGHOST`, `PGDATABASE` (а затем — имя
пользователя ОС и `localhost`).

Оба драйвера PostgreSQL работают в каждом сеансе с `TimeZone=UTC`, какой бы часовой пояс ни был
задан по умолчанию у сервера, базы или роли: от пояса сеанса зависят приведения даты и времени,
`CURRENT_DATE` и значения по умолчанию `Now()`, а hare хранит моменты времени в UTC. Если в
`server_settings` указан другой `TimeZone` (`UTC`, `Etc/UTC`, `GMT` и подобные допускаются), будет
`ConfigurationError`. Пояс, в котором значения показываются приложению, задаёт
`Hare.init(timezone=...)`.

Оба драйвера PostgreSQL возвращают из написанного вручную SQL одинаковые значения Python: `numeric`
— точный `Decimal` (с любым числом цифр), `json`/`jsonb` — их текст, `interval` — `timedelta` (месяц
считается за 30 дней, год — за 365), `inet` — `ipaddress.ip_address`/`ip_interface`, `cidr` —
`ipaddress.ip_network`, `oid`/`xid` — `int`, типы `reg*` — имена, `ROW(...)` — кортеж. Время
`24:00:00`, которое не помещается в `datetime.time`, вызывает ошибку, а не читается как полночь.
Значение типа, который драйвер на Rust не умеет разбирать (`point`, `pg_lsn` и другие), он
возвращает текстом — так, как его печатает PostgreSQL; в параметрах он принимает `timedelta` и
объекты `ipaddress`. Для остальных типов результат написанного вручную SQL зависит от драйвера:
диапазон — это `Range` из `asyncpg` или `hare.dialects.postgresql.fields.Range`, `money` —
текст у `asyncpg` (`'$1.00'`) или `Decimal` у драйвера на Rust, бесконечный `timestamptz` —
`datetime.max`/`min` без пояса или в UTC. Поля моделей (`RangeField`, `DatetimeField` и другие)
читают всё это одинаково на обоих драйверах. Параметр, тип которого не подходит колонке, даёт
`OperationalError`, а не преобразуется. Ошибка базы сохраняет в сообщении `DETAIL` и `HINT` сервера,
а исключение драйвера, из которого она получена, — `sqlstate`, `constraint_name`, `table_name` и
другие поля.

Пароль может содержать `@`, `/`, `?`, `#`, `[` и `]` как есть (кодирование через `%` тоже
работает), значение параметра может содержать `@`, закодированное через `%` имя базы
раскодируется (`/my%20db` → `my db`). Каталог unix-сокета можно указать как
`?host=/var/run/postgresql` с пустым именем хоста или закодировать через `%` прямо на месте хоста
(`postgresql://user@%2Fvar%2Frun%2Fpostgresql/db`). Идентификатор зоны адреса IPv6 тоже кодируется
через `%` (`[fe80::1%25eth0]` → `fe80::1%eth0`).

## <a id="sqlite-connection-parameters"></a>Параметры подключения SQLite

Путь к файлу SQLite в `db_url` раскодируется так же, как имя базы PostgreSQL
(`sqlite+aiosqlite://my%20db.sqlite` → `my db.sqlite`). Каждый остальной параметр адреса или ключ
`credentials` задаёт PRAGMA соединения. Принимаются только перечисленные ниже, и каждое значение
проверяется до отправки в базу (PRAGMA не принимает значения через параметры запроса):

| Тип | PRAGMA | Значения |
|---|---|---|
| ключевое слово | `journal_mode`, `synchronous`, `temp_store`, `locking_mode`, `auto_vacuum`, `secure_delete` | слова, которые документация SQLite перечисляет для каждой из них (`journal_mode`: `DELETE`/`TRUNCATE`/`PERSIST`/`MEMORY`/`WAL`/`OFF`), регистр не важен |
| да/нет | `foreign_keys`, `case_sensitive_like`, `recursive_triggers`, `automatic_index`, `cell_size_check`, `defer_foreign_keys`, `ignore_check_constraints`, `trusted_schema`, `reverse_unordered_selects` | `true`/`false`, `1`/`0`, `yes`/`no`, `on`/`off` |
| целое число | `journal_size_limit` (`-1`..`2**40`), `cache_size`, `busy_timeout` (`0`..`86400000` мс), `mmap_size`, `page_size` (степень двойки, `512`..`65536`), `wal_autocheckpoint`, `threads` (`0`..`64`), `max_page_count`, `cache_spill` | в пределах диапазона |

Любой другой ключ (опечатка вроде `jurnal_mode`) или недопустимое значение дают
`ConfigurationError`; то же — для значения `install_regexp_functions`, которое не является булевым, и
для включённого `automatic_index` на SQLite с 3.38.0 по 3.41.0, чьи автоматические индексы не
учитывают правило сортировки сравнения (на этих версиях он по умолчанию выключен — см.
[ошибки библиотек SQLite](../dialects/dialects-and-features.ru.md#sqlite-library-faults)). Подключение
без `file_path` тоже даёт `ConfigurationError`.

`timezone` проверяется в `Hare.init()`: неизвестное имя часового пояса сразу даёт
`ConfigurationError`, а не ошибку на первом значении даты и времени.

## <a id="connections"></a>`Connections` / `ConnectionHandler`

```python
Connections.get(connection_alias: str) -> DatabaseClient
Connections.get_client(using: str | DatabaseClient | None) -> DatabaseClient | None
Connections.current() -> ConnectionHandler
Connections.aliases() -> list[str]
Connections.get_pool_statuses(connection_alias: str | None = None) -> list[PoolStatus]
Connections.create_task_outside_transactions(coroutine) -> asyncio.Task
await Connections.reconnect() -> None
```

`ConnectionHandler` (доступен как `context.connections` у `HareContext`) — настоящий владелец подключений
контекста:

```python
.get(connection_alias) -> DatabaseClient           # создаётся при первом обращении
.create_independent(connection_alias, credential_overrides=None) -> DatabaseClient   # его использует autonomous()
.all() -> list[DatabaseClient]
.aliases() -> list[str]
async def close_all(discard: bool = True) -> None
```

`get_client(using)` — подключение, которое называет аргумент `using=`: клиент по имени, сам клиент или
`None`. `get_pool_statuses()` перечисляет открытые пулы ([Метрики пула и здоровье](../observability/pool-health.ru.md)).
`create_task_outside_transactions(coroutine)` запускает задачу, которая не видит транзакций вызывающего
кода: каждое имя подключения даёт там общий клиент.

`aliases()` возвращает имена всех подключений, настроенных для текущего контекста (из настроек
подключений `Hare.init()`), — не клиентов, а только имена, например чтобы перебрать все подключения
при проверке работоспособности.

`hare.core.constants.DEFAULT_CONNECTION_NAME` (`"default"`) — имя, которое `HareConfig.from_db_url(...)` даёт
своему единственному подключению, подключение приложения без `default_connection` и подключение, на
котором `Transactions.atomic()` без `using` открывает транзакцию, когда подключения
приложений по умолчанию различаются.

Чтобы узнать, на какое подключение идут запросы модели с учётом маршрутизаторов, используйте
[`Model.get_connection()`](../models/model-methods.ru.md#classmethods) или `QuerySet.get_connection()`.

### <a id="reconnect"></a>Переподключение после замены базы

```python
await Connections.reconnect()
```

Закрывает все подключения и пулы текущего контекста, сохраняя их настройки; следующий запрос
открывает новые подключения. Нужно, когда базу заменили под работающим приложением: файл SQLite
подменили копией, базу PostgreSQL восстановили из дампа или оборвали её подключения через
`pg_terminate_backend()`.

- Объекты подключений и их имена остаются прежними — код, который держит
  `Connections.get("default")`, продолжает работать.
- Вызывайте без открытой транзакции. Запрос, начатый до вызова, доработает на старом подключении.
- Каждый процесс переподключает свои подключения сам; пул рабочих процессов вызывает это в каждом.
- Без активного контекста даёт `ConfigurationError`.

### <a id="health-checks"></a>Проверка связи и неотвечающий сервер

```python
ok: bool = await Connections.get("default").ping(timeout=5.0)
```

Полная проверка здоровья — все соединения пингуются одновременно, их пулы оцениваются по критериям, есть маршрут готовности веб-приложения — это `HealthCheck` ([Метрики пула и здоровье](../observability/pool-health.ru.md)).

`ping()` выполняет `SELECT 1` и возвращает `True`/`False` вместо исключения. Он всегда ограничен своим
временем ожидания (по умолчанию 5 секунд, аргумент `timeout=`), независимо от настроек подключения:
сервер, который перестал отвечать, не закрыв соединение (сеть разделилась, процесс завис), не
заставляет ни один драйвер выдать ошибку, поэтому вызов без ограничения ждал бы вечно. Возвращает
`False` при `DBConnectionError`, `OperationalError` и по истечении времени; всё остальное передаётся
дальше. Работает одинаково на любой базе.

У обычных запросов по умолчанию такого ограничения нет — долгую миграцию или массовую операцию
прерывать нельзя. Чтобы включить его в PostgreSQL, задайте `command_timeout` (секунды) в
`credentials` или параметром адреса (`?command_timeout=30`); см.
[Повторные попытки и лимиты подключения](#connection-resilience).
Команда, превысившая его, отменяется и даёт `TimeoutError` (на обоих драйверах), а соединение остаётся
рабочим. hare отсчитывает это время сам для каждого вызова, выполняющего запрос (и для массового
`COPY`), а не через настройку драйвера: собственный `command_timeout` в `asyncpg` даёт ошибку только
после того, как вернётся ответ на запрос отмены, а от сервера, который перестал отвечать, его не будет
никогда; у `rust_pg` такой настройки нет вовсе. Команды управления транзакцией
(`begin`/`commit`/`rollback`/`savepoint`) намеренно не ограничены: отмена одной из них посреди
выполнения оставила бы состояние транзакции неизвестным.

В `rust_pg` команда транзакции, отменённая во время выполнения, отправляет PostgreSQL запрос отмены по
отдельному соединению; следующая команда транзакции ждёт, пока этот запрос будет доставлен, поэтому
запоздавший запрос отмены никогда не отменит вместо неё следующую команду.
