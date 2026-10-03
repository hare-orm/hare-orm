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

Руководство проходит все части на примере базы, похожей на ClickHouse: колоночной базы без транзакций,
внешних ключей и ограничений уникальности, с именованными местами для параметров, обратными кавычками,
таблицами, которым нужны движок и ключ сортировки, и собственными модификаторами запросов (`FINAL`,
`SAMPLE`).

## Из чего состоит диалект {: #the-parts }

| Часть | Базовый класс | Что решает |
|---|---|---|
| Диалект | `hare.dialects.base.dialect.Dialect` | Признаки и точки расширения синтаксиса, записи типов, операторы фильтров, способы вывода выражений, редактор схемы, чтение схемы. Один объект, общий для всех драйверов и подключений. |
| Драйвер | `hare.dialects.base.driver.Driver` | Имя (`engine` в настройках подключения), схемы адреса и учётные данные, класс клиента, какие ошибки можно повторить. |
| Клиент | `hare.dialects.base.client.DatabaseClient` | Одно подключение: выполнение команд, транзакции, его `Features`. |
| Класс запроса | `hare.sql.queries.Query` / `QueryBuilder` | Выводит команды в контексте диалекта; переопределяет часть запроса, которую база записывает по-своему. |
| Редактор схемы | `hare.dialects.base.schema.editor.BaseSchemaEditor` | Команды создания и изменения схемы и для `generate_schemas()`, и для миграций. |
| Чтение схемы | `hare.inspectdb.introspector.SchemaIntrospector` | Читает существующую схему для `inspectdb` и `hare drift`. Необязательно. |
| Параметры хранения таблицы | `hare.ddl.table_options.TableOptions` | Что принимает `CREATE TABLE` диалекта помимо колонок. Необязательно. |

## Регистрация {: #registering }

Модуль драйвера пакета регистрирует драйвер — а вместе с ним и диалект — при импорте:

```python
# hare_clickhouse/driver.py
from hare.dialects.registry import DialectRegistry

CLICKHOUSE_DRIVER = ClickHouseDriver()
DialectRegistry.register_driver(CLICKHOUSE_DRIVER)
```

и называет этот модуль в группе точек входа `hare.dialects`, чтобы hare импортировал его при первом
поиске драйвера, которого нет среди встроенных, или при получении списка всех драйверов:

```toml
# pyproject.toml пакета hare-clickhouse
[project.entry-points."hare.dialects"]
clickhouse = "hare_clickhouse.driver"
```

После установки пакета адрес подключения `"clickhouse://..."` или настройки подключения с
`"engine": "clickhouse"` его используют. Второй драйвер или диалект под уже занятым именем или схемой
адреса даёт `ConfigurationError`. Регистрация диалекта один раз вызывает его `install()` — место, где
регистрируется то, что он добавляет к собственным классам hare (метод `QuerySet`, часть пути у поля
ядра).

## 1. Подключение — драйвер {: #driver }

```python
class ClickHouseDriver(Driver):
    name = "clickhouse"                       # "engine" в настройках подключения
    dialect = CLICKHOUSE_DIALECT
    url_schemes = ("clickhouse",)             # clickhouse://user:password@host:9000/database
    path_credential = "database"              # что заполняет путь адреса
    authority_credentials = {"hostname": "host", "port": "port", "username": "user", "password": "password"}
    default_credentials = {"port": 9000}
    connection_options = ConnectionOptions(   # hare.dialects.base.connection_options
        ConnectionOption("compression", ConnectionOptionType.BOOLEAN),
        ConnectionOption("connect_timeout", ConnectionOptionType.SECONDS, positive=True, maximum=3600),
        ConnectionOption("max_block_size", ConnectionOptionType.WHOLE_NUMBER, minimum=1, maximum=1_000_000),
    )
    strict_query_parameters = True            # неизвестный ?параметр — ConfigurationError

    def get_client_class(self, credentials):
        return ClickHouseClient

    def get_client_classes(self):
        return (ClickHouseClient,)            # все классы клиентов, включая клиентов транзакций

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

## 2. Выполнение команд — клиент {: #client }

```python
class ClickHouseClient(DatabaseClient):
    driver_name = "clickhouse"
    dialect = CLICKHOUSE_DIALECT
    query_class = ClickHouseQuery
    native_python_types = frozenset({str, int, float, bytes, datetime.datetime, datetime.date, uuid.UUID})
    features = Features(
        supports_transactions=False,
        supports_select_for_update=False,
        supports_update_limit_order_by=False,
        supports_returning=False,
        max_bind_parameters=100_000,
    )
