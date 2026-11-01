# Создание диалекта: драйвер и клиент

Шаги 1 и 2 [создания диалекта](index.ru.md): подключение через драйвер базы и клиент, который выполняет
на нём запросы.

## <a id="driver"></a>1. Подключение: драйвер

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
| `connection_options`, `strict_query_parameters` | Настройки, которые принимает подключение, каждая объявлена один раз как `ConnectionOption` — см. [`connection_options`, `strict_query_parameters`](#connection-options-strict-query) |
| `get_client_class(credentials)` | Класс клиента для подключения — его можно выбрать по учётному параметру и убрать этот параметр (`install_regexp_functions` у SQLite). |
| `get_client_classes()` | Все классы клиентов, включая клиентов транзакций. |
| `is_retryable(error)` | Означает ли ошибка драйвера, что база прервала транзакцию из-за параллельной (ошибка сериализации, взаимная блокировка); для неё hare даёт `TransactionRetryError`. |
| `get_url_path(url)`, `get_testing_path(path, reuse_databases)` | Чтение пути из адреса и путь, к которому подключается запуск тестов (место `{}` из `HARE_TEST_DB`, заполненное новым именем). |

### <a id="connection-options-strict-query"></a>`connection_options`, `strict_query_parameters`

Настройки, которые принимает подключение; каждая объявляется один раз как
`ConnectionOption(name, value_type, *, minimum, maximum, positive, power_of_two, choices)`, где тип
значения — `ConnectionOptionType.WHOLE_NUMBER`, `SECONDS`, `BOOLEAN`, `CHOICE` или `TEXT`. Настройка
проверяется одинаково, пришла ли она с типом из `credentials` словаря настроек или текстом из параметра
адреса (`?connect_timeout=5`): клиент в своём `__init__` вызывает `options.read(settings)`, чтобы забрать
известные настройки, проверенные по типу и диапазону, и
`options.raise_for_unknown(settings, driver_name)`, чтобы отклонить настройку с опечаткой. Разбор вручную
не нужен.

## <a id="client"></a>2. Выполнение команд: клиент

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
`acquire_connection()` (контекстный менеджер, выдающий сырое соединение) и методы выполнения команд —
весь контракт между hare и драйвером:

| Метод | Что выполняет |
|---|---|
| `execute(query, values=None, *, returns_rows=None, rows_by_position=False) -> StatementResult` | Одну команду — см. [`execute()`](#execute) |
| `execute_many(query, values)` | Одну команду по разу на каждую строку параметров; ничего не возвращает. |
| `execute_script(query)` | Сценарий из нескольких команд, написанный пользователем (`execute_script()`, `RunSQL`), передаётся как есть. Сценарий без команд — пустой или из одних комментариев — на каждом встроенном драйвере ничего не выполняет. |
| `execute_built_script(query)` | Сценарий, построенный hare, — его команды схемы — так, как их выполняет соединение; по умолчанию `execute_script()`. Клиент ClickHouse отправляет каждую его команду на кластер соединения (`ON CLUSTER`), чего сценарий пользователя никогда не получает. |
| `execute_described(query, values=None) -> DescribedResult` | Одну команду вместе с именами колонок (`columns`, `rows` кортежами, `row_count`) — чтобы показать любой результат таблицей. |
| `copy(table, columns, records, column_types)` | Массовую загрузку, если у базы есть такой протокол (`Features.supports_copy`). Наблюдателям она сообщается под командой, которую для неё даёт диалект, — `QueryClauses.get_bulk_load_statement_sql()`. |
| `_driver_stream_batches(query, values, chunk_size)` | Асинхронный генератор партий строк (списков) поверх курсора на стороне сервера, при `Features.supports_streaming`, — у клиента транзакции; `chunk_size` 0 — собственный размер партии драйвера. Публичные `stream_batches()` и `stream()` принадлежат базовому классу: они добавляют метки, оборачивают запрос и сообщают о нём. |

### <a id="execute"></a>`execute()`

Одну команду. `StatementResult(row_count, rows, inserted_id=None, description=None)`
(`hare.dialects.base.results`): строки, которые она вернула, — каждая читается по имени колонки, — и
число строк, которые она изменила, для записи без `RETURNING`. `returns_rows` говорит, возвращает ли
команда строки (`SELECT`, запись с `RETURNING`); при `None` драйвер определяет это по тексту SQL сам.
`rows_by_position=True` передаёт читатель, берущий строки только по позиции, при
`Features.supports_positional_rows`: драйвер может тогда отдать самые дешёвые строки, читаемые по
позиции, — обычные кортежи — с DB-API `description` колонок, из которого читает
`StatementResult.column_names` (без него — из имён первой строки). hare передаёт его для своих чтений и
для команд `UPDATE`/`DELETE`, считающих свои строки. Через него идёт каждое чтение и каждая запись ORM —
отдельного метода для вставки или для чтения словарями реализовывать не нужно (`execute_dicts()` построен
на нём; задайте `row_to_dict`, если для корректности или скорости нужно преобразование,
отличающееся от `dict(row)`). Оно сохраняет точное имя и значение каждой колонки: SQLite соединяет
имена со значениями по позициям, поскольку поиск по имени в его строках не различает `a` и `A`.
Потоковое чтение объединённых запросов тоже использует это преобразование.

`values` команды — это `list` или его подкласс `QueryParameters`
(`hare.sql.terms.parameters.query_parameters`), если среди значений есть значение поля с
`sensitive=True`: его `repr()` показывает такие значения как `<hidden>`, поэтому клиент, который пишет
параметры в лог через `%s`, скрывает их без лишних действий. Драйверу, который не принимает подклассы
списка, передаётся `list(values)`.

`native_python_types` — набор типов Python, которые драйвер возвращает уже в нужном полю виде: для поля
такого типа чтение строк пропускает преобразование.

Каждый метод выполнения переводит собственные исключения драйвера в исключения hare —
`DBConnectionError`, `IntegrityError` и `OperationalError` или `TransactionRetryError` через
`_get_operational_error()`, который спрашивает `is_retryable()` драйвера, — и сообщает о вызове
[наблюдателям и обёрткам запросов](../../observability/observers.ru.md). И то и другое делает один
декоратор, `DatabaseClient.translate_exceptions`, которым класс клиента диалекта оборачивает методы
выполнения своих драйверов (`@ColstoreClient.translate_exceptions`). Он добавляет активные метки запроса
(`DatabaseClient.get_tagged_query_arguments()`), выполняет вызов внутри обёрток запросов, если они
установлены (`Observers.run_wrapped(QueryCall(...), proceed)`), сообщает о нём наблюдателям, когда он
завершился, успешно или нет (`Observers.record_query()`), и выбрасывает вместо исключения драйвера то,
что вернёт `translate_driver_error(client, error, sql, parameters, args, is_query_executing)` клиента, —
единственный метод, который клиент диалекта переопределяет, чтобы перевести исключения своего драйвера.
Настройки, которые читает декоратор, — атрибуты класса клиента, читаемые один раз при оборачивании
метода: `rejected_sql_character` (символ, на котором команда отклоняется до отправки),
`checks_aborted_transactions` и `aborted_transaction_message` (команда в транзакции, которую база
прервала, отклоняется), `runs_statement_options` (`command_timeout`, `password_provider`, пулер
транзакций, повторы чтения). Декоратор работает на каждой команде, поэтому читает `is_transaction_client`
клиента (истинно у каждого `TransactionClient`), а не вызывает `isinstance()`.

Когда соединение открыто, клиент вызывает `_post_connect()`, который читает версию сервера из
`get_server_version()` — `(major, minor, ...)`, без наблюдения за запросами; значение по умолчанию `None`
пропускает проверку. Сервер старше `Dialect.minimum_server_version` отклоняется с `UnSupportedError`, и
соединение закрывается; `Dialect.get_server_version_features(version)` возвращает значения `Features`,
которые меняет версия (PostgreSQL до 15 выключает `supports_nulls_distinct` и не перечисляет параметры
`EXPLAIN`, которых у более старого сервера нет), и они применяются только к этому подключению. Обёртка
транзакции делит `features` своего подключения.

База с транзакциями реализует `_get_transaction_client()` — новый клиент подкласса `TransactionClient`
драйвера, который даёт примитивы драйвера (см.
[Транзакции и одновременная работа](transactions.ru.md#transactions)). Контекст транзакции вокруг него —
общий, hare: он забирает ресурсы клиента, начинает транзакцию и на выходе фиксирует или откатывает её.
Команды, выполняемые сразу после `BEGIN`, — уровень изоляции, «только чтение», ограничения времени
команды и ожидания блокировки — дают `TransactionStatements` диалекта; клиент, который сам прерывает
слишком долгую команду без команды диалекта, задаёт `enforces_statement_timeout_itself = True` (клиент
SQLite прерывает запрос).

### <a id="client-hooks"></a>Хуки с поведением по умолчанию

Остальные публичные методы `DatabaseClient` работают с любым драйвером как есть; диалект переопределяет
метод, когда его база или драйвер делают что-то по-своему.

| Метод | Что делает | По умолчанию |
|---|---|---|
| `acquire_from_open_pool() -> (pool, connection)` | Берёт одно соединение пула вместе с пулом, из которого оно взято, — клиент с пулом повторяет попытку на новом пуле, если `Connections.reconnect()` закрыл тот, которого он ждал. | Пул и его соединение. |
| `abandon_connections()` | Выбрасывает соединения, открытые в уже закрытом цикле событий, — закрыть их через него нельзя. | Ничего: драйвер, соединениям которого для закрытия цикл событий не нужен. |
| `check_stream_open()` | Не отдаёт строку потока, которую соединение больше не читает. | Ничего: поток вне транзакции читается до конца или закрывается. |
| `get_result_read_error(error)` | Исключение hare для ошибки, которую драйвер бросает при чтении значения после выполнения команды. | `None`: драйвер раскодирует все значения до возврата команды. |
| `is_unencodable_surrogate_error(error)` | Не смог ли драйвер закодировать текст с одиночным суррогатом. | Собственная ошибка Python или ошибка драйвера, вызванная ею. |
| `raise_rejected_sql_character(sql)` | Бросает исключение для команды с `rejected_sql_character`. | Обязателен для клиента, задающего `rejected_sql_character`. |
| `enforces_foreign_keys()` | Соблюдает ли база внешние ключи на этом подключении сейчас — где нет, жёсткое удаление каскадирует в Python. | `Features.supports_foreign_keys`. |
| `get_address()` | Адрес сервера для отчётов (`ConnectionFailed`). | Пустая строка. |
| `get_pool_bounds() -> (least, most)` | Сколько соединений держит пул по настройкам — для `get_pool_status()` пула, который не открылся. | Обязателен при `Features.supports_pool_status`. |
| `ping(timeout=5.0)` | `SELECT 1` с ограничением `timeout`; `False` при ошибке соединения, ошибке выполнения или истечении времени. | Одинаков для всех драйверов. |
| `create_independent_client(settings=None)` | Новое подключение к той же базе, отдельно от этого и в той же схеме арендатора; закрывает его вызывающий. | Строится из настроек подключения. |
| `get_server_client()` | Клиент самого сервера, в обход пулера, через который идёт подключение (`direct_host`). | Этот клиент. |
| `lock_timeout_session(seconds)` | Клиент на одном соединении, каждая команда которого ждёт блокировку не дольше `seconds`, — для миграции вне транзакции. | Бросает `UnSupportedError`. |
| `get_tenant_client()` | Клиент, через который работает текущая область арендатора, — клиент схемы арендатора при схеме на арендатора. | Этот клиент или клиент схемы. |
| `get_schema_client(schema_name)` | Клиент схемы одного арендатора, создаваемый при первом обращении. | Без схем на арендатора бросает `UnSupportedError`. |
| `enter_tenant_schema(schema_name) -> state` / `leave_tenant_schema(state)` | Выполняет остаток транзакции в схеме другого арендатора и возвращается — удаление, доходящее до строк каждой схемы арендатора. | Без схем на арендатора бросают `UnSupportedError`. |
| `close_tenant_clients()` | Закрывает клиентов схем арендаторов. | Закрывает каждый открытый клиент схемы. |

### <a id="several-drivers"></a>Один диалект, несколько драйверов

Диалект, у которого есть или может появиться больше одного драйвера, делит клиент на две части — так
устроены диалекты самого hare:

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
  `create_connection()`, `acquire_connection()`, методы выполнения команд, курсоры и потоковое чтение,
  примитивы транзакции (`_driver_commit()`, `_driver_savepoint()`, ...), `get_server_version()` и
  `Features`, которые добавляет библиотека (`AiosqliteClient.features` добавляет предел числа параметров
  сборки `sqlite3`). Его клиент транзакции наследует общий клиент транзакции первым:
  `AiosqliteTransactionClient(SqliteTransactionClient, AiosqliteClient)`.
- **Драйвер** хранит одно имя — свою схему database URL `<dialect>+<driver>`: `sqlite+aiosqlite://`,
  `postgresql+asyncpg://`, `clickhouse+clickhouse-connect://`, `clickhouse+clickhouse-driver://`. Простая
  схема диалекта принадлежит собственному движку hare для этого диалекта — `postgresql://` (rust_pg);
  другие драйверы её не берут.
- **Пул.** Клиент, чей пул живёт в Python, считает в `PoolStatistics` hare. Драйвер, чей пул живёт вне
  Python, возвращает из `create_pool_statistics()` свой объект с теми же методами и сообщает занятость
  пула через `get_pool_occupancy()` (при `Features.supports_pool_status`).
- **Нативный драйвер.** Общие для всех нативных драйверов части `rust.native` не принадлежат ни одной
  базе: `rust.native.pool` (`PoolStatistics`, `set_pool_metrics_enabled()`, взятие соединения из пула
  deadpool со счётчиками) и мост к asyncio, через который завершаются его futures. Новый нативный драйвер
  для SQLite или ClickHouse использует их так же, как `rust.native.pg`, и становится ещё одним драйвером
  диалекта — общий клиент остаётся прежним.

### <a id="password-provider"></a>Меняющийся пароль

Клиент базы, пароли которой меняются, поддерживает `password_provider` так же, как клиент PostgreSQL
([Меняющийся пароль](../../dialects/postgresql/connecting.ru.md#rotating-credentials)):

- Его настройки включают `PASSWORD_PROVIDER_OPTIONS` (`hare.dialects.base.connection.constants`) —
  `password_provider` и `password_refresh_seconds`, проверяемые как любая настройка. Диалект без них
  отвергает обе как неизвестные.
- Клиент задаёт `self.password_provider = PasswordProvider.from_settings(settings, password)`
  (`hare.dialects.base.client.password_provider`) — None без функции, `ConfigurationError`, если рядом с
  функцией указан постоянный пароль.
- Драйвер, который сам открывает каждое соединение, спрашивает там `await self.password_provider.get()` —
  сохранённый пароль, запрошенный заново, когда он старше срока обновления. Драйвер, чей пул открывает
  соединения сам, переопределяет `apply_password(password)`, чтобы передать пулу новый пароль, запускает
  `password_provider.start_refreshing(self.apply_password)` после открытия пула и останавливает его через
  `await password_provider.stop_refreshing()` при закрытии пула.
- Когда сервер отказывает в пароле, `await client.renew_password()` сразу спрашивает функцию и применяет
  ответ; повторить отвергнутую команду клиент может, только если знает, что она не дошла до сервера.

### <a id="shell-command"></a>Интерактивный клиент

`hare dbshell` запускает то, что возвращает `await client.get_shell_command()`, — `ShellCommand`
(`hare.dialects.base.client.declarations`): программу с аргументами и переменные, добавляемые в её
окружение. Пароль передаётся через окружение, никогда не в аргументах, которые видят другие пользователи
машины. Значение по умолчанию `None` заставляет `hare dbshell` отказать диалекту; подключение, которое не
может открыть другая программа (база в памяти), бросает `UnSupportedError` с причиной.

### <a id="features"></a>Возможности

`Features` (`hare.dialects.base.features`) — единственное место, где hare спрашивает, что поддерживают
база и её драйвер. Диалект объявляет, что поддерживает база (`Dialect.features`), клиент начинает с них и
задаёт то, что добавляет его драйвер, а версия сервера меняет их для отдельного подключения. Ядро читает
`connection.features` там, где у него есть подключение, и `dialect.features` там, где есть только диалект
(команды схемы для файла миграции, список операторов фильтров).
Возможность, которую читают только части самого диалекта, — поле его собственного подкласса `Features`
(как `SqliteFeatures`, `PostgresqlFeatures`, `ClickhouseFeatures`), экземпляр которого — его
`Dialect.features`; ядро ни одно такое поле не читает.

| Возможность | Что делает hare, если её нет |
|---|---|
| `supports_transactions` | `Transactions.atomic()`/`atomic()` дают `UnSupportedError`; собственные записи hare из нескольких команд (каскад, `add()` у «многие-ко-многим», `bulk_create()` партиями) выполняют команды по одной. |
| `supports_savepoints` | Вложенный `atomic()` присоединяется к транзакции, в которую вложен; ошибка, покинувшая вложенный блок, заставляет транзакцию при завершении откатиться и дать `TransactionManagementError`, даже если внешний блок перехватил ошибку. |
| `supports_generated_keys` | Модель, чей первичный ключ генерирует база (по умолчанию `IntField(primary_key=True)`), отклоняется с `ConfigurationError` при привязке к соединению — дайте ей ключ, который задаёт приложение (`UUIDField(primary_key=True, default=uuid.uuid7)`). Журнал миграций самого hare использует ключ `(app, name)`. |
| `takes_keys_before_insert` | Включите, если ключи берутся из серии до записи строк: hare вызывает `take_generated_keys(model, count)` клиента для строк без ключа и пишет их с ключами; `synchronize_key_series(model)` сдвигает серию за наибольший ключ таблицы (`SynchronizeKeySeries`). |
| `supports_unique_constraints` | Модель, объявившая уникальность (`unique=True`, `UniqueConstraint`, `Index(unique=True)`), отклоняется с `ConfigurationError` при привязке; миграция, объявляющая уникальность, — с `UnSupportedError` до её первой команды. |
| `supports_foreign_keys` | hare сам выполняет каждое `on_delete`, а связь с `db_constraint=True` — в том числе автоматическая промежуточная таблица связи многие-ко-многим — отклоняется с `ConfigurationError` при привязке её модели. |
| `references_other_schemas` | Связь с `db_constraint=True` на модель другой схемы отклоняется с `ConfigurationError` при привязке её модели. |
| `reports_written_row_count` | `StatementResult.row_count` записи у клиента — `None`: `update()`, `delete()` и `bulk_update()` возвращают `None`, запись пропавшей строки не обнаруживается, а модель с `Meta.optimistic_lock_field` отклоняется с `ConfigurationError` при привязке. |
| `supports_insert_conflicts` | `bulk_create(ignore_conflicts=..., update_fields=..., on_conflict=..., on_conflict_constraint=...)` даёт `UnSupportedError` до отправки SQL. |
| `supports_row_updates` | Каждый `UPDATE` — `save()` сохранённой строки, `QuerySet.update()`, `bulk_update()`, `SET_NULL`/`SET_DEFAULT` каскада — даёт `UnSupportedError` до отправки, а каскад, которому он нужен, ничего не пишет — см. [`supports_row_updates`](#supports-row-updates) |
| `supports_row_deletes` | Каждый `DELETE` даёт `UnSupportedError` до отправки, а каскад, которому он нужен, ничего не пишет. |
| `rewrites_correlated_exists` | Включите, если база выполняет связанные подзапросы, но считает связанный `EXISTS` неверно: такой `EXISTS` остаётся в форме `IN`. |
| `supports_ordered_correlated_subqueries` | Связанный подзапрос, который сам сортирует или срезает свои строки, даёт `UnSupportedError` до отправки команды. |
| `orders_by_correlated_subqueries` | Запрос со связанным подзапросом в `ORDER BY` или выбранным рядом с `WHERE` выбирает строки в производной таблице — условия и ключи сортировки становятся её скрытыми колонками — и фильтрует, сортирует и срезает их снаружи; `UPDATE`/`DELETE` со связанным подзапросом даёт `UnSupportedError`. |
| `supports_correlated_subqueries` | `EXISTS`, связанный равенством колонок, записывается как `(outer columns) IN (SELECT inner columns ...)` — см. [`supports_correlated_subqueries`](#supports-correlated-subqueries) |
| `can_rollback_ddl` | Миграция не оборачивается в транзакцию. |
| `supports_select_for_update` | `select_for_update()` и `RowChanges.get_for_update()` дают `UnSupportedError` при выполнении запроса; `update_or_create()` обходится без блокировки. |
| `supports_select_for_no_key_update` | `select_for_update(no_key=True)` берёт обычную блокировку `FOR UPDATE`. |
| `supports_select_for_share` / `supports_select_for_key_share` | `select_for_update(share=True)` / `(key_share=True)` дают `UnSupportedError` при выполнении запроса. |
| `supports_update_limit_order_by` | `QuerySet.update()`/`delete()` запроса со срезом дают `QueryError` до отправки SQL; с ним срез без смещения пишется собственными `ORDER BY ... LIMIT` команды. |
| `supports_returning` | `INSERT` ничего не запрашивает обратно, а `returning()` и `bulk_create(returning=True)` дают `UnSupportedError` — см. [`supports_returning`](#supports-returning) |
| `guarantees_returning_order` | Многострочный `INSERT ... RETURNING` сопоставляется со своими объектами по первичному ключу, а не по позиции. |
| `supports_posix_regex` | Фильтр с `posix_regex`/`iposix_regex` даёт `UnSupportedError` до выполнения запроса. |
| `cuts_like_pattern_at_null_byte` | Включите там, где `LIKE` читает шаблон только до первого нулевого байта: текстовый оператор фильтра с таким шаблоном даёт `ValidationError`. |
| `supports_two_phase_commit` | `Transactions.distributed()` отклоняет подключение. |
| `supports_listen_notify` | `NotificationListener.start()`/`run()` дают `UnSupportedError`, а `ListenNotifyWakeup` outbox даёт `ConfigurationError`, когда подаёт сигнал. |
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
| `max_bulk_update_rows` | `bulk_update()` пишет не больше стольких строк одной командой. |
| `supports_nulls_distinct` | `UniqueConstraint(nulls_distinct=...)` даёт `UnSupportedError` до отправки команды создания. |
| `explain_options` | Параметр `EXPLAIN`, которого нет в списке, даёт `UnSupportedError` до отправки команды. |
| `supports_schemas` | Миграции не создают и не удаляют схем; таблица модели со схемой пишется как есть и должна существовать. Методы редактора схемы `get_index_name_sql()`, `get_indexed_table_sql()` и `get_referenced_table_sql()` пишут имена, которые получают индекс, триггер и внешний ключ, в собственной форме базы. |
| `supports_distinct_on` | `distinct(*fields)` даёт `UnSupportedError` при построении запроса. |
| `supports_grouping_sets` | `group_by(Rollup/Cube/GroupingSets(...))` и `Grouping()` дают `UnSupportedError`. |
| `supports_lateral` | `Lateral(...)` даёт `UnSupportedError`. |
| `supports_table_sample` | `QuerySet.sample(...)` даёт `UnSupportedError`. |
| `supports_asof_join` | `AsofJoin(...)` даёт `UnSupportedError`. |
| `supports_array_join` | `ArrayJoin(...)` даёт `UnSupportedError`. |
| `supports_dictionaries` | `Meta.dictionaries` даёт `UnSupportedError` до DDL; иначе — `dictionaries_class` редактора схемы (`Dictionaries`: `get_dictionary_create_sqls()`, `drop_dictionary()`, `alter_dictionary()`, `rename_dictionary()`, `reload_dictionary()`). |
| `supports_merge` | `QuerySet.merge()` даёт `UnSupportedError`. |
| `supports_merge_returning` | `merge().returning()` даёт `UnSupportedError`. |
| `supports_merge_not_matched_by_source` | `merge().when_not_matched_by_source()` даёт `UnSupportedError`. |
| `supports_pool_status` | `get_pool_status()` даёт `UnSupportedError`, а пулы клиента не попадают в `Connections.get_pool_statuses()`. |
| `sorts_nulls_first` | Где по умолчанию оказывается NULL при сортировке — `nulls_first`/`nulls_last` добавляют фразу, только если она меняет порядок. |
| `divides_integers_exactly` | Включите, если `/` двух целых даёт точное частное: поле вывода выражения — `FloatField` вместо целого поля. |
| `computes_integer_statistics_exactly` | Включите, если база считает `AVG`, `STDDEV` и `VARIANCE` от целых точными десятичными числами: результат читается как `Decimal`; без этого hare читает его как число с плавающей точкой. |
| `computes_decimal_statistics_exactly` | Включено по умолчанию. Выключите, если база считает `AVG`, `STDDEV` и `VARIANCE` от десятичных числами с плавающей точкой: тогда hare читает результат как такое число, а не `Decimal`. |
| `keeps_enum_columns` | Включите, если колонка поля-перечисления — перечислимого типа, который хранит только элементы: тогда hare не добавляет `CHECK` по значениям элементов. |
| `supports_conflict_constraint_names`, `supports_conflict_where` | `bulk_create(on_conflict_constraint=...)` / `conflict_where=` дают `UnSupportedError`. |
| `matches_ordering_to_grouping_by_sql` | Задайте его, если выражение сортировки, по которому идёт и группировка, нужно записать точно как в `GROUP BY`. |
| `supports_virtual_generated_columns` | `GeneratedField(stored=False)` даёт `UnSupportedError`. |
| `supports_text_search_configurations` | Конфигурация поиска, `SearchVector` как значение, лексемы, веса меток, `normalization` и `cover_density` у `SearchRank` и параметры фрагментов `SearchHeadline` дают `UnSupportedError`. |
| `supports_full_text_index` | `FullTextIndex` и веса полей у `SearchRank` дают `UnSupportedError`. |
| `supports_vector_search` | Расстояния векторов и `__nearby` дают `UnSupportedError`. |
| `supports_tenant_schemas` | `TenantSchemas.create()`/`drop()`/`get_tenants()` дают `UnSupportedError`; нужны `get_tenant_client_settings(schema_name)` клиента (путь поиска схемы арендатора), параметр подключения `tenant_schema_template` и `fetch_schema_names()` интроспектора. |
| `supports_spatial` | Операторы, пути, функции и агрегаты `hare.gis` дают `UnSupportedError`; зарегистрируйте рендереры `GeometryValue`, `SpatialRelationTerm`, `SpatialFunctionTerm` и имена `SpatialAggregateFunction`, а также тип колонки `GeometryField` и преобразования его значений. |
| `supports_geography` | Операторы и функции `GeometryField(geography=True)` дают `UnSupportedError`. |
| `spatial_reference_ids` | None: geography и пространственный индекс принимают любую SRID. Множество SRID — те, что есть в пространственных метаданных базы, прочитанные при открытии соединения, — и geography или пространственный индекс в другой SRID дают `UnSupportedError`. |
| `supports_ordered_aggregates` | Агрегат с `order_by=`, у которого нет другой формы, даёт `UnSupportedError` (`MakeLine`). |
| `supports_uuid_v7` | `db_default=UuidV7()` даёт `UnSupportedError` до DDL. |
| `supports_without_overlaps` | `without_overlaps=True` у `UniqueConstraint`/`CompositePrimaryKey` даёт `UnSupportedError`. |
| `supports_returning_old_new` | `returning(old=...)` даёт `UnSupportedError`; иначе `QueryClauses.get_old_row_value_sql()`. |
| `supports_json_table` | `JsonTable` даёт `UnSupportedError`; иначе `QueryClauses.get_json_table_sql()` (по умолчанию ISO). |
| `isolation_levels` | Уровни, на которых выполняется транзакция, от самого слабого — см. [Транзакции](transactions.ru.md#transactions). |
| `max_identifier_length` | Наибольшая длина имени в байтах, None — без предела. hare генерирует имена до 63 байт, укорачивая более длинное с хэшем; диалект с меньшим пределом не регистрируется. |
| `cascade_depth_limit`, `checks_foreign_keys_per_cascade_step`, `checks_restrict_at_statement_end` | Как ведёт себя собственный каскад базы — см. [базы без гарантий](guarantees-testing-observability.ru.md#without-guarantees). |

<a id="supports-row-updates"></a>**`supports_row_updates`.** Каждый `UPDATE` — `save()` сохранённой
строки, `QuerySet.update()`, `bulk_update()`, `SET_NULL`/`SET_DEFAULT` каскада — даёт `UnSupportedError`
до отправки, а каскад, которому он нужен, ничего не пишет. Модель с мягким удалением
(`Meta.soft_delete_field`) отклоняется с `ConfigurationError` при привязке; релей outbox отказывается
запускаться.

<a id="supports-correlated-subqueries"></a>**`supports_correlated_subqueries`.** `EXISTS`, связанный с
внешним запросом равенствами колонок, — фильтр или исключение через связь «ко многим», `<m2m>__isnull`,
`Exists(...)` с равенствами `OuterReference`, — пишется как
`(outer columns) IN (SELECT inner columns ...)` с исключёнными NULL: он истинен и ложен ровно там же, где
`EXISTS`. Любой другой связанный подзапрос (`Subquery(...)` с `OuterReference`, `OuterReference`,
сравниваемый не через `=`) даёт `UnSupportedError` до отправки команды; так же отказывается запуск релея
outbox.

<a id="supports-returning"></a>**`supports_returning`.** `INSERT` ничего не запрашивает обратно: ключ,
который генерирует база, приходит собственным путём диалекта (последний rowid SQLite, серия, взятая до
вставки при `takes_keys_before_insert`), — первичный ключ, оставленный `db_default`, отклоняется при
записи строки, — а значения колонок `db_default` и вычисляемых колонок остаются непрочитанными:
поле держит `DatabaseDefault`, вычисляемое поле не загружено, а `refresh_from_db()` читает их.
`UPDATE` не читает изменённые генерируемые колонки, массовая запись не может узнать, какие строки она
действительно вставила, а `returning()` и `bulk_create(returning=True)` дают `UnSupportedError`.

Возможности команд схемы перечислены вместе с [редактором схемы](schema.ru.md#schema-editor). Класс
клиента может получить возможности другого с изменениями:
`AiosqliteClient.features.replace(supports_posix_regex=True)`.

### <a id="executor"></a>Команды уровня моделей

Между моделью и `execute()` нет ничего своего у каждого драйвера: один и тот же конвейер записи строит
каждый `INSERT`, `UPDATE`, вставку-или-обновление и `DELETE` — и для `save()`, и для `bulk_create()`, и
для `QuerySet.update()`, — и одно и то же чтение строк строит объекты из того, что вернул `execute()`.
То, чем базы различаются, спрашивается у частей диалекта и у `Features` подключения: генерируемые колонки
и колонки со значениями по умолчанию базы возвращаются через `INSERT ... RETURNING` при
`supports_returning`; `clauses.get_upsert_inserted_flag_sql()` даёт выражение `RETURNING`, отличающее
строку, вставленную вставкой-или-обновлением, от изменённой (`xmax = 0` в PostgreSQL; при `None` hare
читает существующие ключи перед записью, и только пока есть наблюдатель).
