# Как написать диалект

hare работает с базой через два объекта. **Диалект** — язык базы: синтаксис SQL, типы колонок,
команды создания и изменения схемы, системный каталог. **Драйвер** к ней подключается: схемы адреса,
учётные данные и класс клиента, который выполняет команды. У одного диалекта может быть несколько
драйверов — у PostgreSQL это `asyncpg` и `rust_pg`.

Новая база добавляется отдельным пакетом; в самом hare при этом ничего не меняется. Набор тестов hare
проверяется и на таком диалекте: `tests/dialects/columnar/` — диалект, собранный только из публичного
API (нумерованные места для параметров, имена в обратных кавычках, нет транзакций, внешних ключей и
ограничений уникальности, UUID хранится байтами, свой метод `QuerySet` и свои параметры хранения
таблицы), и весь набор тестов на нём проходит. Читайте его вместе с этим руководством.

Руководство проходит все части на примере Colstore — выдуманной базы, похожей на ClickHouse (собственный
диалект ClickHouse в hare, `hare/dialects/clickhouse/`, устроен так же): колоночной базы без транзакций,
внешних ключей и ограничений уникальности, с именованными местами для параметров, обратными кавычками,
таблицами, которым нужны движок и ключ сортировки, и собственными модификаторами запросов (`FINAL`,
`SAMPLE`).

## <a id="the-parts"></a>Из чего состоит диалект

Всё, что решает база, живёт в пакете её диалекта. Ядро hare не называет диалекты, не пишет собственный
SQL какой-либо базы и само не проверяет её возможности — оно спрашивает части диалекта и читает
`Features`. Объект `Dialect` только собирает части: каждая создаётся при первом обращении методом
`build_*()`, который диалект переопределяет, и хранит свой диалект в `dialect`.

| Часть | Базовый класс | Член `Dialect` | Что решает |
|---|---|---|---|
| Возможности | `hare.dialects.base.features.Features` | `features` | Каждое «умеет ли база» — один ответ, который ядро читает как `connection.features` (или `dialect.features`, когда подключения нет). |
| Имена и литералы | `hare.dialects.base.literals.sql_literals.SqlLiterals` | `literals` / `build_literals()` | Кавычки имён, запись значений в текст SQL. |
| Параметры | `hare.dialects.base.parameters.sql_parameters.SqlParameters` | `parameters` / `build_parameters()` | Места для параметров, приведения, которые нужны голому параметру, источники строк многострочного `INSERT`, типы колонок для COPY. |
| Выражения | `hare.dialects.base.renderers.term_renderers.TermRenderers` | `renderers` / `build_renderers()` | SQL функций и выражений, которые база записывает иначе. |
| Части команд | `hare.dialects.base.clauses.query_clauses.QueryClauses` | `clauses` / `build_clauses()` | `LIMIT`/`OFFSET`, блокировки строк, `RETURNING`, `ON CONFLICT`, `DISTINCT ON`, `UPDATE ... FROM`, `EXPLAIN`. |
| Транзакции | `hare.dialects.base.transactions.transaction_statements.TransactionStatements` | `transactions` / `build_transactions()` | Уровень изоляции транзакции и команды, которые настраивают её сразу после `BEGIN`. |
| Типы колонок | `hare.dialects.base.types.TypeRegistry` | `types` / `build_types()` | Как поля хранятся и преобразуются. |
| Операторы фильтров | `hare.dialects.base.lookups.filter_operators.FilterOperators` | `filter_operators` / `build_filter_operators()` | Чем выполняются операторы фильтров и какие из них диалект выполняет вообще. |
| Полнотекстовый поиск | `hare.dialects.base.search.text_search.TextSearch` | `text_search` / `build_text_search()` | SQL запросов, векторов, рангов и разметки [`hare.search`](../dialects/search-and-geodata/full-text-search.ru.md). Необязательно: без него каждое выражение даёт `UnSupportedError`. |
| Редактор схемы | `hare.dialects.base.schema.base_schema_editor.BaseSchemaEditor` | `schema_editor_class` / `build_schema_editor_class()` | Все команды схемы — и для `generate_schemas()`, и для миграций. |
| Чтение схемы | `hare.inspectdb.introspection.SchemaIntrospector` | `introspector_class` / `build_introspector_class()` | Читает существующую схему для `inspectdb` и `hare drift`. Необязательно. |
| Параметры хранения таблицы | `hare.ddl.table_options.TableOptions` | `table_options_class` / `build_table_options_class()` | Что принимает `CREATE TABLE` диалекта помимо колонок. Необязательно. |
| Двухфазная фиксация | `hare.dialects.base.transactions.two_phase_commit.TwoPhaseCommit` | `two_phase_commit` / `build_two_phase_commit()` | Команды `Transactions.distributed()`. Необязательно. |
| Правила проверки миграций | `hare.dialects.base.migration_safety.MigrationSafetyRules` | `migration_safety_rules` / `build_migration_safety_rules()` | Правила, которые применяют `makemigrations` и `checkmigrations`, и как считаются строки таблицы. |

Ещё два объекта принадлежат подключению, а не диалекту:

| Объект | Базовый класс | Что решает |
|---|---|---|
| Драйвер | `hare.dialects.base.connection.driver.Driver` | Имя (`engine` в настройках подключения), схемы адреса и учётные данные, класс клиента, какие ошибки можно повторить. |
| Клиент | `hare.dialects.base.client.DatabaseClient` | Одно подключение: выполнение команд, транзакции, его `Features`. |
| Класс запроса | `hare.sql.builder.Query` | Привязывает команды к диалекту: подкласс, который только задаёт `SQL_CONTEXT = dialect.sql_context`. |

Каждая базовая часть пишет SQL по стандарту ISO; там, где у стандарта нет формы для чего-либо, её хук
даёт `UnSupportedError` — команда отклоняется до отправки SQL. То, что несколько баз
пишут одинаково сверх стандарта, — тоже класс `hare.dialects.base`, названный по содержимому:
`LimitReturningConflictQueryClauses` (`LIMIT`/`OFFSET`, `RETURNING`, `ON CONFLICT`,
`UPDATE ... FROM`), от которого наследуются диалекты PostgreSQL и SQLite. Сторонний диалект такой
базы начинает с него, любой другой — с базовых частей.

## <a id="registering"></a>Регистрация

Модуль драйвера пакета регистрирует драйвер — а вместе с ним и диалект — при импорте:

```python
# hare_colstore/driver.py
from hare.dialects.dialect_registry import DialectRegistry

COLSTORE_DRIVER = ColstoreDriver()
DialectRegistry.register_driver(COLSTORE_DRIVER)
```

и называет этот модуль в группе точек входа `hare.dialects`, чтобы hare импортировал его при первом
поиске драйвера, которого нет среди встроенных, или при получении списка всех драйверов:

```toml
# pyproject.toml пакета hare-colstore
[project.entry-points."hare.dialects"]
colstore = "hare_colstore.driver"
```

После установки пакета адрес подключения `"colstore+colstore-client://..."` или настройки подключения с
`"engine": "colstore+colstore-client"` его используют. Второй драйвер или диалект под уже занятым именем или схемой
адреса даёт `ConfigurationError`. Регистрация диалекта один раз вызывает его `install()` — место, где
регистрируется то, что он добавляет к собственным классам hare (метод `QuerySet`, часть пути у поля
ядра).

## <a id="driver"></a>1. Подключение — драйвер

```python
class ColstoreDriver(Driver):
    name = "colstore+colstore-client"    # "engine" в настройках подключения
    dialect = COLSTORE_DIALECT
    url_schemes = ("colstore+colstore-client",)  # colstore+colstore-client://user:password@host:9000/database
    path_credential = "database"              # что заполняет путь адреса
    authority_credentials = {"hostname": "host", "port": "port", "username": "user", "password": "password"}
    default_credentials = {"port": 9000}
    connection_options = ConnectionOptions(   # hare.dialects.base.connection.connection_options
        ConnectionOption("compression", ConnectionOptionType.BOOLEAN),
        ConnectionOption("connect_timeout", ConnectionOptionType.SECONDS, positive=True, maximum=3600),
        ConnectionOption("max_block_size", ConnectionOptionType.WHOLE_NUMBER, minimum=1, maximum=1_000_000),
    )
    strict_query_parameters = True            # неизвестный ?параметр — ConfigurationError

    def get_client_class(self, credentials):
        return ColstoreClient

    def get_client_classes(self):
        return (ColstoreClient,)            # все классы клиентов, включая клиентов транзакций

    def is_retryable(self, error):
        return False                          # транзакций нет — повторять целиком нечего
```

| Член класса | Что задаёт |
|---|---|
| `url_schemes`, `path_credential`, `authority_credentials`, `default_credentials`, `url_has_userinfo` | Как адрес базы превращается в именованные аргументы клиента. |
| `connection_options`, `strict_query_parameters` | Настройки, которые принимает подключение; каждая объявляется один раз как `ConnectionOption(name, value_type, *, minimum, maximum, positive, power_of_two, choices)`, где тип значения — `ConnectionOptionType.WHOLE_NUMBER`, `SECONDS`, `BOOLEAN`, `CHOICE` или `TEXT`. Настройка проверяется одинаково, пришла ли она с типом из `credentials` словаря настроек или текстом из параметра адреса (`?connect_timeout=5`): клиент в своём `__init__` вызывает `options.read(settings)`, чтобы забрать известные настройки, проверенные по типу и диапазону, и `options.raise_for_unknown(settings, driver_name)`, чтобы отклонить настройку с опечаткой. Разбор вручную не нужен. |
| `get_client_class(credentials)` | Класс клиента для подключения — его можно выбрать по учётному параметру и убрать этот параметр (`install_regexp_functions` у SQLite). |
| `get_client_classes()` | Все классы клиентов, включая клиентов транзакций. |
| `is_retryable(error)` | Означает ли ошибка драйвера, что база прервала транзакцию из-за параллельной (ошибка сериализации, взаимная блокировка); для неё hare даёт `TransactionRetryError`. |
| `get_url_path(url)`, `get_testing_path(path, reuse_databases)` | Чтение пути из адреса и путь, к которому подключается запуск тестов (место `{}` из `HARE_TEST_DB`, заполненное новым именем). |

## <a id="client"></a>2. Выполнение команд — клиент

```python
class ColstoreClient(DatabaseClient):
    driver_name = "colstore+colstore-client"
    dialect = COLSTORE_DIALECT
    query_class = ColstoreQuery
    native_python_types = frozenset({str, int, float, bytes, datetime.datetime, datetime.date, uuid.UUID})
    # Что поддерживает база — у диалекта; клиент добавляет то, что умеет его драйвер.
    features = COLSTORE_DIALECT.features.replace(max_bind_parameters=100_000)
```