```

Клиент реализует `create_connection(with_db)`, `close()`, `db_create()`, `db_delete()`,
`acquire_connection()` (контекстный менеджер, выдающий сырое соединение) и методы выполнения
команд — весь контракт между hare и драйвером:

| Метод | Что выполняет |
|---|---|
| `execute(query, values=None, *, returns_rows=None) -> StatementResult` | Одну команду. `StatementResult(row_count, rows)` (`hare.dialects.base.results`): строки, которые она вернула, — каждая читается по имени колонки, а при `Features.supports_positional_rows` и по позиции, — и число строк, которые она изменила, для записи без `RETURNING`. `returns_rows` говорит, возвращает ли команда строки (`SELECT`, запись с `RETURNING`); при `None` драйвер определяет это по тексту SQL сам. hare передаёт его для своих чтений и для команд `UPDATE`/`DELETE`, считающих свои строки. Через него идёт каждое чтение и каждая запись ORM — отдельного метода для вставки или для чтения словарями реализовывать не нужно (`execute_dicts()` построен на нём; задайте `row_to_dict`, если строки драйвера превращаются в словарь быстрее, чем через `dict(row)`). |
| `execute_many(query, values)` | Одну команду по разу на каждую строку параметров; ничего не возвращает. |
| `execute_script(query)` | Сценарий из нескольких команд, передаётся как есть. |
| `execute_described(query, values=None) -> DescribedResult` | Одну команду вместе с именами колонок (`columns`, `rows` кортежами, `row_count`) — чтобы показать любой результат таблицей. |
| `copy(table, columns, records, column_types)` | Массовую загрузку, если у базы есть такой протокол (`Dialect.supports_copy`). |
| `_driver_stream(query, values, chunk_size)` | Асинхронный генератор поверх курсора на стороне сервера, при `Features.supports_streaming`, — у клиента транзакции. Публичный `stream()` принадлежит базовому классу: он добавляет метки, оборачивает запрос и сообщает о нём. |

`native_python_types` — набор типов Python, которые драйвер возвращает уже в нужном полю виде: для
поля такого типа чтение строк пропускает преобразование.

Каждый метод выполнения переводит собственные исключения драйвера в исключения hare —
`DBConnectionError`, `IntegrityError` и `OperationalError` или `TransactionRetryError` через
`_get_operational_error()`, который спрашивает `is_retryable()` драйвера, — и сообщает о вызове
[наблюдателям и обёрткам запросов](../observability/observers.ru.md). И то и другое делает один
декоратор, которым класс клиента оборачивает свои методы выполнения, как
`SqliteClient.translate_exceptions`: `DatabaseClient.get_tagged_query_arguments(args, kwargs,
method_name)` добавляет активные метки запроса и отдаёт SQL с параметрами;
`Observers.run_wrapped(QueryCall(...), proceed)` выполняет вызов внутри обёрток запросов, если они
установлены; `Observers.record_query(sql, params, start_time, error, connection_name)` сообщает о
нём, когда он завершился, успешно или нет. Декоратор работает на каждой команде, поэтому читает
`is_transaction_client` клиента (истинно у каждого `TransactionClient`), а не вызывает `isinstance()`.

Когда соединение открыто, клиент вызывает `_post_connect()`, который читает версию сервера из
`get_server_version()` — `(major, minor, ...)`, без наблюдения за запросами; значение по умолчанию
`None` пропускает проверку. Сервер старше `Dialect.minimum_server_version` отклоняется с
`UnSupportedError`, и соединение закрывается; `Dialect.get_server_version_features(version)` возвращает
значения `Features`, которые меняет версия (PostgreSQL до 15 выключает `supports_nulls_distinct`), и
они применяются только к этому подключению. Обёртка транзакции делит `features` своего подключения.

База с транзакциями реализует `_get_transaction_client()` — новый клиент подкласса
`TransactionClient` драйвера, который даёт примитивы драйвера (см.
[Транзакции и одновременная работа](#transactions)). Контекст транзакции вокруг него — общий,
hare: он забирает ресурсы клиента, начинает транзакцию и на выходе фиксирует или откатывает её.
Параметры транзакции несут уровень изоляции (`Dialect.get_isolation_level_sql()`), режим «только
чтение» и ограничение времени команды (`_get_transaction_restriction_statements()`).

### Возможности подключения {: #features }

`Features` (`hare.dialects.base.features`) — то, что поддерживают база и драйвер одного подключения;
они фиксируются при создании подключения и читаются как `connection.features`:

| Возможность | Что делает hare, если её нет |
|---|---|
| `supports_transactions` | `Transactions.atomic()`/`atomic()` дают `UnSupportedError`; собственные записи hare из нескольких команд (каскад, `add()` у «многие-ко-многим», `bulk_create()` пачками) выполняют команды по одной. |
| `can_rollback_ddl` | Миграция не оборачивается в транзакцию. |
| `supports_select_for_update` | `select_for_update()` даёт `UnSupportedError` при выполнении запроса; `update_or_create()` пропускает блокировку. |
| `supports_select_for_no_key_update` | `select_for_update(no_key=True)` берёт обычную блокировку `FOR UPDATE`. |
| `supports_update_limit_order_by` | `QuerySet.update()`/`delete()` запроса со срезом выбирают строки через подзапрос `pk IN (SELECT ...)`. |
| `supports_returning` | `INSERT` ничего не запрашивает обратно: ключ, который генерирует база, в объект не читается (давайте таким моделям ключ, который задаёт приложение), а значения колонок с `db_default` читаются `SELECT` по первичному ключу. `UPDATE` не читает изменённые вычисляемые колонки, а массовая запись не знает, какие строки она действительно вставила. |
| `supports_posix_regex` | Фильтр с `posix_regex`/`iposix_regex` даёт `UnSupportedError` до выполнения запроса. |
| `supports_two_phase_commit` | `Transactions.distributed()` отклоняет подключение. |
| `supports_listen_notify` | Очередь исходящих событий не отправляет `NOTIFY` о новом событии. |
| `supports_streaming` | `QuerySet.stream()` не поддерживается. |
| `inline_comments` | Комментарии таблиц и колонок записываются в `CREATE TABLE`, а не через `COMMENT ON`. |
| `supports_positional_rows` | Строки читаются только по имени колонки — запросы `select_related()` не идут быстрым путём по номерам. |
| `execute_many_scales_poorly` | Массовые записи отправляются командами на много строк, а не через `executemany()`. |
| `max_bind_parameters` | Массовые записи, предзагрузка и каскады делят свои команды, чтобы не превысить его. |
| `supports_nulls_distinct` | `UniqueConstraint(nulls_distinct=...)` даёт `UnSupportedError` до отправки команды создания. |

Класс клиента может получить возможности другого с изменениями:
`SqliteClient.features.replace(supports_posix_regex=True)`.

### Команды уровня моделей {: #executor }

Между моделью и `execute()` нет ничего своего у каждого драйвера: один и тот же конвейер записи
строит каждый `INSERT`, `UPDATE`, upsert и `DELETE` — и для `save()`, и для `bulk_create()`, и для
`QuerySet.update()` — через класс запросов диалекта, и одно и то же чтение строк строит объекты из
того, что вернул `execute()`. То, чем базы различаются, спрашивается у диалекта и у `Features`
подключения: генерируемые колонки и колонки со значениями по умолчанию базы возвращаются через
`INSERT ... RETURNING` при `supports_returning`; `Dialect.get_upsert_inserted_flag_sql()` даёт
выражение `RETURNING`, отличающее вставленную upsert строку от изменённой (`xmax = 0` в PostgreSQL;
при `None` hare читает существующие ключи перед записью, и только пока есть наблюдатель).

## 3. Параметры, имена и литералы — диалект {: #dialect }

```python
class ClickHouseDialect(Dialect):
    name = "clickhouse"
    otel_system_name = "clickhouse"           # db.system.name в OpenTelemetry
    placeholder_template = "{{p{}}}"         # {p1}, {p2}, ...
    identifier_quote_char = "`"
    alias_quote_char = "`"
    max_identifier_length = None
    supports_schemas = True                   # database.table
