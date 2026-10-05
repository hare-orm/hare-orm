# Диалекты и их возможности

**Диалект** — это язык SQL, типы колонок, команды создания и изменения схемы и системный каталог
одного вида баз данных; **драйвер** — то, как hare к такой базе подключается. У одного диалекта может
быть несколько драйверов — у PostgreSQL их два. `engine` подключения (или схема в адресе базы — это
одно и то же слово) выбирает драйвер, а вместе с ним и диалект; см.
[Выбор драйвера PostgreSQL](../connections/connections.ru.md#choosing-a-postgres-driver).

| Диалект | `engine` / схема адреса | Драйвер | Самая старая версия сервера |
|---|---|---|---|
| `sqlite` | `sqlite+aiosqlite` | `aiosqlite` поверх стандартного модуля `sqlite3` | SQLite 3.35.5 |
| `postgresql` | `postgresql` | драйвер hare на Rust | PostgreSQL 14 |
| `postgresql` | `postgresql+asyncpg` | `asyncpg` (`hare-orm[asyncpg]`) | PostgreSQL 14 |
| `clickhouse` | `clickhouse+clickhouse-connect` | clickhouse-connect, по HTTP (`hare-orm[clickhouse]`) — [ClickHouse](clickhouse/connecting.ru.md) | ClickHouse 24.3 |
| `clickhouse` | `clickhouse+clickhouse-driver` | clickhouse-driver, по родному протоколу поверх TCP (`hare-orm[clickhouse-driver]`) — [ClickHouse](clickhouse/connecting.ru.md) | ClickHouse 24.3 |

`aiosqlite` выполняет каждое подключение SQLite в отдельном потоке, и этот поток не удерживает
интерпретатор: программа, которая завершается с незакрытым подключением (например, из-за
исключения), выходит, а не зависает, а журнал SQLite откатывает незавершённую транзакцию.

Всё, что hare делает по-разному для разных баз, — синтаксис SQL, типы колонок, как значение
записывается и читается, команды изменения схемы, чтение существующей схемы, — решают диалект
подключения и его `Features`, а не сравнение названий баз. Запрос выполняется на диалекте того
подключения, на котором он работает (`using()`, транзакция, маршрутизатор или подключение модели
по умолчанию), поэтому одну модель можно читать и из SQLite, и из PostgreSQL в одном процессе.

## <a id="the-server-version"></a>Версия сервера

Сразу после открытия соединения hare читает версию сервера — так, что этого не видят
наблюдатели (драйверы PostgreSQL читают `server_version_num`, SQLite сообщает версию своей библиотеки).
Сервер старше `minimum_server_version` своего диалекта отклоняется до выполнения чего-либо ещё, а
соединение закрывается:

```text
UnSupportedError: The postgresql server is version 13.9, older than 14, the oldest hare runs on
```

Возможность, которая появилась только в какой-то версии, определяется по собственной версии
сервера: у подключения к PostgreSQL 14 `features.supports_nulls_distinct` выключено, поэтому
`UniqueConstraint(..., nulls_distinct=False)` на нём даёт `UnSupportedError` до отправки команд
создания схемы. По той же причине `Hare.generate_schemas()` подключается к базе, прежде чем записывать
команды.

### <a id="sqlite-library-faults"></a>Ошибки библиотек SQLite

Две ошибки отдельных версий библиотеки SQLite hare обходит по её версии:

- SQLite с 3.38.0 по 3.41.0 строит автоматические индексы без учёта правила сортировки сравнения,
  которому они служат, поэтому сравнение десятичных значений или времени по присоединённой таблице —
  hare сравнивает их по своим правилам сортировки — не находит строк. Соединение на такой библиотеке
  работает с `PRAGMA automatic_index = OFF`, а `automatic_index=True` в его настройках даёт
  `ConfigurationError`. Исправлено в SQLite 3.41.1.
- SQLite с 3.38.0 по 3.38.5 сообщает о нарушении внешнего ключа командой с `RETURNING` как об
  обычной ошибке, а не о нарушении ограничения. hare выбрасывает его как `IntegrityError`, как и на
  остальных версиях. Исправлено в SQLite 3.39.0.

## <a id="connection-dialect-and-features"></a>`connection.dialect` и `connection.features`

```python
connection = Book.get_connection()
connection.dialect.name                        # "postgresql"
connection.dialect.features.supports_copy      # True
connection.features.supports_nulls_distinct    # True у PostgreSQL 15 и новее
```

`connection.dialect` — объект диалекта, один на диалект и общий для всех подключений, которые на нём
работают; `connection.dialect.features` — что поддерживает база, одинаково для всех подключений к ней.
`connection.features` — неизменяемый `Features` этого подключения: возможности диалекта вместе с тем,
что добавляет драйвер и меняет версия сервера; значения фиксируются после подключения. hare читает
`connection.features` везде, где у него есть подключение.

### <a id="features"></a>`Features`

| Поле | Что означает | `sqlite` | `postgresql` | `postgresql+asyncpg` | `clickhouse` |
|---|---|---|---|---|---|
| `supports_transactions` | Транзакции | да | да | да | с `transactions=true` — см. [Транзакции и блокировки](clickhouse/transactions-and-locks.ru.md) |
| `can_rollback_ddl` | Команды изменения схемы выполняются внутри транзакции и откатываются вместе с ней | да | да | да | нет |
| `supports_select_for_update` | `SELECT ... FOR UPDATE` | нет | да | да | с `transactions=true` и `keeper_hosts` |
| `locks_rows_by_key` | `select_for_update()` блокирует строки по их ключам вне SQL — сначала читаются и блокируются ключи, затем по ним читаются строки | нет | нет | нет | с `keeper_hosts`: в ClickHouse Keeper |
| `supports_select_for_no_key_update` | `SELECT ... FOR NO KEY UPDATE` | нет | да | да | нет |
| `supports_select_for_share` | `SELECT ... FOR SHARE` | нет | да | да | нет |
| `supports_select_for_key_share` | `SELECT ... FOR KEY SHARE` | нет | да | да | нет |
| `supports_update_limit_order_by` | `UPDATE`/`DELETE` с `ORDER BY` и `LIMIT` | нет | нет | нет | нет |
| `supports_posix_regex` | Операторы фильтра с регулярными выражениями POSIX | при `install_regexp_functions=True` | да | да | нет |
| `supports_returning` | `INSERT ... RETURNING` | да | да | да | нет |
| `returns_rows_by_reading` | Строки, которые запись возвращает без `RETURNING`, читаются по ключам — до удаления, после изменения или вставки | нет | нет | нет | да |
| `supports_two_phase_commit` | `PREPARE TRANSACTION` / `COMMIT PREPARED` | нет | да | да | нет |
| `supports_listen_notify` | `LISTEN` / `NOTIFY` | нет | да | да | нет |
| `inline_comments` | Комментарии таблиц и колонок записываются внутри `CREATE TABLE` | да | нет | нет | да |
| `supports_positional_rows` | Строки `execute(rows_by_position=True)` читаются по номеру колонки | да | да | да | да |
| `supports_streaming` | `QuerySet.stream()` читает строки через курсор внутри транзакции | да | да | да | да |
| `streams_without_transaction` | `stream()` читает строки и вне транзакции — сервер отдаёт строки по мере вычисления | нет | нет | нет | да |
| `execute_many_scales_poorly` | `executemany()` медленнее одной команды на много строк | нет | да | нет | да |
| `binds_written_parameters` | Драйвер передаёт строки, собранные встроенным записывающим кодом hare, как есть | нет | да | нет | нет |
| `max_bind_parameters` | Наибольшее число параметров в одной команде | 32766 | 32767 | 32767 | 1000000 |
| `cascade_depth_limit` | Глубина рекурсии, на которой останавливается собственный `ON DELETE CASCADE` базы; тогда клиент даёт `CascadeDepthLimitError`, а глубокий каскад доводит до конца hare — меньшим числом уровней за раз | `SQLITE_LIMIT_TRIGGER_DEPTH` сборки (1000; 100 у официальной библиотеки 3.37.2) | нет | нет | нет |
| `supports_nulls_distinct` | Ограничение уникальности принимает `NULLS [NOT] DISTINCT` | нет | с 15 | с 15 | нет |
| `supports_partitioned_exclusion_constraints` | Партиционированная таблица принимает ограничение-исключение | нет | с 17 | с 17 | нет |
| `supports_unhex` | Функция SQL `unhex()` — длинный `__in` по байтам передаётся одним массивом JSON | с 3.41.0 | нет | нет | нет |
| `supports_drop_column` | `ALTER TABLE ... DROP COLUMN` — `RemoveField` простой колонки удаляет её на месте, без пересборки таблицы | с 3.35.5 | да | да | да |
| `explain_options` | Параметры, которые принимает `explain(**options)` | нет | параметры версии сервера (`GENERIC_PLAN` с 16, `MEMORY` и `SERIALIZE` с 17) | так же | нет |
| `supports_pool_status` | Клиент сообщает состояние и метрики своего пула — `get_pool_status()`, [метрики пула](../observability/pool-health.ru.md) | да | да | да | нет |

Если возможности нет, операция, которой она нужна, даёт `UnSupportedError` с её названием до отправки
чего-либо: `select_for_update()` в SQLite, `Transactions.distributed()` без двухфазной фиксации,
`stream()` без потоковой передачи.

### <a id="database-features"></a>Возможности базы

Остальные поля `Features` принадлежат самой базе: их объявляет её диалект, и у каждого её драйвера
значения одинаковы.

| Поле | Что означает | `sqlite` | `postgresql` | `clickhouse` |
|---|---|---|---|---|
| `supports_schemas` | Таблицу можно указать вместе со схемой (`Meta.schema`); без этого схема не учитывается | нет | да | нет |
| `supports_distinct_on` | `SELECT DISTINCT ON (...)` — `distinct("field")` выполняет его; без него те же строки выбираются по номеру строки | нет | да | нет |
| `supports_grouping_sets` | `GROUP BY ROLLUP/CUBE/GROUPING SETS` и `GROUPING()` — `group_by(Rollup(...))`, `Grouping()` | нет | да | нет |
| `supports_lateral` | Подзапрос `LATERAL` во `FROM` — `Lateral(queryset)` | нет | да | нет |
| `supports_table_sample` | Выборка таблицы во `FROM` — `sample()`: `TABLESAMPLE`, `SAMPLE` ClickHouse | нет | да | да |
| `supports_asof_join` | `ASOF LEFT JOIN` — `AsofJoin(...)` | нет | нет | да |
| `supports_array_join` | Каждая строка повторяется с каждым элементом своего массива — `ArrayJoin(...)` | нет | нет | да |
| `supports_merge` | `MERGE` — `merge()` | нет | да (15+) | нет |
| `supports_merge_returning` | `MERGE ... RETURNING` — `merge().returning()` | нет | да (17+) | нет |
| `supports_merge_not_matched_by_source` | `WHEN NOT MATCHED BY SOURCE` — `when_not_matched_by_source()` | нет | да (17+) | нет |
| `supports_enum_types` | `CREATE TYPE ... AS ENUM` — `NativeEnumField` | нет | да | нет |
| `supports_views` | `Meta.views` и операции представлений — см. [Представления, функции, последовательности и доступ](../models/schema-objects.ru.md) | нет | да | да |
| `supports_materialized_views` | `Meta.materialized_views`, `RefreshMaterializedView`, `refresh_materialized_view()` | нет | да | да — см. [Объекты схемы](clickhouse/schema-objects.ru.md) |
| `supports_refreshable_materialized_views` | Материализованное представление, которое сервер обновляет по расписанию, — `ClickhouseMaterializedView(refresh=...)` | нет | нет | с 24.10 |
| `supports_dictionaries` | `Meta.dictionaries` — строки таблицы, загруженные для поиска по ключу, `DictGet(...)` | нет | нет | да |
| `supports_database_functions` | `Meta.functions` и операции функций | нет | да | нет |
| `supports_sequences` | `Meta.sequences`, операции последовательностей, `get_next_sequence_value()` | нет | да | нет |
| `supports_row_level_security` | `Meta.row_level_security`, `Meta.policies` и их операции | нет | да | нет |
| `supports_grants` | `Meta.grants`, `AddGrant`/`RemoveGrant` | нет | да | нет |
| `sorts_nulls_first` | При сортировке по возрастанию `NULL` по умолчанию идёт раньше всех значений | да | нет | нет |
| `enforces_numeric_ranges` | Целые и десятичные колонки сами отклоняют значения вне диапазона; без этого их проверяет hare | нет | да | нет |
| `guarantees_returning_order` | `INSERT ... RETURNING` на много строк возвращает строки в порядке вставки — `bulk_create(returning=True)` | нет | да | да (строки дочитываются по ключам) |
| `supports_conflict_constraint_names` | `bulk_create(on_conflict_constraint=...)` | нет | да | нет |
| `supports_conflict_where` | `bulk_create(conflict_where=...)` | нет | да | нет |
| `supports_copy` | Массовая загрузка строк не текстом SQL — `bulk_create(use_copy=True)`: протокол `COPY` на PostgreSQL, двоичная вставка на ClickHouse | нет | да | да |
| `copies_bulk_inserts` | `bulk_create()` загружает строки так и без `use_copy=True` — когда не обрабатывает конфликты и не читает строки обратно | нет | нет | да |
| `supports_virtual_generated_columns` | Вычисляемая колонка может вычисляться при чтении (`stored=False`); без него колонка `VIRTUAL` даёт `UnSupportedError` до DDL | да | да (18+) | да (`ALIAS`) |
| `supports_uuid_v7` | `db_default=UuidV7()` (`uuidv7()`) | нет | да (18+) | нет |
| `supports_without_overlaps` | `UniqueConstraint(without_overlaps=True)` и `CompositePrimaryKey(without_overlaps=True)` — `WITHOUT OVERLAPS` у последнего поля, диапазона | нет | да (18+) | нет |
| `supports_returning_old_new` | `returning(old=...)` у `update()` и `merge()` — `RETURNING old.column` | нет | да (18+) | нет |
| `supports_json_table` | `JSON_TABLE` — `JsonTable(...)` | нет | да (17+) | нет |
| `supports_strict_tables` | `SqliteTableOptions(strict=True)` — таблицы `STRICT` | да (3.37+) | нет | нет |
| `supports_text_search_configurations` | [Полнотекстовый поиск](search-and-geodata/full-text-search.ru.md) с конфигурациями, `SearchVector` как значением, лексемами, весами меток, нормализацией ранга и параметрами фрагментов разметки | нет | да | нет |
| `supports_full_text_index` | `FullTextIndex` — таблица FTS5, которую держат в согласии триггеры и из которой читает [полнотекстовый поиск](search-and-geodata/full-text-search.ru.md), и веса полей у `SearchRank` | да (SQLite, собранная с FTS5) | нет (поиск по tsvector) | нет |
| `supports_vector_search` | Расстояния и `__nearby` из [`hare.vectors`](search-and-geodata/vector-search.ru.md) — pgvector в PostgreSQL, sqlite-vec в SQLite | с `load_sqlite_vec=true` | да (pgvector) | нет |
| `supports_tenant_schemas` | [Схема на арендатора](../soft-delete-versions-tenants/schema-per-tenant.ru.md) — `tenant_schema_template`, `Meta.tenant_schema`, `TenantSchemas` | нет | да | нет |
| `supports_spatial` | Пространственные операторы, пути, функции и агрегаты [`hare.gis`](search-and-geodata/gis.ru.md) | с `load_spatialite=true` | да (PostGIS) | да (гео-типы — см. [Типы](clickhouse/types.ru.md#geo)) |
| `supports_geography` | `GeometryField(geography=True)`, измеряемое на эллипсоиде | с пространственными метаданными SpatiaLite (`spatialite_metadata` не `none`) | да | да (измеряется на сфере) |
| `supports_spatial_index` | [`SpatialiteIndex`](search-and-geodata/gis.ru.md#spatial-index) — R*Tree SpatiaLite и пространственные операторы, сужаемые через него | с пространственными метаданными SpatiaLite | нет (`GistIndex`) | нет |
| `spatial_reference_ids` | SRID пространственных метаданных, которые принимают geography и пространственный индекс, — None для любых | из метаданных, читаются при открытии соединения | None | нет |
| `supports_ordered_aggregates` | `ORDER BY` внутри агрегата — `MakeLine(order_by=...)` | да (3.44+) | да | нет |
| `isolation_levels` | Уровни изоляции, на которых выполняется транзакция, — см. [Уровень изоляции](../connections/transactions.ru.md#isolation-level) | `SERIALIZABLE` | все четыре | `REPEATABLE READ` — снимок на начало транзакции |
| `max_identifier_length` | Наибольшая длина имени в байтах. hare держит каждое сгенерированное имя в пределах 63 байт, укорачивая более длинное с хэшем, и не регистрирует диалект с меньшим пределом | нет | 63 | нет |
| `supports_adding_constraints` | `ALTER TABLE ... ADD CONSTRAINT`; без этого изменение ограничения позже пересоздаёт таблицу | нет | да | да |
| `supports_partial_indexes` | Индекс принимает условие `WHERE` | да | да | нет |
| `supports_exclusion_constraints` | `ExclusionConstraint` | нет | да | нет |
| `supports_deferrable_constraints` | `deferrable=True` у ограничения или триггера | нет | да | нет |
| `supports_index_nulls_order` | Ключ индекса задаёт место `NULL` | нет | да | нет |
| `supports_concurrent_indexes` | Операции с индексами `CONCURRENTLY` | нет | да | нет |
| `supports_not_valid_constraints` | Ограничение добавляется как `NOT VALID` и проверяется позже | нет | да | нет |
| `supports_statement_triggers` | `TriggerForEach.STATEMENT` | нет | да | нет |
| `supports_extensions` | `CREATE EXTENSION` | нет | да | нет |
| `supports_collations` | `CREATE COLLATION` | нет | да | нет |
| `supports_foreign_keys` | Внешние ключи проверяются базой; без этого каждое действие `on_delete` выполняет hare | да | да | нет |
| `supports_unique_constraints` | Уникальность проверяется базой | да | да | нет |
| `checks_constraints_before_write` | hare сам проверяет объявленные в модели уникальность и связи до записи строк — см. [Модели](clickhouse/models.ru.md#uniqueness-and-relations) | нет | нет | да |
| `truncates_values_on_type_change` | Сужающая смена типа молча обрезает значения; hare сначала проверяет данные | нет | да | да |
| `alters_indexed_columns` | Столбец под индексом меняет тип или допустимость NULL; без неё hare удаляет покрывающие индексы на время изменения и создаёт их заново | да | да | нет |
| `binds_array_parameters` | Список передаётся одним параметром-массивом (`RawSQL("... = ANY(%s)", [ids])`) | нет | да | нет |
| `matches_ordering_to_grouping_by_sql` | Выражение сортировки, по которому идёт и группировка, записывается точно как в `GROUP BY` | нет | да | нет |
| `checks_foreign_keys_per_cascade_step` | Внешний ключ `NO ACTION` проверяется после каждого вложенного шага `ON DELETE CASCADE`, а не один раз в конце команды — `DELETE` тогда падает на строке, которую охраняет связь `on_delete=PROTECT`, даже если тот же каскад позже удаляет охраняющую строку, пока ограничения PROTECT не отложены | нет | да | нет |
| `checks_restrict_at_statement_end` | Внешний ключ `RESTRICT` проверяется в конце команды, как `NO ACTION`, а не сразу, до продолжения собственного каскада команды | нет | да | нет |
| `supports_savepoints` | Точки сохранения — вложенный `atomic()` | да | да | нет |
| `supports_generated_keys` | База генерирует первичный ключ (`IntField(primary_key=True)`); без этого такая модель отклоняется при привязке | да | да | с 25.1, с Keeper |
| `takes_keys_before_insert` | Ключи берутся из серии до записи строк — `generateSerialID` | нет | нет | да |
| `supports_row_updates` | `UPDATE` хранимых строк | да | да | да |
| `supports_row_deletes` | `DELETE` хранимых строк | да | да | да |
| `supports_lightweight_update` | `UPDATE`, который пишет новые значения строки рядом с ней, а не мутация, переписывающая её часть, — `ClickhouseTableOptions(lightweight_updates=True)` | нет | нет | с 25.7 |
| `rebuilds_projections` | Таблица с проекциями принимает лёгкий `DELETE`; без этого её строки удаляются мутацией | нет | нет | с 24.8 |
| `supports_json_type` | Значение JSON хранится собственным типом с типизированными путями — `JSONField` становится колонкой `JSON` | нет | нет | с 25.3 |
| `supports_variant_types` | Колонка хранит значения нескольких типов — `VariantField`, `DynamicField` | нет | нет | с 25.3 |
| `supports_correlated_subqueries` | Связанный подзапрос; без этого `EXISTS`, связанный равенством колонок, пишется как проверка `IN`, а любой другой отклоняется | да | да | с 25.4 |
| `rewrites_correlated_exists` | `EXISTS`, связанный равенством колонок, пишется как проверка `IN` и там, где связанные подзапросы выполняются, — собственный `EXISTS` сервера теряет строки | нет | нет | да |
| `supports_ordered_correlated_subqueries` | Связанный подзапрос сам сортирует и срезает свои строки (`Subquery(... .order_by(...)[:1])`) | да | да | нет |
| `orders_by_correlated_subqueries` | Связанный подзапрос выполняется в `ORDER BY` и рядом с `WHERE`; без этого строки выбираются в производной таблице, а фильтруются, сортируются и срезаются снаружи | да | да | нет |

Каждое поле описано в `hare.dialects.base.features.Features`.
`connection.dialect.minimum_server_version` — самая старая версия сервера, с которой работает hare:
SQLite 3.35.5, PostgreSQL 14, ClickHouse 24.3. Как диалект хранит таблицу — `WITHOUT
ROWID` в SQLite, табличное пространство, `UNLOGGED`, параметры хранения и
[партиционирование](../models/meta-options.ru.md#partitioning) (хеш-, списочные и диапазонные партиции,
которые добавляют и удаляют миграции) в PostgreSQL, — задаётся для каждой модели через
[`Meta.table_options`](../models/meta-options.ru.md#table_options).

## <a id="dialectregistry"></a>`DialectRegistry`

```python
from hare.dialects.dialect_registry import DialectRegistry

DialectRegistry.get_dialect("postgresql")                  # объект диалекта
DialectRegistry.get_driver("postgresql+asyncpg")           # драйвер, который называет engine
DialectRegistry.get_driver_for_url_scheme("sqlite")        # драйвер, который выбирает схема адреса
DialectRegistry.get_dialects(), DialectRegistry.get_drivers()
```

Неизвестное имя или схема дают `ConfigurationError` со списком зарегистрированных. Встроенный
диалект или драйвер импортируется и регистрируется, только когда его называет подключение или поиск,
поэтому `Hare.init()` на `sqlite+aiosqlite://` не импортирует ничего от PostgreSQL, а `Hare.init()` на
`postgresql://` — ничего от SQLite; `get_dialects()` перечисляет встроенные диалекты первыми, всегда
в одном порядке. Имя
или схема, которых нет у встроенных драйверов, а также `get_dialects()`/`get_drivers()` загружают
каждый модуль, названный в группе точек входа `hare.dialects` установленного пакета, — сторонний
диалект регистрирует свои драйверы при импорте:

```toml
[project.entry-points."hare.dialects"]
colstore = "hare_colstore.driver"
```

После этого его `engine` и схема адреса работают так же, как встроенные. Как написать свой диалект —
в руководстве [Как написать диалект](../extending/writing-a-dialect.ru.md).

### <a id="registering-after-init"></a>Регистрация после `Hare.init()`

Диалект, драйвер, способ вывода выражения или запись типа диалекта, оператор фильтра из
`register_lookup()`, часть пути внутри значения и метод `QuerySet` от диалекта можно регистрировать в
любой момент. Каждая регистрация сбрасывает всё, что hare сохранил из реестров, — SQL запросов, планы
чтения строк, описания фильтров и сортировок — и заново собирает фильтры каждой привязанной модели,
поэтому следующий запрос уже использует новое; запрос, построенный раньше, выполняется со своим SQL.
Обычно регистрация происходит при импорте, до `Hare.init()`, когда сохранённого ещё ничего нет и
сброс ничего не стоит.

## <a id="tests-by-capability"></a>Тесты по возможностям

[`requires_features()`](../testing/setup.ru.md#requires-features) пропускает тест, если у подключения нет
нужного ему — поля `Features`, атрибута диалекта или имени диалекта:

```python
@requires_features(supports_distinct_on=True)
async def test_latest_per_author(db): ...
```