Клиент реализует `create_connection(with_db)`, `close()`, `db_create()`, `db_delete()`,
`acquire_connection()` (контекстный менеджер, выдающий сырое соединение) и методы выполнения
команд — весь контракт между hare и драйвером:

| Метод | Что выполняет |
|---|---|
| `execute(query, values=None, *, returns_rows=None, rows_by_position=False) -> StatementResult` | Одну команду. `StatementResult(row_count, rows, inserted_id=None, description=None)` (`hare.dialects.base.results`): строки, которые она вернула, — каждая читается по имени колонки, — и число строк, которые она изменила, для записи без `RETURNING`. `returns_rows` говорит, возвращает ли команда строки (`SELECT`, запись с `RETURNING`); при `None` драйвер определяет это по тексту SQL сам. `rows_by_position=True` передаёт читатель, берущий строки только по позиции, при `Features.supports_positional_rows`: драйвер может тогда отдать самые дешёвые строки, читаемые по позиции, — обычные кортежи — с DB-API `description` колонок, из которого читает `StatementResult.column_names` (без него — из имён первой строки). hare передаёт его для своих чтений и для команд `UPDATE`/`DELETE`, считающих свои строки. Через него идёт каждое чтение и каждая запись ORM — отдельного метода для вставки или для чтения словарями реализовывать не нужно (`execute_dicts()` построен на нём; задайте `row_to_dict`, если строки драйвера превращаются в словарь быстрее, чем через `dict(row)`). |
| `execute_many(query, values)` | Одну команду по разу на каждую строку параметров; ничего не возвращает. |
| `execute_script(query)` | Сценарий из нескольких команд, передаётся как есть. |
| `execute_described(query, values=None) -> DescribedResult` | Одну команду вместе с именами колонок (`columns`, `rows` кортежами, `row_count`) — чтобы показать любой результат таблицей. |
| `copy(table, columns, records, column_types)` | Массовую загрузку, если у базы есть такой протокол (`Features.supports_copy`). Наблюдателям она сообщается под командой, которую для неё даёт диалект, — `QueryClauses.get_bulk_load_statement_sql()`. |
| `_driver_stream_batches(query, values, chunk_size)` | Асинхронный генератор партий строк (списков) поверх курсора на стороне сервера, при `Features.supports_streaming`, — у клиента транзакции; `chunk_size` 0 — собственный размер партии драйвера. Публичные `stream_batches()` и `stream()` принадлежат базовому классу: они добавляют метки, оборачивают запрос и сообщают о нём. |

`values` команды — это `list` или его подкласс `QueryParameters`
(`hare.sql.terms.parameters.query_parameters`), если среди значений есть значение поля с `sensitive=True`:
его `repr()` показывает такие значения как `<hidden>`, поэтому клиент, который пишет параметры в лог
через `%s`, скрывает их без лишних действий. Драйверу, который не принимает подклассы списка,
передаётся `list(values)`.

`native_python_types` — набор типов Python, которые драйвер возвращает уже в нужном полю виде: для
поля такого типа чтение строк пропускает преобразование.

Каждый метод выполнения переводит собственные исключения драйвера в исключения hare —
`DBConnectionError`, `IntegrityError` и `OperationalError` или `TransactionRetryError` через
`_get_operational_error()`, который спрашивает `is_retryable()` драйвера, — и сообщает о вызове
[наблюдателям и обёрткам запросов](../observability/observers.ru.md). И то и другое делает один
декоратор, `DatabaseClient.translate_exceptions`, которым класс клиента диалекта оборачивает методы
выполнения своих драйверов (`@ColstoreClient.translate_exceptions`). Он добавляет активные метки
запроса (`DatabaseClient.get_tagged_query_arguments()`), выполняет вызов внутри обёрток запросов, если
они установлены (`Observers.run_wrapped(QueryCall(...), proceed)`), сообщает о нём наблюдателям, когда
он завершился, успешно или нет (`Observers.record_query()`), и выбрасывает вместо исключения драйвера
то, что вернёт `translate_driver_error(client, error, sql, parameters, args, is_query_executing)`
клиента, — единственный метод, который клиент диалекта переопределяет, чтобы перевести исключения своего
драйвера. Настройки, которые читает декоратор, — атрибуты класса клиента, читаемые один раз при
оборачивании метода: `rejected_sql_character` (символ, на котором команда отклоняется до отправки),
`checks_aborted_transactions` и `aborted_transaction_message` (команда в транзакции, которую база
прервала, отклоняется), `runs_statement_options` (`command_timeout`, `password_provider`, пулер
транзакций, повторы чтения). Декоратор работает на каждой команде, поэтому читает
`is_transaction_client` клиента (истинно у каждого `TransactionClient`), а не вызывает `isinstance()`.

Когда соединение открыто, клиент вызывает `_post_connect()`, который читает версию сервера из
`get_server_version()` — `(major, minor, ...)`, без наблюдения за запросами; значение по умолчанию
`None` пропускает проверку. Сервер старше `Dialect.minimum_server_version` отклоняется с
`UnSupportedError`, и соединение закрывается; `Dialect.get_server_version_features(version)` возвращает
значения `Features`, которые меняет версия (PostgreSQL до 15 выключает `supports_nulls_distinct` и
не перечисляет параметры `EXPLAIN`, которых у более старого сервера нет), и они применяются только к
этому подключению. Обёртка транзакции делит `features` своего подключения.