```

| Член класса | Что задаёт |
|---|---|
| `placeholder_template`, `get_placeholder(index)` | Место для параметра запроса; `{}` — его номер, начиная с 1 (`$1` в PostgreSQL, `?` в SQLite). Кэш вида запроса подставляет значения по позиции, поэтому с ним работает любой вид мест для параметров. |
| `get_parameter_cast_type(value, position)`, `get_field_parameter_cast_type(field)`, `get_json_object_value_cast_type(value, value_type)`, `get_cast_parameter_sql(sql, value)`, `get_concatenated_argument_sql(sql, argument)` | Тип, к которому приводится значение-параметр там, где его тип ничто не задаёт: по месту в запросе (`ParameterPosition`: ветка `CASE`, выбранная или сравниваемая константа, аргумент функции), как значение колонки, как значение объекта JSON, в готовом SQL или как аргумент конкатенации. По умолчанию `None` или SQL без изменений; PostgreSQL, который определяет тип параметра только по окружению, добавляет приведение. |
| `binds_array_parameters` | Передаётся ли список одним параметром-массивом (параметр `RawSQL` вроде `= ANY(%s)`); без него такой параметр вызывает `UnSupportedError`. |
| `get_decimal_overflow_predicate_sql(column, max_digits, decimal_places)` | Условие `WHERE`, по которому `AlterField` находит хранимые числа, не помещающиеся в `DECIMAL(max_digits, decimal_places)`, — по умолчанию через `CAST(... AS NUMERIC)`; база, хранящая decimal текстом, переопределяет его. |
| `identifier_quote_char`, `alias_quote_char`, `quote_identifier(name)` | Кавычки для имён таблиц, колонок, индексов и ограничений и для псевдонимов в `SELECT`. |
| `max_identifier_length` | Наибольшая длина имени в байтах, None — без предела. hare генерирует имена до 63 байт, укорачивая более длинное с хэшем; диалект с меньшим пределом не регистрируется. |
| `supports_schemas` | Без этого таблица модели со схемой используется без имени схемы. |
| `get_string_literal_sql(text)`, `get_literal_sql(value)`, `get_bytes_literal_sql()`, `get_array_literal_sql()`, `get_bindable_number()` | Литералы, записываемые в текст SQL, — строка, умолчание столбца, байты, массив, — и как передаётся число из JSON. Имена и литералы экранируются только здесь: редактор схемы, отрисовка SQL и интроспектор спрашивают диалект. |
| `get_unbounded_limit_sql()` | `LIMIT`, который нужен `OFFSET` без ограничения (`LIMIT -1` в SQLite). |
| `get_explain_sql(sql, output_format, options)` | Команда `EXPLAIN`, которую используют `QuerySet.sql(explain=True)` и `explain()`. |
| `sql_context` | Контекст, в котором выводится каждая команда; класс запроса задаёт `SQL_CONTEXT = dialect.sql_context`. |

## 4. Типы колонок и значения — `build_types()` {: #types }

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

Признаки диалекта описывают поведение значений: `enforces_numeric_ranges` (без него hare сам проверяет
диапазоны целых и десятичных), `supports_virtual_generated_columns`. Там, где значение хранится или
вычисляется иначе, SQL даёт сам диалект: `get_decimal_compared_term()`, `get_decimal_value_term()` и
`get_decimal_dividend()` — для десятичных, хранящихся текстом, `get_json_path_comparand()` — для JSON,
хранящегося текстом, `get_integer_aggregate_as_float()` — для среднего целых, которое база считает
десятичным, `get_datetime_part_comparand()` — для даты или времени суток, сравниваемых с моментом
времени. Функция, которая есть в базе только для части типов аргументов (`ROUND(numeric, int)` в
PostgreSQL), — забота рендерера диалекта.

Пакет, добавляющий поле, может зарегистрировать его хранение на любом диалекте:

```python
DialectRegistry.get_dialect("clickhouse").types.register(MoneyField, TypeMapping(column_type="Decimal(18, 2)"))
```

## 5. Функции и операторы фильтров — `build_renderers()`, `build_filter_operators()` {: #functions-and-lookups }

По умолчанию каждое выражение выводится стандартным SQL. `build_renderers()` возвращает `TermRenderers`
— замены для того, что база записывает иначе; они находятся по иерархии классов выражения:

```python
def build_renderers(self):
    renderers = TermRenderers()
    renderers.register_function("LENGTH", self.render_length)         # (function, ctx) -> sql
    renderers.register_name(functions.Coalesce, lambda term, ctx: "ifNull")
    return renderers

@staticmethod
def render_length(length, ctx):
    return f"lengthUTF8({length.get_arg_sql(length.args[0], ctx)})"
```

`build_filter_operators()` возвращает `FilterOperators` — соответствие между функцией сравнения,
которую задаёт оператор фильтра, и её заменой в диалекте (`FilterOperators({Lookups.is_in: clickhouse_is_in, ...})`, `{}` —
без замен). Оператор, который реализуют только диалекты (`DialectImplementedOperators`), должен быть
заменён, иначе оператор фильтра не поддерживается: `Dialect.supports_lookup()` — а с ним и
`Model._meta.get_lookups(path, dialect)` — его не включает, а фильтр с ним даёт ошибку до построения
SQL.

Слишком длинный список значений, чтобы привязывать по параметру на значение, сокращает диалект:
создайте подкласс `LargeInList` (`hare.dialects.base.large_in_list`) и в `build_filter_operators()`
сопоставьте `Lookups.is_in`/`not_in`/`row_is_in`/`row_not_in` его одноимённым методам. Алгоритм
написан один раз в базовом классе — когда применяется короткая форма, как отдельно сравниваются
`NULL` из списка, как отрицается `not_in`, форма «строка значений» для составного ключа; диалект
даёт только контейнер, в который значения привязываются **одним** параметром:
`get_membership_criterion(field, values, non_null_values, element_type)` (`= ANY($1::type[])` в
PostgreSQL, `IN (SELECT value FROM json_each(?))` в SQLite) и
`get_row_container(value_rows, element_types)` для строк значений. Возврат `None` оставляет обычный
список `IN (...)`.

`is_distinct_from_operator` — неравенство, правильное при `NULL` (`IS NOT` в SQLite),
`get_composite_distinct_key()` — то, по чему `COUNT(DISTINCT ...)` считает составной ключ.

## 6. Устройство команд — класс запроса {: #query-class }

Класс запроса выводит команды через `QueryBuilder`, части которого — методы (`_limit_sql`,
`_offset_sql`, `_for_update_sql`, `_returning_sql`, `_on_conflict_sql`, `_update_sql`, `_delete_sql`, ...).
Диалект наследует оба класса и переопределяет части, которые его база записывает иначе:

```python
class ClickHouseQuery(Query):
    SQL_CONTEXT = CLICKHOUSE_DIALECT.sql_context

    @classmethod
    def _builder(cls, **kwargs):
        return ClickHouseQueryBuilder(**kwargs)