База с транзакциями реализует `_get_transaction_client()` — новый клиент подкласса
`TransactionClient` драйвера, который даёт примитивы драйвера (см.
[Транзакции и одновременная работа](#transactions)). Контекст транзакции вокруг него — общий,
hare: он забирает ресурсы клиента, начинает транзакцию и на выходе фиксирует или откатывает её.
Команды, выполняемые сразу после `BEGIN`, — уровень изоляции, «только чтение», ограничения времени
команды и ожидания блокировки — дают `TransactionStatements` диалекта; клиент, который сам прерывает
слишком долгую команду без команды диалекта, задаёт `enforces_statement_timeout_itself = True` (клиент
SQLite прерывает запрос).

### <a id="several-drivers"></a>Один диалект, несколько драйверов

Диалект, у которого есть или может появиться больше одного драйвера, делит клиент на две части —
так устроены диалекты самого hare:

```text
hare/dialects/sqlite/
    client/
        sqlite_client.py               # SqliteClient: общее для любого драйвера SQLite
        sqlite_transaction_client.py   # SqliteTransactionClient: общий жизненный цикл транзакции
        declarations.py                # SqliteDriverErrors: классы исключений драйвера
    driver.py                          # SqliteDriver: общая основа Driver
    drivers/
        aiosqlite/
            driver.py                  # AiosqliteDriver: имя, схемы DB_URL, регистрация
            client/
                aiosqlite_client.py    # AiosqliteClient(SqliteClient)
                aiosqlite_transaction_client.py
```

- **Общий клиент** содержит то, что не зависит от библиотеки, через которую идёт работа с базой:
  настройки соединения и их проверки, жизненный цикл транзакции (начало, фиксация, откат, точки
  сохранения, прерванные транзакции), перевод ошибок, состояние пула. Он не импортирует библиотеку
  драйвера. Где ему нужен факт о библиотеке — классы исключений, собственная версия библиотеки и её
  известные дефекты, — он читает атрибут класса, который задаёт клиент драйвера: клиенты SQLite задают
  `driver_errors = SqliteDriverErrors(...)`. Этот атрибут читается только внутри `except`, поэтому
  успешная команда за него ничего не платит.
- **Клиент драйвера** наследует общий клиент и реализует то, что делает библиотека:
  `create_connection()`, `acquire_connection()`, методы выполнения команд, курсоры и потоковое
  чтение, примитивы транзакции (`_driver_commit()`, `_driver_savepoint()`, ...),
  `get_server_version()` и `Features`, которые добавляет библиотека (`AiosqliteClient.features`
  добавляет предел числа параметров сборки `sqlite3`). Его клиент транзакции наследует общий клиент
  транзакции первым: `AiosqliteTransactionClient(SqliteTransactionClient, AiosqliteClient)`.
- **Драйвер** хранит одно имя — свою схему DB_URL `<диалект>+<драйвер>`: `sqlite+aiosqlite://`,
  `postgresql+asyncpg://`, `clickhouse+clickhouse-connect://`, `clickhouse+clickhouse-driver://`. Простая
  схема диалекта принадлежит
  собственному движку hare для этого диалекта — `postgresql://` (rust_pg); другие драйверы её не берут.
- **Пул.** Клиент, чей пул живёт в Python, считает в `PoolStatistics` hare. Драйвер, чей пул живёт
  вне Python, возвращает из `create_pool_statistics()` свой объект с теми же методами и сообщает
  занятость пула через `get_pool_occupancy()` (при `Features.supports_pool_status`).
- **Нативный драйвер.** Общие для всех нативных драйверов части `rust.native` не принадлежат ни одной
  базе: `rust.native.pool` (`PoolStatistics`, `set_pool_metrics_enabled()`, взятие соединения из пула
  deadpool со счётчиками) и мост к asyncio, через который завершаются его futures. Новый нативный
  драйвер для SQLite или ClickHouse использует их так же, как `rust.native.pg`, и становится ещё
  одним драйвером диалекта — общий клиент остаётся прежним.

### <a id="password-provider"></a>Меняющийся пароль

Клиент базы, пароли которой меняются, поддерживает `password_provider` так же, как клиент PostgreSQL
([Меняющийся пароль](../connections/connections.ru.md#rotating-credentials)):

- Его настройки включают `PASSWORD_PROVIDER_OPTIONS` (`hare.dialects.base.connection.constants`) —
  `password_provider` и `password_refresh_seconds`, проверяемые как любая настройка. Диалект без них
  отвергает обе как неизвестные.
- Клиент задаёт `self.password_provider = PasswordProvider.from_settings(settings, password)`
  (`hare.dialects.base.client.password_provider`) — None без функции, `ConfigurationError`, если
  рядом с функцией указан постоянный пароль.
- Драйвер, который сам открывает каждое соединение, спрашивает там `await self.password_provider.get()`
  — сохранённый пароль, запрошенный заново, когда он старше срока обновления. Драйвер, чей пул
  открывает соединения сам, переопределяет `apply_password(password)`, чтобы передать пулу новый
  пароль, запускает `password_provider.start_refreshing(self.apply_password)` после открытия пула и
  останавливает его через `await password_provider.stop_refreshing()` при закрытии пула.
- Когда сервер отказывает в пароле, `await client.renew_password()` сразу спрашивает функцию и
  применяет ответ; повторить отвергнутую команду клиент может, только если знает, что она не дошла до
  сервера.

### <a id="shell-command"></a>Интерактивный клиент

`hare dbshell` запускает то, что возвращает `await client.get_shell_command()`, — `ShellCommand`
(`hare.dialects.base.client.declarations`): программу с аргументами и переменные, добавляемые в её
окружение. Пароль передаётся через окружение, никогда не в аргументах, которые видят другие
пользователи машины. Значение по умолчанию `None` заставляет `hare dbshell` отказать диалекту;
подключение, которое не может открыть другая программа (база в памяти), бросает `UnSupportedError`
с причиной.

### <a id="features"></a>Возможности

`Features` (`hare.dialects.base.features`) — единственное место, где hare спрашивает, что
поддерживают база и её драйвер. Диалект объявляет, что поддерживает база (`Dialect.features`), клиент
начинает с них и задаёт то, что добавляет его драйвер, а версия сервера меняет их для отдельного
подключения. Ядро читает `connection.features` там, где у него есть подключение, и `dialect.features`
там, где есть только диалект (команды схемы для файла миграции, список операторов фильтров).

| Возможность | Что делает hare, если её нет |
|---|---|
| `supports_transactions` | `Transactions.atomic()`/`atomic()` дают `UnSupportedError`; собственные записи hare из нескольких команд (каскад, `add()` у «многие-ко-многим», `bulk_create()` партиями) выполняют команды по одной. |
| `supports_savepoints` | Вложенный `atomic()` присоединяется к транзакции, в которую вложен; ошибка, покинувшая вложенный блок, заставляет транзакцию при завершении откатиться и дать `TransactionManagementError`, даже если внешний блок перехватил ошибку. |
| `supports_generated_keys` | Модель, чей первичный ключ генерирует база (по умолчанию `IntField(primary_key=True)`), отклоняется с `ConfigurationError` при привязке к соединению — дайте ей ключ, который задаёт приложение (`UUIDField(primary_key=True, default=uuid.uuid7)`). Журнал миграций самого hare использует ключ `(app, name)`. |
| `takes_keys_before_insert` | Включите, если ключи берутся из серии до записи строк: hare вызывает `take_generated_keys(model, count)` клиента для строк без ключа и пишет их с ключами; `synchronize_key_series(model)` сдвигает серию за наибольший ключ таблицы (`SynchronizeKeySeries`). |
| `checks_constraints_before_write` | Включите, если база не держит ни ограничений уникальности, ни внешних ключей: перед `create()`, `save()`, `bulk_create()`, `update()` и `bulk_update()` hare проверяет объявленные уникальность и связи модели одним `SELECT` на пачку и даёт `IntegrityError`; ключ таблицы, опции которой говорят `keeps_row_versions()`, не проверяется. Вставка-или-обновление без `ON CONFLICT` сначала читает конфликтующие ключи и по ним вставляет или обновляет. |
| `returns_rows_by_reading` | Включите, если нет `RETURNING`: `update().returning()` читает ключи до записи и строки после неё, `delete().returning()` — строки до неё, `bulk_create(returning=True)` — строки после вставки, всё по ключам; `returning(old=...)` по-прежнему отклоняется. |
| `supports_row_updates` | Каждый `UPDATE` — `save()` сохранённой строки, `QuerySet.update()`, `bulk_update()`, `SET_NULL`/`SET_DEFAULT` каскада — даёт `UnSupportedError` до отправки, а каскад, которому он нужен, ничего не пишет. Модель с мягким удалением (`Meta.soft_delete_field`) отклоняется с `ConfigurationError` при привязке; релей outbox отказывается запускаться. |
| `supports_row_deletes` | Каждый `DELETE` даёт `UnSupportedError` до отправки, а каскад, которому он нужен, ничего не пишет. |
| `rewrites_correlated_exists` | Включите, если база выполняет связанные подзапросы, но считает связанный `EXISTS` неверно: такой `EXISTS` остаётся в форме `IN`. |
| `supports_ordered_correlated_subqueries` | Связанный подзапрос, который сам сортирует или срезает свои строки, даёт `UnSupportedError` до отправки команды. |
| `orders_by_correlated_subqueries` | Запрос со связанным подзапросом в `ORDER BY` или выбранным рядом с `WHERE` выбирает строки в производной таблице — условия и ключи сортировки становятся её скрытыми колонками — и фильтрует, сортирует и срезает их снаружи; `UPDATE`/`DELETE` со связанным подзапросом даёт `UnSupportedError`. |
| `supports_correlated_subqueries` | `EXISTS`, связанный с внешним запросом равенствами колонок, — фильтр или исключение через связь «ко многим», `<m2m>__isnull`, `Exists(...)` с равенствами `OuterReference`, — пишется как `(внешние колонки) IN (SELECT внутренние колонки ...)` с исключёнными NULL: он истинен и ложен ровно там же, где `EXISTS`. Любой другой связанный подзапрос (`Subquery(...)` с `OuterReference`, `OuterReference`, сравниваемый не через `=`) даёт `UnSupportedError` до отправки команды; так же отказывается запуск релея outbox. |
| `can_rollback_ddl` | Миграция не оборачивается в транзакцию. |
| `supports_select_for_update` | `select_for_update()` даёт `UnSupportedError` при выполнении запроса; `update_or_create()` пропускает блокировку. |
| `locks_rows_by_key` | Включите, если строки блокируются вне SQL: `select_for_update()` читает ключи своих строк в транзакции, передаёт их имена (`<таблица>/<ключ>`, отсортированные) в `take_row_locks(lock_names, wait=...)` клиента транзакции, который возвращает `RowLockOutcome` (`taken`, `busy`, `waited`), и читает строки по ключам; дождавшаяся строка читается ещё раз вне транзакции, а изменённая даёт `TransactionRetryError`. Команда не несёт выражения блокировки. |
| `supports_select_for_no_key_update` | `select_for_update(no_key=True)` берёт обычную блокировку `FOR UPDATE`. |
| `supports_select_for_share` / `supports_select_for_key_share` | `select_for_update(share=True)` / `(key_share=True)` дают `UnSupportedError` при выполнении запроса. |
| `supports_update_limit_order_by` | `QuerySet.update()`/`delete()` запроса со срезом выбирают строки через подзапрос `pk IN (SELECT ...)`. |
| `supports_returning` | `INSERT` ничего не запрашивает обратно: ключ, который генерирует база, в объект не читается (давайте таким моделям ключ, который задаёт приложение), а значения колонок с `db_default` читаются `SELECT` по первичному ключу. `UPDATE` не читает изменённые вычисляемые колонки, а массовая запись не знает, какие строки она действительно вставила. |
| `guarantees_returning_order` | Многострочный `INSERT ... RETURNING` сопоставляется со своими объектами по первичному ключу, а не по позиции. |
| `supports_posix_regex` | Фильтр с `posix_regex`/`iposix_regex` даёт `UnSupportedError` до выполнения запроса. |
| `supports_two_phase_commit` | `Transactions.distributed()` отклоняет подключение. |
| `supports_listen_notify` | Очередь исходящих событий не отправляет `NOTIFY` о новом событии. |
| `supports_streaming` | `QuerySet.stream()` не поддерживается. |
| `streams_without_transaction` | `stream()` вне транзакции даёт `QueryError`; включите, если `stream_batches()` клиента читает поток на собственном соединении. |
| `supports_copy` | `bulk_create(use_copy=True)` даёт `UnSupportedError`. |
| `copies_bulk_inserts` | `bulk_create()` загружает строки через `copy()` только с `use_copy=True`. Задайте его там, где `copy()` — обычный для базы способ записать много строк: тогда `bulk_create()` выбирает его всегда, когда не обрабатывает конфликты и не читает строки обратно. |
| `inline_comments` | Комментарии таблиц и колонок записываются в `CREATE TABLE`, а не через `COMMENT ON`. |
| `supports_positional_rows` | Строки читаются только по имени колонки — без `execute(rows_by_position=True)`, без нативного чтения строк и без выполнения QuerySet по плану его вызовов. |
| `execute_many_scales_poorly` | Массовые записи отправляются командами на много строк, а не через `executemany()`. |
| `binds_written_parameters` | Строки, записываемые встроенным записывающим кодом, передаются объектами Python. |
| `binds_array_parameters` | Список, переданный одним параметром (параметр `RawSQL` вроде `= ANY(%s)`), даёт `UnSupportedError`. |
| `max_bind_parameters` | Массовые записи, предзагрузка и каскады делят свои команды, чтобы не превысить его. |
| `supports_nulls_distinct` | `UniqueConstraint(nulls_distinct=...)` даёт `UnSupportedError` до отправки команды создания. |
| `supports_unhex` | Длинный `__in` по байтам передаёт по параметру на значение, а не один массив JSON. |
| `supports_drop_column` | Редактор схемы, который пересобирает таблицы (у SQLite), пересобирает её при каждом удалении поля. |
| `explain_options` | Параметр `EXPLAIN`, которого нет в списке, даёт `UnSupportedError` до отправки команды. |
| `supports_schemas` | Таблица модели со схемой используется без имени схемы. |
| `supports_distinct_on` | `distinct(*fields)` выбирает первую строку каждого сочетания через `ROW_NUMBER()` в подзапросе `pk IN`. |
| `supports_grouping_sets` | `group_by(Rollup/Cube/GroupingSets(...))` и `Grouping()` дают `UnSupportedError`. |
| `supports_lateral` | `Lateral(...)` даёт `UnSupportedError`. |
| `supports_table_sample` | `QuerySet.sample(...)` даёт `UnSupportedError`. |
| `supports_asof_join` | `AsofJoin(...)` даёт `UnSupportedError`. |
| `supports_array_join` | `ArrayJoin(...)` даёт `UnSupportedError`. |
| `supports_lightweight_update`, `rebuilds_projections`, `supports_json_type`, `supports_variant_types`, `supports_refreshable_materialized_views` | Собственные признаки ClickHouse по версии сервера (`get_server_version_features()`): лёгкий `UPDATE`, проекции, которые лёгкий `DELETE` оставляет верными, тип `JSON`, типы `Variant` и `Dynamic`, материализованные представления, обновляемые по расписанию. |
| `supports_dictionaries` | `Meta.dictionaries` даёт `UnSupportedError` до DDL; иначе — `dictionaries_class` редактора схемы (`Dictionaries`: `get_dictionary_create_sqls()`, `drop_dictionary()`, `alter_dictionary()`, `rename_dictionary()`, `reload_dictionary()`). |
| `supports_merge` | `QuerySet.merge()` даёт `UnSupportedError`. |
| `supports_merge_returning` | `merge().returning()` даёт `UnSupportedError`. |
| `supports_merge_not_matched_by_source` | `merge().when_not_matched_by_source()` даёт `UnSupportedError`. |
| `supports_partitioned_exclusion_constraints` | `ExclusionConstraint` у партиционированной модели даёт `UnSupportedError`. |
| `supports_pool_status` | `get_pool_status()` даёт `UnSupportedError`, а пулы клиента не попадают в `Connections.get_pool_statuses()`. |
| `sorts_nulls_first` | Где по умолчанию оказывается NULL при сортировке — `nulls_first`/`nulls_last` добавляют фразу, только если она меняет порядок. |
| `enforces_numeric_ranges` | hare сам проверяет диапазоны целых и десятичных перед записью. |
| `supports_conflict_constraint_names`, `supports_conflict_where` | `bulk_create(on_conflict_constraint=...)` / `conflict_where=` дают `UnSupportedError`. |
| `matches_ordering_to_grouping_by_sql` | Задайте его, если выражение сортировки, по которому идёт и группировка, нужно записать точно как в `GROUP BY`. |
| `supports_virtual_generated_columns` | `GeneratedField(stored=False)` даёт `UnSupportedError`. |
| `supports_strict_tables` | `SqliteTableOptions(strict=True)` даёт `UnSupportedError` — опция таблиц SQLite. |
| `supports_text_search_configurations` | Конфигурация поиска, `SearchVector` как значение, лексемы, веса меток, `normalization` и `cover_density` у `SearchRank` и параметры фрагментов `SearchHeadline` дают `UnSupportedError`. |
| `supports_full_text_index` | `FullTextIndex` и веса полей у `SearchRank` дают `UnSupportedError`. |
| `supports_vector_search` | Расстояния векторов и `__nearby` дают `UnSupportedError`. |
| `supports_tenant_schemas` | `TenantSchemas.create()`/`drop()`/`get_tenants()` дают `UnSupportedError`; нужны `get_tenant_client_settings(schema_name)` клиента (путь поиска схемы арендатора), параметр подключения `tenant_schema_template` и `fetch_schema_names()` интроспектора. |
| `supports_spatial` | Операторы, пути, функции и агрегаты `hare.gis` дают `UnSupportedError`; зарегистрируйте рендереры `GeometryValue`, `SpatialRelationTerm`, `SpatialFunctionTerm` и имена `SpatialAggregateFunction`, а также тип колонки `GeometryField` и преобразования его значений. |
| `supports_geography` | Операторы и функции `GeometryField(geography=True)` дают `UnSupportedError`. |
| `supports_spatial_index` | `SpatialiteIndex` даёт `UnSupportedError` — это индекс SQLite; диалект со своим пространственным индексом объявляет свой класс индекса. |
| `spatial_reference_ids` | None: geography и пространственный индекс принимают любую SRID. Множество SRID — те, что есть в пространственных метаданных базы, прочитанные при открытии соединения, — и geography или пространственный индекс в другой SRID дают `UnSupportedError`. |
| `supports_ordered_aggregates` | Агрегат с `order_by=`, у которого нет другой формы, даёт `UnSupportedError` (`MakeLine`). |
| `supports_uuid_v7` | `db_default=UuidV7()` даёт `UnSupportedError` до DDL. |
| `supports_without_overlaps` | `without_overlaps=True` у `UniqueConstraint`/`CompositePrimaryKey` даёт `UnSupportedError`. |
| `supports_returning_old_new` | `returning(old=...)` даёт `UnSupportedError`; иначе `QueryClauses.get_old_row_value_sql()`. |
| `supports_json_table` | `JsonTable` даёт `UnSupportedError`; иначе `QueryClauses.get_json_table_sql()` (по умолчанию ISO). |
| `isolation_levels` | Уровни, на которых выполняется транзакция, от самого слабого — см. [Транзакции](#transactions). |
| `max_identifier_length` | Наибольшая длина имени в байтах, None — без предела. hare генерирует имена до 63 байт, укорачивая более длинное с хэшем; диалект с меньшим пределом не регистрируется. |
| `cascade_depth_limit`, `checks_foreign_keys_per_cascade_step`, `checks_restrict_at_statement_end` | Как ведёт себя собственный каскад базы — см. [базы без гарантий](#without-guarantees). |

Возможности команд схемы перечислены вместе с [редактором схемы](#schema-editor). Класс клиента может
получить возможности другого с изменениями: `AiosqliteClient.features.replace(supports_posix_regex=True)`.

### <a id="executor"></a>Команды уровня моделей

Между моделью и `execute()` нет ничего своего у каждого драйвера: один и тот же конвейер записи
строит каждый `INSERT`, `UPDATE`, вставку-или-обновление и `DELETE` — и для `save()`, и для `bulk_create()`, и для
`QuerySet.update()`, — и одно и то же чтение строк строит объекты из того, что вернул `execute()`.
То, чем базы различаются, спрашивается у частей диалекта и у `Features` подключения: генерируемые
колонки и колонки со значениями по умолчанию базы возвращаются через `INSERT ... RETURNING` при
`supports_returning`; `clauses.get_upsert_inserted_flag_sql()` даёт выражение `RETURNING`,
отличающее строку, вставленную вставкой-или-обновлением, от изменённой (`xmax = 0` в PostgreSQL; при `None` hare читает
существующие ключи перед записью, и только пока есть наблюдатель).

## <a id="dialect"></a>3. Имена, литералы и параметры — `SqlLiterals`, `SqlParameters`

```python
class ColstoreLiterals(SqlLiterals):
    identifier_quote_char = "`"
    alias_quote_char = "`"


class ColstoreParameters(SqlParameters):
    placeholder_template = "{{p{}}}"          # {p1}, {p2}, ...


class ColstoreDialect(Dialect):
    name = "colstore"
    otel_system_name = "colstore"           # db.system.name в OpenTelemetry
    features = Features(
        supports_transactions=False,
        supports_select_for_update=False,
        supports_update_limit_order_by=False,
        supports_foreign_keys=False,
        supports_unique_constraints=False,
    )

    def build_literals(self):
        return ColstoreLiterals(self)

    def build_parameters(self):
        return ColstoreParameters(self)
```

`SqlLiterals` записывает имена и значения в текст SQL; имена и литералы экранируются только здесь —
редактор схемы, отрисовка SQL и чтение схемы спрашивают его:

| Член класса | Что задаёт |
|---|---|
| `identifier_quote_char`, `alias_quote_char`, `quote_identifier(name)`, `qualify_table_name(table, schema)` | Кавычки для имён таблиц, колонок, индексов и ограничений и для псевдонимов в `SELECT`. |
| `get_string_literal_sql(text)`, `get_literal_sql(value)`, `get_boolean_literal_sql(value)`, `get_bytes_literal_sql(value)`, `get_array_literal_sql(element_sqls)` | Литералы, записываемые в текст SQL, — строка, значение колонки по умолчанию, логическое значение запроса, байты, массив. |

`SqlParameters` передаёт значения параметрами:

| Член класса | Что задаёт |
|---|---|
| `placeholder_template`, `get_placeholder(index)`, `numbers_parameters` | Место для параметра запроса; `{}` — его номер, начиная с 1 (`$1` в PostgreSQL, `?` в SQLite). Кэш вида запроса подставляет значения по позиции, поэтому с ним работает любой вид мест для параметров. |
| `get_parameter_cast_type(value, position)`, `get_field_parameter_cast_type(field)`, `get_json_object_value_cast_type(value, value_type)`, `get_cast_parameter_sql(sql, value)` | Тип, к которому приводится значение-параметр там, где его тип ничто не задаёт: по месту в запросе (`ParameterPosition`: ветка `CASE`, выбранная или сравниваемая константа, аргумент функции), как значение колонки, как значение объекта JSON или в готовом SQL. По умолчанию `None` или SQL без изменений; PostgreSQL, который определяет тип параметра только по окружению, добавляет приведение. |
| `get_bindable_number(value)` | Как передаётся число, означающее число JSON. |
| `single_parameter_in_list_min_length` | Длина, начиная с которой список `__in`/`__not_in` передаётся одним параметром. |
| `get_default_rows_source_sql(row_count)`, `get_column_arrays_rows_source_sql(column_types, first_index)` | Источники, из которых многострочный `INSERT` читает строки значений по умолчанию или по одному массиву на колонку, — `None` для команды на строку и `VALUES`. |
| `get_copy_column_type(field)` | Тип, который массовой загрузке (`copy()`) называют для колонки поля, — по умолчанию тот, что объявлен в таблице. |
| `supports_copy_column_type(column_type)` | Загружает ли массовая загрузка колонку такого типа. |

`Dialect.sql_context` — контекст, в котором выводится каждая команда; класс запроса задаёт
`SQL_CONTEXT = dialect.sql_context`.

## <a id="types"></a>4. Типы колонок и значения — `build_types()`

`build_types()` возвращает `TypeRegistry` (`hare.dialects.base.types`), который сопоставляет классам
полей то, как диалект их хранит. Класс поля использует запись ближайшего зарегистрированного базового
класса, поэтому стороннее поле наследует хранение своего родителя:

```python
def build_types(self):
    types = TypeRegistry()
    types.register(BigIntField, TypeMapping(column_type="Int64"))
    types.register(DatetimeField, TypeMapping(column_type="DateTime64(6, 'UTC')"))
    types.register(UUIDField, TypeMapping(column_type="UUID"))
    types.register(CharField, TypeMapping(column_type=lambda field: "String"))
    types.register(
        DecimalField,
        TypeMapping(column_type=lambda field: f"Decimal({field.max_digits}, {field.decimal_places})"),
    )
    return types
```

| Член `TypeMapping` | Что задаёт |
|---|---|
| `column_type` | Тип колонки или функция `(field) -> str`. Без него — собственный `SQL_TYPE` поля. |
| `generated_sql` | Команда создания колонки первичного ключа, который генерирует база. |
| `function_cast` | Функция `(field, term) -> term`, которой колонка оборачивается везде, где её сравнивают, сортируют или копируют. |
| `to_db`, `to_lookup`, `to_python` | Заменяют `to_db_value()`, `to_lookup_value()` и `from_db_value()` поля — каждое значение передаётся и читается через подключение запроса. |
| `json_term` | Текст, которым значение колонки записывается в объект JSON, — для значения, которое хранится в виде, непригодном для JSON (16-байтовые UUID колоночного диалекта). |
| `extension` | Расширение базы, которое нужно типу колонки (`postgis` для пространственных типов PostgreSQL), — создаётся везде, где используется поле этого класса, и попадает в миграции как `CreateExtension`. |
| `naive_datetime_is_utc` | `to_python` — собственный `from_db_value()` поля, а дата-время без пояса от драйвера читается как момент в UTC. |
| `inserted_by_select` | INSERT, записывающий колонку, пишет строки как `SELECT` из них, а не `VALUES`, — для значения, записанного выражением, которое `VALUES` диалекта не читает; или `(field) -> bool`, решающая это для каждого поля. |

`Features` описывают поведение значений: `enforces_numeric_ranges` (без него hare сам проверяет
диапазоны целых и десятичных), `supports_virtual_generated_columns`. Там, где значение хранится или
вычисляется иначе, SQL дают `TermRenderers` диалекта ([следующий раздел](#functions-and-lookups)).
Функция, которая есть в базе только для части типов аргументов (`ROUND(numeric, int)` в PostgreSQL),
— тоже забота рендерера.

Пакет, добавляющий поле, может зарегистрировать его хранение на любом диалекте:

```python
DialectRegistry.get_dialect("colstore").types.register(MoneyField, TypeMapping(column_type="Decimal(18, 2)"))
```

## <a id="functions-and-lookups"></a>5. Выражения и операторы фильтров — `TermRenderers`, `FilterOperators`

По умолчанию каждое выражение выводится стандартным SQL. `build_renderers()` возвращает `TermRenderers`
— замены для того, что база записывает иначе; они находятся по иерархии классов выражения:

```python
def build_renderers(self):
    renderers = TermRenderers(self)
    renderers.register_function("LENGTH", self.render_length)         # (function, sql_context) -> sql
    renderers.register_name(functions.Coalesce, lambda term, sql_context: "ifNull")
    return renderers

@staticmethod
def render_length(length, sql_context):
    return f"lengthUTF8({length.get_arg_sql(length.args[0], sql_context)})"
```

Собственные диалекты hare строят свои рендереры на `CheckedTermRenderers` (`hare.dialects.base.renderers`): его
`add_own_renderers()` регистрирует рендереры диалекта, сгруппированные по темам в классы с одним
`register(renderers)` — `<Диалект>JsonRenderers`, `<Диалект>TemporalRenderers`,
`<Диалект>NumberRenderers`, `<Диалект>TextRenderers`, а также рендереры пространственных терминов,
векторов и полнотекстового поиска. При создании он проверяет, что у каждого термина, который должен
отрисовывать каждый диалект hare, — терминов JSON, дат и времени, чисел и текста, которые ядро
оставляет диалекту, — есть рендерер, и даёт `TypeError` с именами недостающих. Сторонний диалект может
начать с него так же или с обычного `TermRenderers`.

Подкласс `TermRenderers` даёт ещё и выражения, которые диалект подставляет вместо исходного там, где
его SQL нужно другое:

| Член класса | Что задаёт |
|---|---|
| `is_distinct_from_operator` | Неравенство, правильное при `NULL` (`IS NOT` в SQLite). |
| `get_decimal_compared_term()`, `get_decimal_value_term()`, `get_decimal_dividend()`, `get_assigned_decimal_term()` | Десятичные, хранящиеся текстом: при сравнении, в смеси с другими числами, при делении, при присваивании в `UPDATE`. |
| `get_json_path_comparand()` | С чем сравнивается значение JSON по пути — JSON, хранящийся текстом. |
| `get_integer_aggregate_as_float()` | Среднее или статистика по целым, читаемые как число с плавающей точкой. |
| `get_datetime_part_comparand()` | Дата или время суток, сравниваемые с частью момента времени. |
| `get_concatenated_argument_sql(sql, argument)` | Аргумент склейки текста — с приведением там, где база определяет его тип только по окружению. |
| `get_ordering_term()` | По чему `ORDER BY` сортирует для выражения. |
| `get_composite_distinct_key()` | По чему `COUNT(DISTINCT ...)` считает составной ключ. |
| `get_never_null_column_count_argument(term)` | Что `COUNT` получает для колонки запрашиваемой таблицы, в которой нет NULL, — по умолчанию саму колонку; `*` там, где посчитать строки дешевле, чем читать колонку. |
| `get_connection_only_function(sql)` | Функция, которую hare устанавливает в своих соединениях и которую нельзя использовать в командах схемы. |

`build_filter_operators()` возвращает `FilterOperators` — соответствие между функцией сравнения,
которую задаёт оператор фильтра, и её заменой в диалекте
(`FilterOperators(self, {Lookups.is_in: colstore_is_in, ...})`, `{}` — без замен). Оператор, который
реализуют только диалекты (`DialectImplementedOperators`), должен быть заменён, иначе оператор фильтра
не поддерживается: `FilterOperators.supports_lookup()` — а с ним и
`Model._meta.get_lookups(path, dialect)` — его не включает, а фильтр с ним даёт ошибку до построения
SQL.

Слишком длинный список значений, чтобы привязывать по параметру на значение, сокращает диалект:
создайте подкласс `LargeInList` (`hare.dialects.base.parameters.large_in_list`) и в `build_filter_operators()`
сопоставьте `Lookups.is_in`/`not_in`/`row_is_in`/`row_not_in` его одноимённым методам. Алгоритм
написан один раз в базовом классе — когда применяется короткая форма, как отдельно сравниваются
`NULL` из списка, как отрицается `not_in`, форма «строка значений» для составного ключа; диалект
даёт только контейнер, в который значения привязываются **одним** параметром:
`get_membership_criterion(field, values, non_null_values, element_type)` (`= ANY($1::type[])` в
PostgreSQL, `IN (SELECT value FROM json_each(?))` в SQLite) и
`get_row_container(value_rows, element_types)` для строк значений. Возврат `None` оставляет обычный
список `IN (...)`.

Рендерер меняет то, как пишется SQL терма, но никогда не то, по чему составлен ключ плана запроса, —
диалект входит в каждый ключ. Собственный класс выражения диалекта (функции массивов, триграмм и
пространственные функции PostgreSQL) объявляет в `plan_parts`, как его атрибуты входят в план, и
разрешает аргументы через `ExpressionArguments.get_result()`, как любое
[своё выражение](custom-functions-and-expressions.ru.md#writing-a-custom-expression).

## <a id="query-class"></a>6. Части команд — `QueryClauses`

`QueryBuilder` один на все базы. Он выводит SQL по стандарту ISO и спрашивает `QueryClauses` диалекта
о каждой части, которую базы записывают по-разному; базовый `QueryClauses` пишет SQL по стандарту
(`OFFSET ... ROWS FETCH FIRST ... ROWS ONLY`) и даёт `UnSupportedError` для части, которой в стандарте
нет:

| Член класса | Что записывает |
|---|---|
| `get_statement_context(builder, sql_context)` | Контекст, в котором выводятся термы самой команды, — по умолчанию тот же; диалект, рендерерам которого нужно знать что-то о команде вокруг терма (что в ней есть группировка), возвращает свой контекст с этим знанием. |
| `get_bulk_load_statement_sql(table, columns)` | Команда, под которой массовая загрузка (`copy()`) сообщается наблюдателям, спанам и счётчику запросов. |
| `get_distinct_sql(builder, sql_context)` | `DISTINCT` / `DISTINCT ON (...)`. |
| `get_row_lock_sql(builder, sql_context)` | Блокировку строк (`select_for_update()`): `FOR UPDATE`, `NO KEY`, `OF`, `NOWAIT`, `SKIP LOCKED`. |
| `get_statement_end_sql(builder, sql_context)` | Окончание подзапроса или запроса с клаузами методов `QuerySet` диалекта после его `LIMIT` — по умолчанию пустое; ClickHouse пишет туда свои `SETTINGS` и даёт подзапросу с соединениями `join_use_nulls = 1`: иначе условие мутации выполняется без этой настройки сессии. |
| `get_main_table_suffix_sql(builder, sql_context)` | То, что идёт после первой таблицы `FROM`, — по умолчанию её выборка (`get_table_sample_sql()`); ClickHouse пишет туда `FINAL` и `SAMPLE ... OFFSET ...`. |
| `get_prewhere_sql(builder, sql_context)`, `get_limit_by_sql(builder, sql_context)` | Условие, читаемое до `WHERE`, после соединений; ограничение числа строк в группе, после `ORDER BY` — по умолчанию пустые. |
| `get_limit_offset_sql(limit_sql, offset_sql)` | Границы строк запроса. |
| `get_returning_sql(returned)` | `RETURNING` из `ReturnedValue` — SQL значения, псевдоним, под которым оно возвращается, имя обычной колонки. |
| `get_lateral_sql(subquery_sql)` | Подзапрос, присоединённый как `LATERAL`, — по умолчанию ISO `LATERAL (...)`. |
| `get_table_sample_sql(method, percent_sql, seed_sql)` | Выборку таблицы во `FROM` — по умолчанию ISO `TABLESAMPLE ... REPEATABLE (...)`. |
| `get_merge_sql(...)`, `get_merge_when_sql(when)`, `get_merge_returning_sql(returned, action_alias_sql)` | `MERGE` из готовых частей (`MergeWhenSql`) — по умолчанию ISO `MERGE`; ветка `DO NOTHING`, `WHEN NOT MATCHED BY SOURCE` и `RETURNING` там дают `UnSupportedError`. |
| `get_on_conflict_sql(builder, sql_context)` | Обработку конфликта у `INSERT`. |
| `get_update_sql(builder, sql_context)` | `UPDATE` — с чтением других таблиц во `FROM`, сортировкой и ограничением строк, если база это умеет. |
| `get_delete_sql(builder, sql_context)` | `DELETE` — по умолчанию ISO `DELETE FROM` с условиями; база, которая удаляет строки иначе (мутация `ALTER TABLE ... DELETE`), пишет свой. |
| `get_typed_placeholder_template()`, `get_values_table_columns_sql()`, `get_update_from_values_sql()`, `get_insert_rows_source_sql()` | Команды, которые hare пишет текстом: `UPDATE` из таблицы `VALUES` (`bulk_update()`), `INSERT` строк из источника. |
| `get_upsert_inserted_flag_sql()` | Выражение `RETURNING`, отличающее строку, вставленную вставкой-или-обновлением, от изменённой. |
| `get_explain_sql(sql, output_format, options, features)` | Команду `EXPLAIN`, которую используют `QuerySet.sql(explain=True)` и `explain()`. |

Класс запроса только привязывает команды к диалекту:

```python
ColstoreQuery = DeclaredSubclass.make(
    Query, "ColstoreQuery", __package__, "A query in Colstore's SQL.", SQL_CONTEXT=COLSTORE_DIALECT.sql_context
)
```

### <a id="queryset-methods"></a>Методы `QuerySet` от диалекта

Собственные модификаторы запросов базы становятся методами `QuerySet`, и hare о них знать не нужно.
Зарегистрируйте их в `install()` функцией, которая получает построитель запроса и аргументы вызова и
возвращает построитель, — например, `random_share()` колоночного тестового диалекта:

```python
def install(self):
    super().install()
    QuerySetExtensions.register("random_share", self.name, self.apply_random_share)

def apply_random_share(self, builder, percent):
    return builder.where(LiteralValue(f"abs(random()) % 100 < {int(percent)}"))
```

`Event.objects.filter(...).random_share(10)` запоминает вызов — в том числе до `Hare.init()`, — а запрос применяет
его, когда строится для своего подключения; на подключении другого диалекта это даёт
`UnSupportedError`. Пакет может зарегистрировать метод и для диалекта, который определил не он.

`QuerySetExtensions.register(name, dialect_name, apply)` (из `hare.query.queryset.extensions`) обычно
вызывается в `install()` диалекта. `apply(builder, *args, **kwargs)` возвращает построитель запроса с
применённым вызовом. Вызов запоминается в запросе (в том числе до `Hare.init()`) и переносится в
`values()`, `count()` и другие запросы, построенные из него; когда запрос строится для своего
подключения, вызов применяет реализация диалекта этого подключения. На подключении, диалект которого
такой метод не зарегистрировал, запрос даёт `UnSupportedError`, а не молча пропускает вызов. Имя,
которое у `QuerySet` уже есть, или имя, начинающееся с `_`, отклоняется с `ConfigurationError`. Имя и
аргументы каждого вызова входят в ключ плана: они вписываются в запрос после его построения и
поэтому — часть текста SQL; вызов с аргументом, который не может быть частью ключа (список, словарь),
лишает запрос плана. Аргумент, который сам описывает себя для плана, — `Q`, выражение, — входит в ключ
своей структурой, а его значения подставляются, как значения фильтров.

`register()` принимает ещё четыре именованных аргумента — для метода, которому мало построителя:

| Аргумент | Значение |
|---|---|
| `reads_query=True` | `apply(builder, extension_query, *args, **kwargs)` получает и запрос — `QuerySetExtensionQuery`: его `model`, `connection`, `dialect`, `table`, признаки `is_write` и `is_summary`, а также `get_condition(q)`, `get_expression(name_or_expression)` и `join(builder, joins)`, которые разрешают условие или поле так же, как собственные фильтры запроса, и записывают их значения в его план. |
| `takes_condition=True` | Метод принимает аргументы `filter()` — объекты `Q` и фильтры, — записанные одним `Q`, который получает `apply` (`prewhere()` ClickHouse). Все диалекты, регистрирующие это имя, объявляют его одинаково, иначе `register()` даёт `ConfigurationError`. |
| `changes_rows=True` | Вызов меняет, какие строки возвращает запрос, помимо его условий (`limit_by()` ClickHouse): `count()` и `exists()` считают строки запроса как производной таблицы. |
| `read_result=` | `await read_result(extension_query, result, *args, **kwargs)` читает результат запроса строк с этим вызовом и возвращает то, что вернёт запрос (`with_totals()` ClickHouse выполняет второй оператор и возвращает строки вместе с итогами). |

`QueryBuilder.set_dialect_clause(name, value)` хранит то, что задаёт вызов, — это пишет `QueryClauses`
диалекта в `get_main_table_suffix_sql()`, `get_prewhere_sql()`, `get_limit_by_sql()` и
`get_statement_end_sql()`; их спрашивают только у запроса, где такие клаузы есть. `hare stubs` и плагин
mypy объявляют методы диалектов соединений проекта с параметрами `apply` после построителя (и запроса).

## <a id="transactions"></a>7. Транзакции и одновременная работа

`Features.supports_transactions` решает, есть ли транзакции вообще (см. выше).

Сама транзакция — один автомат состояний, написанный один раз в `TransactionClient`
(`hare.dialects.base.client`): `begin()`, `commit()`, `rollback()`, `savepoint()`,
`release_savepoint()` и `savepoint_rollback()`, вложенность, обработчики `on_commit()`/`on_rollback()`,
защита `COMMIT` от отмены задачи, отказ выполнять команду после окончания транзакции и сообщения
`TransactionEvent`. Клиент транзакции драйвера даёт только примитивы, которые автомат вызывает:

| Примитив | Что делает |
|---|---|
| `_driver_begin()`, `_driver_commit()`, `_driver_rollback()` | Начинает транзакцию на соединении; отправляет её `COMMIT`; отправляет её `ROLLBACK`. |
| `_driver_savepoint(name)`, `_driver_release_savepoint(name)`, `_driver_rollback_to_savepoint(name)` | Открывает точку сохранения, освобождает её и откатывается к ней. |
| `_get_new_savepoint_name()` | Имя точки сохранения, ещё не использованное на этом соединении. |
| `_is_connection_lost(error)` | Означает ли ошибка драйвера при `COMMIT`/`ROLLBACK`, что соединение пропало, — такую транзакцию сервер откатывает. По умолчанию `False`. |
| `_is_commit_outcome_unknown(error)` | Остаётся ли при потере соединения неизвестным, прошёл ли выполнявшийся `COMMIT`. |
| `_is_commit_rejection(error)` | Ответила ли база на `COMMIT` ошибкой — транзакция закончена. |
| `_is_transaction_finished_error(error)` | Сообщает ли драйвер, что более ранний, прерванный `COMMIT`/`ROLLBACK` уже закончил транзакцию. |
| `_check_commit_allowed()`, `_check_savepoint_allowed()`, `_before_top_level_end(event)`, `_after_rejected_commit(error)` | Необязательные точки расширения вокруг шагов автомата: SQLite отказывается фиксировать прерванную транзакцию и возвращает транзакцию «только чтение» в обычный режим перед её окончанием. |
| `_take_transaction_resources()`, `_give_back_transaction_resources()` | Забирают то, что транзакция верхнего уровня держит всю свою жизнь, до `BEGIN` — соединение из пула, блокировку соединения — и возвращают сразу, как только `COMMIT`/`ROLLBACK` выполнен, до обратных вызовов. По умолчанию ничего не делают. |
| `_undo_failed_begin()` | Завершает транзакцию, чей `BEGIN` упал, но мог успеть выполниться. По умолчанию ничего не делает. |
| `_end_unfinished_transaction()` | Завершает то, что транзакция верхнего уровня оставила открытым у драйвера, когда её блок закончился. По умолчанию ничего не делает. |

Шесть методов `_driver_*` и `_get_new_savepoint_name()` абстрактные; у остальных есть реализация по
умолчанию. Вложенная транзакция — клиент того же класса на том же соединении; его создаёт базовый
класс.

Клиент, который сам применяет `lock_timeout` транзакции, — таймаут занятости SQLite, ожидание
блокировок строк ClickHouse — ставит `enforces_lock_timeout_itself = True`, и база без команды для
него тогда принимает эту опцию.

`TransactionStatements` диалекта (`build_transactions()`) настраивают транзакцию:

| Член класса | Что задаёт |
|---|---|
| `get_isolation_level(requested)` | Уровень, на котором выполняется транзакция, попросившая уровень, — самый слабый из `Features.isolation_levels`, который не слабее запрошенного. |
| `get_isolation_level_sql(level)` | Команду, задающую его; `None`, если каждая транзакция и так выполняется на этом уровне. |
| `get_read_only_sql()` | Команду, делающую транзакцию «только чтение» (по умолчанию `SET TRANSACTION READ ONLY` по стандарту). |
| `get_begin_sql()`, `get_commit_sql()`, `get_rollback_sql()` | Команды, которые открывают, фиксируют и откатывают транзакцию (по умолчанию `START TRANSACTION`, `COMMIT`, `ROLLBACK` по стандарту), — ими обрамляется SQL, который `sqlmigrate` печатает для атомарной миграции, и они отправляются там, где hare завершает транзакцию в обход своих обработчиков. |
| `get_statement_timeout_sql(milliseconds)`, `get_lock_timeout_sql(milliseconds)` | Ограничения времени команды и ожидания блокировки на время транзакции; `None`, если у базы их нет. |

Часть `TableLocks` редактора схемы (`table_locks_class`): `get_lock_table_sql()` блокирует таблицу на время транзакции, а
`get_migration_lock_sql()` выстраивает одновременные запуски `migrate` по очереди;
`build_two_phase_commit()` даёт `Transactions.distributed()` его команды.

## <a id="schema-editor"></a>8. Команды схемы — редактор схемы

`build_schema_editor_class()` возвращает подкласс `BaseSchemaEditor`. Редактор выполняет публичные
операции (`create_model()`, `add_field()`, `alter_field()`, `add_index()`, ...) как порядок их шагов;
команды каждого вида изменений пишут его **части** — подклассы `SchemaEditorPart` из
`hare.dialects.base.schema.<папка>`, которые редактор создаёт один раз из объявленных им классов и
которые обращаются к нему как `self.editor`:

| Папка | Части |
|---|---|
| `columns/` | `ColumnDefinitions`, `ColumnTypeChanges`, `ColumnBackfill`, `ColumnNarrowingCheck` |
| `tables/` | `TableCreation`, `TableRebuild`, `TableComments`, `TablePartitions` |
| `relations/` | `ForeignKeyRebuild`, `ManyToManyThroughTables` |
| `indexes/` | `IndexStatements`, `GeneratedIndexNames` |
| `constraints/` | `ConstraintStatements`, `ConstraintNames` |
| `triggers/` | `TriggerStatements` |
| `schema_objects/` | `Views`, `MaterializedViews`, `DatabaseFunctions`, `Sequences`, `RowLevelSecurityPolicies`, `Grants`, `EnumTypes`, `Extensions`, `Schemas` |
| `runtime_statements/` | `TableLocks`, `TableClearing`, `TenantConditions` |

Диалект пишет часть по-своему, создав её подкласс и назвав его у своего редактора, — атрибут
называется по имени части в snake case с `_class`, сама часть — без него:

```python
class ColstoreTableComments(TableComments):
    editor: ColstoreSchemaEditor

    def get_table_comment_sql(self, table, comment):
        literal = self.editor.client.dialect.literals.get_string_literal_sql(comment)
        return f"ALTER TABLE {self.editor.quote(table)} MODIFY COMMENT {literal}"


class ColstoreSchemaEditor(BaseSchemaEditor):
    table_comments_class = ColstoreTableComments    # editor.table_comments
```

Базовый класс выводит стандартные команды по шаблонам класса редактора (`TABLE_CREATE_TEMPLATE`,
`FIELD_TEMPLATE`, `INDEX_CREATE_TEMPLATE`, `FOREIGN_KEY_TEMPLATE`, `ADD_FIELD_TEMPLATE`,
`ALTER_FIELD_TYPE_TEMPLATE`, `RENAME_INDEX_TEMPLATE`, ...); диалект переопределяет те, которые его база
записывает иначе, и задаёт шаблону `None`, если такой команды нет, — тогда изменение пересоздаёт
таблицу (`TableRebuild.remake_table()`: новая таблица по модели, копирование строк, замена старой).
`TableComments.get_table_comment_sql()` и `TableComments.get_column_comment_sql()` записывают
комментарии; `ColumnNarrowingCheck.get_decimal_overflow_predicate_sql()` находит хранимые числа,
которые не поместятся после `AlterField` на более узкий десятичный тип.

`Features` решают, какие команды вообще записываются:

| Возможность | Без неё |
|---|---|
| `supports_foreign_keys` | Нет ограничений `FOREIGN KEY`; hare сам выполняет каждое действие `on_delete` и проверку `PROTECT`, как для связи с `db_constraint=False`. |
| `supports_unique_constraints` | Нет ограничений уникальности; уникальный `Index` становится обычным индексом; вставка-или-обновление ищет конфликт только по первичному ключу. |
| `supports_adding_constraints` | Ограничения CHECK записываются в `CREATE TABLE`, ограничения уникальности становятся уникальными индексами, а последующее изменение пересоздаёт таблицу. |
| `supports_partial_indexes`, `supports_index_nulls_order`, `supports_concurrent_indexes` | Эта часть индекса не записывается или отклоняется. |
| `supports_exclusion_constraints`, `supports_deferrable_constraints`, `supports_not_valid_constraints` | Такой вид ограничения отклоняется. |
| `supports_statement_triggers`, `supports_extensions`, `supports_collations` | Триггеры `FOR EACH STATEMENT`, `CreateExtension`, `CreateCollation` отклоняются или пропускаются. |
| `truncates_values_on_type_change` | Задайте её, если сужающая смена типа молча обрезает данные, — тогда hare сначала проверяет данные. |
| `alters_indexed_columns` | Сбросьте её, если СУБД не меняет столбец, покрытый индексом, — hare удалит покрывающие индексы на время изменения и создаст их заново. |

Возможность только сообщает, что она у базы есть; базовый редактор её синтаксис не пишет. SQL пишут
части диалекта, который её выставил; фразы ниже — методы класса части, и ядро вызывает их как
`dialect.schema_editor_class.<часть>_class.<метод>()` там, где редактора у него нет:

| Возможность | Что реализуют части диалекта |
|---|---|
| Неключевые колонки индекса (`include=`) | `IndexStatements.get_index_include_sql(quoted_columns)` — фраза после ключей индекса; по умолчанию `""`, и колонки в индекс не попадают. |
| `UniqueConstraint(nulls_distinct=...)` | `ConstraintStatements.get_nulls_distinct_sql(nulls_distinct)` — фраза; ещё выставьте `Features.supports_nulls_distinct`. |
| Ограничения исключения | `ConstraintStatements.get_exclusion_constraint_extension(constraint, fields_by_name)` — расширение, которое нужно ограничению; `ConstraintStatements.exclusion_constraint_sql(model, constraint)` — его определение. |
| `supports_concurrent_indexes` | `add_index(model, index, concurrently)` и `remove_index(...)` редактора; базовый редактор создаёт и удаляет индекс обычным способом. `IndexStatements.get_index_create_sql()` и `IndexStatements.get_index_drop_sql()` дают обычные команды, от которых можно оттолкнуться. |
| `supports_not_valid_constraints` | `ConstraintStatements.add_check_constraint_not_valid()` и `validate_constraint()`; базовый редактор добавляет ограничение как обычно и ничего не проверяет отдельно. |
| `supports_extensions` | `Extensions.create_extension()`, `drop_extension()` и `get_extension_create_sql()`. |
| `supports_enum_types` | `EnumTypes.get_enum_type_create_sql()`, `drop_enum_type()` и `alter_enum_type()` — типы `ENUM` колонок `NativeEnumField`. |
| `supports_views` | `Views.get_view_create_sqls()`, `drop_view()` и `rename_view()`; базовый `alter_view()` удаляет представление, создаёт новое и снова выдаёт права на него. |
| `supports_materialized_views` | `MaterializedViews.get_materialized_view_create_sqls()` (с уникальным индексом по `unique_columns`), `drop_materialized_view()`, `rename_materialized_view()` и `refresh_materialized_view()`. |
| `supports_database_functions` | `DatabaseFunctions.get_function_create_sqls()`, `drop_database_function()`, `alter_database_function()` и `rename_database_function()`. |
| `supports_sequences` | `Sequences.get_sequence_create_sqls()`, `get_sequence_owner_sqls()`, `drop_sequence()`, `alter_sequence()`, `rename_sequence()` и `get_next_sequence_value()`. |
| `supports_row_level_security` | `RowLevelSecurityPolicies.get_row_level_security_sqls()`, `get_policy_create_sqls()`, `drop_policy()`, `alter_policy()` и `rename_policy()`; для арендаторов через row level security ещё `TenantConditions.get_tenant_condition_sql(quoted_column, column_type)` (условие `TenantCondition`), `TransactionStatements.get_tenant_setting_sql(tenant_texts)` (команда после `BEGIN`, записывающая арендаторов транзакции) и параметр подключения `tenant_row_level_security`. |
| `supports_grants` | `Grants.get_grant_sqls()` и `get_revoke_sqls()`. |
| `supports_collations` | `Extensions.create_collation()` и `drop_collation()`. |
| `supports_schemas` | `Schemas.move_table_to_schema()` — для модели, у которой меняется `Meta.schema`. |
| Отложенные ограничения | `TableClearing.defer_cascade_foreign_keys(model, connection)` — откладывает внешние ключи, в которые упирается каскад. |
| Тестовые базы | `TableClearing.clear_tables(connection, quoted_tables)` — очищает таблицы между тестами (по умолчанию — один скрипт с `DELETE FROM` для каждой таблицы). |

То же относится к операторам фильтра, которые есть только у одной базы: диалект регистрирует их в
своём `install()` через [`Field.register_lookup()`](custom-lookups.ru.md),
называя себя в `dialects=`, а нужное расширение — в `required_extension=`; так PostgreSQL
регистрирует свои триграммные операторы.

### <a id="table-options"></a>Параметры хранения таблицы

База, у которой `CREATE TABLE` принимает больше, чем колонки, объявляет подкласс `TableOptions`; модели
перечисляют его в `Meta.table_options`, по одной записи на диалект, а подключение использует запись
своего диалекта:

```python
@dataclasses.dataclass(frozen=True)
class ColstoreTableOptions(TableOptions):
    dialect_name: ClassVar[str] = "colstore"

    engine: str = "MergeTree()"
    order_by: tuple[str, ...] = ()
    partition_by: RawSQLTerm | None = None

    def raise_if_unsupported(self, model):
        if not self.order_by:
            raise ConfigurationError(f"{model.__name__}: ColstoreTableOptions needs order_by")

    def get_create_suffix_sql(self, model, quote):
        sql = f" ENGINE = {self.engine} ORDER BY ({', '.join(quote(column) for column in self.order_by)})"
        return sql + (f" PARTITION BY {self.partition_by.sql}" if self.partition_by else "")


class Event(Model):
    class Meta:
        primary_key = None
        table_options = [ColstoreTableOptions(order_by=("created_at",))]
```

Диалект называет класс в `build_table_options_class()`. Миграции хранят параметры в состоянии модели и
записывают их в файлы миграций через `deconstruct()`; их изменение применяет `alter_table_options()`
части `TablePartitions` — по умолчанию таблица пересоздаётся с новыми параметрами. `hare drift` сравнивает их с
тем, что прочитано из базы, а `inspectdb` записывает их в `Meta.table_options` (см. чтение схемы
ниже).

Параметры, в которых есть части таблицы, добавляемые и удаляемые миграциями по одной, — партиции, —
получают ещё четыре хука; значение по умолчанию у каждого означает «таких частей нет»:

| Хук | Что даёт |
|---|---|
| `get_partitions()` | Партиции по имени; объект партиции называет свой диалект в `dialect_name`, имеет `name` и `deconstruct()`. |
| `with_partitions(partitions)` | Те же параметры с другим набором партиций. |
| `can_change_partitions_to(new_options)` | Достижимы ли другие параметры добавлением и удалением партиций — иначе изменение идёт одним `AlterModelOptions`. |
| `with_field_names(column_to_field_name)` | Параметры с именами полей модели вместо имён колонок — параметры, прочитанные из базы, называют колонки. |

Параметрам, которые хранят строки не в собственной таблице модели, — таблица, распределённая по
серверам кластера, — нужны ещё три хука:

| Хук | Что даёт |
|---|---|
| `get_storage_table_name(table_name)` | Таблицу, для которой пишется DDL колонок и хранения модели, — по умолчанию собственную таблицу модели. |
| `get_companion_table_sqls(model, quote, client, replaces=...)` | Команды, создающие таблицу перед той, что хранит строки; по умолчанию никаких. |
| `keeps_row_versions()` | Хранит ли таблица несколько строк одного ключа, которые база сливает, — тогда ключ не проверяется (`checks_constraints_before_write`). |

`copy_table_rows(model, new_table_name, old_table_name, column_mapping)` перестройки таблицы копирует
строки в перестроенную таблицу — по умолчанию одним `INSERT ... SELECT`. `get_journal_table_options(connection)`
диалекта задаёт хранение журнала применённых миграций (на кластере ClickHouse он реплицируется), а
`synchronize_table(connection, table_name)` ждёт строк, которые в него записали другие серверы, перед
чтением журнала.

`raise_if_unsampled(model)` отклоняет `sample()` таблицы, параметры которой выборку не допускают, —
у ClickHouse без `sample_by`; по умолчанию ничего не делает. Диалект, который пишет первичный ключ
таблицы не среди её колонок (ClickHouse — в клаузе `PRIMARY KEY` движка), возвращает `""` из
`get_composite_pk_constraint_sql()` своей части создания таблиц и не пишет `PRIMARY KEY` в
`get_field_sql()` своей части определений колонок.

Тогда `makemigrations` пишет `AddPartition`/`RemovePartition` на каждую добавленную или убранную
партицию, а они вызывают `add_partition(model, partition)`/`remove_partition(model, partition)`
части `TablePartitions` (по умолчанию — `UnSupportedError`); `TablePartitions.get_partition_create_sqls(model, safe)`
возвращает команды создания партиций сразу после `CREATE TABLE` таблицы. На этих хуках построены
[партиционированные таблицы](../models/meta-options.ru.md#partitioning) PostgreSQL.

### <a id="models-without-a-primary-key"></a>Модели без первичного ключа

У таблицы колоночной базы часто нет первичного ключа: `Meta.primary_key = None` (см.
[Опции Meta](../models/meta-options.ru.md#primary_key)). Чтение, фильтры, агрегаты,
`bulk_create()` и `QuerySet.update()`/`delete()` работают; то, чему нужен ключ, чтобы найти строку
(`save()` прочитанной строки, `instance.delete()`, связи с моделью и т. п.), даёт `ConfigurationError`.

## <a id="introspector"></a>9. Чтение существующей схемы

`build_introspector_class()` возвращает подкласс `SchemaIntrospector`, реализующий
`fetch_default_schema()`, `fetch_table_names(connection, schema, include_partitions)` и
`fetch_tables(connection, tables, schema) -> list[TableInfo]`; `fetch_schema_names(connection)` перечисляет схемы для
[схемы на арендатора](../soft-delete-versions-tenants/schema-per-tenant.ru.md) (по умолчанию `UnSupportedError`). Остальное — `inspectdb`, поиск
расхождений, обратное сопоставление типов и полей — работает с общими `TableInfo`/`ColumnInfo`/
`IndexInfo`/`ForeignKeyInfo`. Хранение таблицы попадает в `TableInfo.table_options` через
`table_options_class.from_observed({"engine": ..., "order_by": ...})`, который оставляет имена,
объявленные классом, и даёт `None`, если все параметры по умолчанию; `fetch_declared_table_options()`
позволяет поиску расхождений считать то же хранение, записанное базой иначе, совпадающим с объявленным,
а `fetch_declared_schema_objects(connection, schema, declared, table_info, column_to_field_name)`
возвращает объявленные моделью представления, материализованные представления и словари такими, какими
их держит база, — по умолчанию как объявлены, база не спрашивается.

То, как база возвращает выражение, тоже решает чтение схемы: `NOW_EXPRESSIONS` (написания текущего
момента), `SEQUENCE_DEFAULT_PREFIXES` (значение по умолчанию из последовательности),
`split_type_cast(sql)` (приведение типа в конце выражения, отделённое от него, — например
`'active'::character varying` в PostgreSQL), `NUMERIC_CAST_TYPES` (приведения, при которых литерал в
кавычках — число) и `strip_type_casts(sql)` (выражение без всех приведений — чтобы сравнить
объявленное значение по умолчанию с прочитанным). По умолчанию ничего такого не читается. Без чтения
схемы (`None`, по умолчанию) `inspectdb` и `hare drift` дают `UnsupportedDialectError` (`hare.inspectdb.exceptions`, это `UnSupportedError`).

## <a id="migrations"></a>10. Миграции

Журнал миграций и каждая операция идут через редактор схемы, поэтому своего им ничего не нужно.
`Features.can_rollback_ddl` решает, выполняется ли миграция в транзакции, `TableLocks.get_migration_lock_sql()`
— ждут ли друг друга два запуска `migrate`, а `get_connection_only_function(sql)`
рендереров называет функцию, которую hare устанавливает только в своих соединениях и которую поэтому
нельзя использовать в командах схемы (функции SQLite, которые hare регистрирует в каждом соединении).

`ForeignKeyRebuild.add_foreign_key_constraint_not_valid()` и
`ConstraintStatements.add_unique_constraint_using_index()` стоят за `AddField(..., not_valid=True)` и `AddConstraint(..., using_index=...)`; базовый редактор добавляет
ограничение с проверкой строк, а для `using_index` строит собственный индекс ограничения и удаляет
прежний. Метод класса `ColumnTypeChanges.rewrites_table_on_alter()` говорит, какие изменения поля переписывают
таблицу, — по умолчанию любая смена типа колонки.

[Проверка миграций](../migrations/zero-downtime.ru.md#checking-migrations) берёт правила из
`MigrationSafetyRules`: их перечисляет `get_rules()` — базовый класс даёт общие для всех баз, диалект
добавляет свои; каждое правило — подкласс `MigrationSafetyRule` в своём файле с `code`
(`MigrationRiskCode`) и `check_operation(context)` (или `check_migration(contexts)` для правила обо всей
миграции), возвращающим `MigrationRisk` из `context.get_risk(...)`.
`count_table_rows(client, table_name, schema, at_most)` считает строки таблицы до `at_most` (подойдёт и
оценка), 0 — для таблицы, которой нет; базовый класс возвращает None — тогда любая таблица считается
большой.

## <a id="without-guarantees"></a>11. Что hare делает для базы без гарантий

| У базы нет | hare |
|---|---|
| Транзакций | Отклоняет `atomic()`; свои записи из нескольких команд выполняет по одной команде. |
| Точек сохранения | Присоединяет вложенный `atomic()` к транзакции, которая откатывается целиком, если вложенный блок завершился ошибкой. |
| Генерируемых ключей | Отклоняет при привязке модель, чей первичный ключ генерирует база, — или берёт её ключи из серии до вставки (`takes_keys_before_insert`). |
| `UPDATE` / `DELETE` сохранённых строк | Отклоняет каждую такую команду до отправки; весь каскад проверяется до первой записи, поэтому база без транзакций никогда не остаётся с половиной каскада. |
| Связанных подзапросов | Пишет `EXISTS`, связанный равенствами колонок, как проверку принадлежности; любой другой связанный подзапрос отклоняет до отправки. |
| Внешних ключей | Выполняет `on_delete` и `PROTECT` в Python; с `checks_constraints_before_write` проверяет цель связи до записи. |
| Ограничений уникальности | Не создаёт их; вставка-или-обновление ищет конфликт только по первичному ключу — с `checks_constraints_before_write` объявленная уникальность проверяется до записи, а вставка-или-обновление сначала читает конфликтующие строки. |
| `RETURNING` | `UPDATE` не читает изменённые вычисляемые колонки; ключ, который генерирует база, в объект не читается; значения `db_default` читаются `SELECT` после `INSERT` — с `returns_rows_by_reading` `returning()` читает строки по ключам. |
| Блокировок строк | Отклоняет `select_for_update()` — или блокирует строки по ключам через клиента транзакции (`locks_rows_by_key`). |
| Отложенных ограничений | `TableClearing.defer_cascade_foreign_keys()` даёт `UnSupportedError` — настоящее удаление через защиту `PROTECT` в том же каскаде нельзя отложить. |
| Первичного ключа | Поддерживает модели без ключа, как описано выше. |

`Features.checks_foreign_keys_per_cascade_step`, `checks_restrict_at_statement_end` и
`cascade_depth_limit` описывают, как ведёт себя собственный каскад базы, чтобы hare знал, когда
откладывать его или выполнять самому. Клиент, у которого в возможностях задан `cascade_depth_limit`,
даёт `CascadeDepthLimitError` из `hare.exceptions` — или свой подкласс, как
`SqliteTriggerRecursionLimitError` у SQLite, — когда база останавливает каскад на этой глубине;
`delete()` перехватывает её, откатывает попытку и проходит каскад в Python.

## <a id="testing"></a>12. Тестирование

Собственный набор тестов hare работает на диалекте: установите пакет (его точка входа регистрирует
драйвер) и направьте на него `HARE_TEST_DB` (`colstore+colstore-client://.../test_{}`, место `{}` заполняется при
каждом запуске) — выполнится каждый тест, который допускают возможности диалекта. Тест того, чего у
базы может не быть, помечается нужными ему возможностями:

```python
from hare.contrib.test import requires_features

@requires_features(supports_transactions=True)
async def test_rollback(db): ...

@requires_features(supports_foreign_keys=True)
async def test_cascade_in_the_database(db): ...
```

`tests/test_dialect_contract.py` проверяет каждый зарегистрированный диалект и драйвер: имена, схемы,
возможности, то, что каждая часть построена для своего диалекта, реестры, редактор схемы, чтение
схемы и то, что SQL строится одинаково при повторе. Между тестами база очищается через
`TableClearing.clear_tables()`.

## <a id="observability"></a>13. Наблюдение и скорость

`otel_system_name` — значение `db.system.name` в OpenTelemetry (`postgresql`, `clickhouse` или `other_sql` для базы, которой OpenTelemetry имени не даёт). Чтение
строк на Rust зависит от `native_python_types` клиента, а не от имени диалекта. Каждая команда,
которую выполняет клиент, доходит до [`Observers`](../observability/observers.ru.md) —
`QueryExecuted`, обёртки запросов, журнал медленных запросов — через декоратор, описанный в разделе
[о клиенте](#client); клиент, который его пропускает, невидим для `capture_queries`, поиска N+1 и
OpenTelemetry.

## <a id="checklist"></a>Проверочный список

- [ ] Подкласс `Dialect`: имя, `otel_system_name`, `features` и части, которые база записывает иначе, —
      `build_literals()`, `build_parameters()`, `build_renderers()`, `build_clauses()`,
      `build_transactions()`, `build_types()`, `build_filter_operators()`.
- [ ] Класс запроса с `SQL_CONTEXT = dialect.sql_context`.
- [ ] Клиент: команды, перевод ошибок, `features` из возможностей диалекта и драйвера; транзакции,
      если они есть у базы.
- [ ] Драйвер: имя, схемы, учётные данные, классы клиентов; регистрация при импорте.
- [ ] Точка входа `hare.dialects`.
- [ ] Шаблоны и методы класса редактора схемы, параметры хранения таблицы, чтение схемы — сколько нужно базе.
- [ ] Набор тестов hare проходит с `HARE_TEST_DB`, включая `tests/test_dialect_contract.py`.