class ClickHouseQueryBuilder(QueryBuilder):
    QUERY_CLS = ClickHouseQuery

    def get_sql(self, ctx=None):
        ...   # ALTER TABLE ... UPDATE / DELETE для обновлений и удалений
```

Остальное задают признаки диалекта: `supports_distinct_on`, `sorts_nulls_first`,
`matches_ordering_to_grouping_by_sql`, `supports_conflict_constraint_names`, `supports_conflict_where`,
`guarantees_returning_order`, `supports_copy` вместе с `supports_copy_column_type()` и
`get_default_rows_source_sql()` для вставки строк из значений по умолчанию.

### Методы `QuerySet` от диалекта {: #queryset-methods }

Собственные модификаторы запросов базы становятся методами `QuerySet`, и hare о них знать не нужно.
Зарегистрируйте их в `install()` функцией, которая получает построитель запроса и аргументы вызова и
возвращает построитель, — например, `sample()` колоночного тестового диалекта:

```python
def install(self):
    super().install()
    QuerySetExtensions.register("sample", self.name, self.apply_sample)

def apply_sample(self, builder, percent):
    return builder.where(LiteralValue(f"abs(random()) % 100 < {int(percent)}"))
```

`Event.objects.filter(...).sample(10)` запоминает вызов — в том числе до `Hare.init()`, — а запрос применяет
его, когда строится для своего подключения; на подключении другого диалекта это даёт
`UnSupportedError`. Модификатор, который база записывает в части `FROM` (`FINAL`, `SAMPLE 0.1`), функция
сохраняет в построителе диалекта, а записывает его `_from_sql()`. Пакет может зарегистрировать метод и
для диалекта, который определил не он.

Диалект (или пакет для него) может добавить свои методы `QuerySet` — например, `.final()`,
`.sample()`, `.prewhere()` колоночной базы — через
`QuerySetExtensions.register(name, dialect_name, apply)` из `hare.query.queryset.extensions`, обычно в
своём `Dialect.install()`. `apply(builder, *args, **kwargs)` возвращает построитель запроса с
применённым вызовом. Вызов запоминается в запросе (в том числе до `Hare.init()`) и переносится в
`values()`, `count()` и другие запросы, построенные из него; когда запрос строится для своего
подключения, вызов применяет реализация диалекта этого подключения. На подключении, диалект которого
такой метод не зарегистрировал, запрос даёт `UnSupportedError`, а не молча пропускает вызов. Имя,
которое у `QuerySet` уже есть, или имя, начинающееся с `_`, отклоняется с `ConfigurationError`.

## 7. Транзакции и одновременная работа {: #transactions }

`Features.supports_transactions` решает, есть ли транзакции вообще (см. выше).

Сама транзакция — один автомат состояний, написанный один раз в `TransactionClient`
(`hare.dialects.base.client`): `begin()`, `commit()`, `rollback()`, `savepoint()`,
`release_savepoint()` и `savepoint_rollback()`, вложенность, колбэки `on_commit()`/`on_rollback()`,
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

Если транзакции есть, диалект
перечисляет `isolation_levels` от самого слабого: транзакция, которая просит уровень, выполняется на
самом слабом из перечисленных, который не слабее запрошенного (`get_isolation_level()`), а
`get_isolation_level_sql()` его задаёт. `get_lock_table_sql()` блокирует таблицу на время транзакции,
`get_migration_lock_sql()` выстраивает одновременные запуски `migrate` по очереди, а
`build_two_phase_commit()` даёт `Transactions.distributed()` его команды.

## 8. Команды схемы — редактор схемы {: #schema-editor }

`build_schema_editor_class()` возвращает подкласс `BaseSchemaEditor`. Базовый класс выводит стандартные
команды по шаблонам класса (`TABLE_CREATE_TEMPLATE`, `FIELD_TEMPLATE`, `INDEX_CREATE_TEMPLATE`,
`FK_TEMPLATE`, `ADD_FIELD_TEMPLATE`, `ALTER_FIELD_TYPE_TEMPLATE`, `RENAME_INDEX_TEMPLATE`, ...); диалект
переопределяет те, которые его база записывает иначе, и задаёт шаблону `None`, если такой команды нет,
— тогда изменение пересоздаёт таблицу (`_remake_table()`: новая таблица по модели, копирование строк,
замена старой). `_get_table_comment_sql()` и `_get_column_comment_sql()` записывают комментарии.

Признаки диалекта решают, какие команды вообще записываются:

| Признак | Без него |
|---|---|
| `supports_foreign_keys` | Нет ограничений `FOREIGN KEY`; hare сам выполняет каждое действие `on_delete` и проверку `PROTECT`, как для связи с `db_constraint=False`. |
| `supports_unique_constraints` | Нет ограничений уникальности; уникальный `Index` становится обычным индексом; вставка-или-обновление ищет конфликт только по первичному ключу. |
| `supports_adding_constraints` | Ограничения CHECK записываются в `CREATE TABLE`, ограничения уникальности становятся уникальными индексами, а последующее изменение пересоздаёт таблицу. |
| `supports_partial_indexes`, `supports_index_nulls_order`, `supports_concurrent_indexes` | Эта часть индекса не записывается или отклоняется. |
| `supports_exclusion_constraints`, `supports_deferrable_constraints`, `supports_not_valid_constraints` | Такой вид ограничения отклоняется. |
| `supports_statement_triggers`, `supports_extensions`, `supports_collations` | Триггеры `FOR EACH STATEMENT`, `CreateExtension`, `CreateCollation` отклоняются или пропускаются. |
| `truncates_values_on_type_change` | Задайте его, если сужающая смена типа молча обрезает данные, — тогда hare сначала проверяет данные. |

Флаг только сообщает, что у базы есть возможность; базовый редактор её синтаксис не пишет. SQL
пишет сам диалект, который выставил флаг:

| Возможность | Что реализует диалект |
|---|---|
| Неключевые колонки индекса (`include=`) | `Dialect.get_index_include_sql(quoted_columns)` — фраза после ключей индекса; по умолчанию `""`, и колонки в индекс не попадают. |
| `UniqueConstraint(nulls_distinct=...)` | `Dialect.get_nulls_distinct_sql(nulls_distinct)` — фраза; ещё выставьте `Features.supports_nulls_distinct`. |
| `supports_concurrent_indexes` | `add_index(model, index, concurrently)` и `remove_index(...)` редактора; базовый редактор создаёт и удаляет индекс обычным способом. `_get_index_create_sql()` и `_get_index_drop_sql()` дают обычные команды, от которых можно оттолкнуться. |
| `supports_not_valid_constraints` | `add_check_constraint_not_valid()` и `validate_constraint()` редактора; базовый редактор добавляет ограничение как обычно и ничего не проверяет отдельно. |
| `supports_exclusion_constraints` | `_exclusion_constraint_sql(model, constraint)` редактора — определение ограничения. |
| `supports_extensions` | `create_extension()`, `drop_extension()` и `_get_extension_create_sql()` редактора. |
| `supports_collations` | `create_collation()` и `drop_collation()` редактора. |
| `supports_schemas` | `move_table_to_schema()` редактора — для модели, у которой меняется `Meta.schema`. |

То же относится к операторам фильтра, которые есть только у одной базы: диалект регистрирует их в
своём `install()` через [`Field.register_lookup()`](custom-lookups.ru.md),
называя себя в `dialects=`, а нужное расширение — в `required_extension=`; так PostgreSQL
регистрирует свои триграммные операторы.

### Параметры хранения таблицы {: #table-options }

База, у которой `CREATE TABLE` принимает больше, чем колонки, объявляет подкласс `TableOptions`; модели
перечисляют его в `Meta.table_options`, по одной записи на диалект, а подключение использует запись
своего диалекта:

```python
@dataclasses.dataclass(frozen=True)
class ClickHouseTableOptions(TableOptions):
    dialect_name: ClassVar[str] = "clickhouse"

    engine: str = "MergeTree()"
    order_by: tuple[str, ...] = ()
    partition_by: str | None = None

    def raise_if_unsupported(self, model):
        if not self.order_by:
            raise ConfigurationError(f"{model.__name__}: ClickHouseTableOptions needs order_by")

    def get_create_suffix_sql(self, model, quote):
        sql = f" ENGINE = {self.engine} ORDER BY ({', '.join(quote(column) for column in self.order_by)})"
        return sql + (f" PARTITION BY {self.partition_by}" if self.partition_by else "")


class Event(Model):
    class Meta:
        primary_key = None
        table_options = [ClickHouseTableOptions(order_by=("created_at",))]
```

Диалект называет класс в `build_table_options_class()`. Миграции хранят параметры в состоянии модели и
записывают их в файлы миграций через `deconstruct()`; их изменение применяет `alter_table_options()`
редактора схемы — по умолчанию таблица пересоздаётся с новыми параметрами. `hare drift` сравнивает их с
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

Тогда `makemigrations` пишет `AddPartition`/`RemovePartition` на каждую добавленную или убранную
партицию, а они вызывают `add_partition(model, partition)`/`remove_partition(model, partition)`
редактора схемы (по умолчанию — `UnSupportedError`); `_get_partition_create_sqls(model, safe)`
возвращает команды создания партиций сразу после `CREATE TABLE` таблицы. На этих хуках построены
[партиционированные таблицы](../models/meta-options.ru.md#partitioning) PostgreSQL.

### Модели без первичного ключа {: #models-without-a-primary-key }

У таблицы колоночной базы часто нет первичного ключа: `Meta.primary_key = None` (см.
[Опции Meta](../models/meta-options.ru.md#primary_key)). Чтение, фильтры, агрегаты,
`bulk_create()` и `QuerySet.update()`/`delete()` работают; то, чему нужен ключ, чтобы найти строку
(`save()` прочитанной строки, `instance.delete()`, связи с моделью и т. п.), даёт `ConfigurationError`.

## 9. Чтение существующей схемы {: #introspector }

`build_introspector_class()` возвращает подкласс `SchemaIntrospector`, реализующий
`fetch_default_schema()`, `fetch_table_names(connection, schema, include_partitions)` и
`fetch_tables(connection, tables, schema) -> list[TableInfo]`. Остальное — `inspectdb`, поиск
расхождений, обратное сопоставление типов и полей — работает с общими `TableInfo`/`ColumnInfo`/
`IndexInfo`/`ForeignKeyInfo`. Хранение таблицы попадает в `TableInfo.table_options` через
`table_options_class.from_observed({"engine": ..., "order_by": ...})`, который оставляет имена,
объявленные классом, и даёт `None`, если все параметры по умолчанию; `get_declared_table_options()`
позволяет поиску расхождений считать то же хранение, записанное базой иначе, совпадающим с объявленным.
Без чтения схемы (`None`, по умолчанию) `inspectdb` и `hare drift` дают `UnsupportedDialectError`.

## 10. Миграции {: #migrations }

Журнал миграций и каждая операция идут через редактор схемы, поэтому своего им ничего не нужно.
`Features.can_rollback_ddl` решает, выполняется ли миграция в транзакции, `get_migration_lock_sql()` —
ждут ли друг друга два запуска `migrate`, а `get_connection_only_function(sql)` называет функцию,
которую hare устанавливает только в своих соединениях и которую поэтому нельзя использовать в командах
схемы (функции SQLite, которые hare регистрирует в каждом соединении).

## 11. Что hare делает для базы без гарантий {: #without-guarantees }

| У базы нет | hare |
|---|---|
| Транзакций | Отклоняет `atomic()`; свои записи из нескольких команд выполняет по одной команде. |
| Внешних ключей | Выполняет `on_delete` и `PROTECT` в Python. |
| Ограничений уникальности | Не создаёт их; вставка-или-обновление ищет конфликт только по первичному ключу. |
| `RETURNING` | `UPDATE` не читает изменённые вычисляемые колонки; ключ, который генерирует база, в объект не читается; значения `db_default` читаются `SELECT` после `INSERT`. |
| Отложенных ограничений | `defer_cascade_foreign_keys()` даёт `UnSupportedError` — настоящее удаление через защиту `PROTECT` в том же каскаде нельзя отложить. |
| Первичного ключа | Поддерживает модели без ключа, как описано выше. |

`checks_foreign_keys_per_cascade_step` и `checks_restrict_at_statement_end` диалекта и
`cascade_depth_limit` в `Features` клиента описывают, как ведёт себя собственный каскад базы, чтобы hare
знал, когда откладывать его или выполнять самому. Клиент, у которого в возможностях задан
`cascade_depth_limit`, даёт `CascadeDepthLimitError`
из `hare.exceptions` — или свой подкласс, как `SqliteTriggerRecursionLimitError` у SQLite, — когда база
останавливает каскад на этой глубине; `delete()` перехватывает её, откатывает попытку и проходит каскад в
Python.

## 12. Тестирование {: #testing }

Собственный набор тестов hare работает на диалекте: установите пакет (его точка входа регистрирует
драйвер) и направьте на него `HARE_TEST_DB` (`clickhouse://.../test_{}`, место `{}` заполняется при
каждом запуске) — выполнится каждый тест, который допускают возможности диалекта. Тест того, чего у
базы может не быть, помечается нужными ему возможностями:

```python
from hare.contrib.test import requires_features

@requires_features(supports_transactions=True)
async def test_rollback(db): ...

@requires_features(supports_foreign_keys=True)      # признак Dialect тоже подходит
async def test_cascade_in_the_database(db): ...
```

`tests/test_dialect_contract.py` проверяет каждый зарегистрированный диалект и драйвер: имена, схемы,
возможности, реестры, редактор схемы, чтение схемы и то, что SQL строится одинаково при повторе. Между
тестами база очищается через `Dialect.clear_tables()` (по умолчанию — один скрипт с `DELETE FROM` для
каждой таблицы).

## 13. Наблюдение и скорость {: #observability }

`otel_system_name` — значение `db.system.name` в OpenTelemetry (`clickhouse` или `other_sql`). Чтение
строк на Rust зависит от `native_python_types` клиента, а не от имени диалекта. Каждая команда,
которую выполняет клиент, доходит до [`Observers`](../observability/observers.ru.md) —
`QueryExecuted`, обёртки запросов, журнал медленных запросов — через декоратор, описанный в разделе
[о клиенте](#client); клиент, который его пропускает, невидим для `capture_queries`, поиска N+1 и
OpenTelemetry.

## Проверочный список {: #checklist }

- [ ] Подкласс `Dialect`: имя, `otel_system_name`, признаки, `build_types()`,
      `build_filter_operators()`, `build_renderers()`, `get_explain_sql()`.
- [ ] Класс запроса и построитель с `SQL_CONTEXT = dialect.sql_context`.
- [ ] Клиент: команды, перевод ошибок, `features`; транзакции, если они есть у базы.
- [ ] Драйвер: имя, схемы, учётные данные, классы клиентов; регистрация при импорте.
- [ ] Точка входа `hare.dialects`.
- [ ] Шаблоны редактора схемы, параметры хранения таблицы, чтение схемы — сколько нужно базе.
- [ ] Набор тестов hare проходит с `HARE_TEST_DB`, включая `tests/test_dialect_contract.py`.
