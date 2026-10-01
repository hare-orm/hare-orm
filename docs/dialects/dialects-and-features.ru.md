# Диалекты и их возможности

**Диалект** — это язык SQL, типы колонок, команды создания и изменения схемы и системный каталог
одного вида баз данных; **драйвер** — то, как hare к такой базе подключается. У одного диалекта может
быть несколько драйверов — у PostgreSQL их два. `engine` подключения (или схема в адресе базы — это
одно и то же слово) выбирает драйвер, а вместе с ним и диалект; см.
[Выбор драйвера PostgreSQL](../connections/connections.ru.md#choosing-a-postgres-driver).

| Диалект | `engine` / схема адреса | Драйвер | Самая старая версия сервера |
|---|---|---|---|
| `sqlite` | `sqlite` | `aiosqlite` поверх стандартного модуля `sqlite3` | SQLite 3.35.0 |
| `postgresql` | `postgresql` | драйвер hare на Rust | PostgreSQL 14 |
| `postgresql` | `postgresql+asyncpg` | `asyncpg` (`hare-orm[asyncpg]`) | PostgreSQL 14 |

`aiosqlite` выполняет каждое подключение SQLite в отдельном потоке, и этот поток не удерживает
интерпретатор: программа, которая завершается с незакрытым подключением (например, из-за
исключения), выходит, а не зависает, а журнал SQLite откатывает незавершённую транзакцию.

Всё, что hare делает по-разному для разных баз, — синтаксис SQL, типы колонок, как значение
записывается и читается, команды изменения схемы, чтение существующей схемы, — решают диалект
подключения и его `Features`, а не сравнение названий баз. Запрос выполняется на диалекте того
подключения, на котором он работает (`using()`, транзакция, маршрутизатор или подключение модели
по умолчанию), поэтому одну модель можно читать и из SQLite, и из PostgreSQL в одном процессе.

## Версия сервера {: #the-server-version }

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

## `connection.dialect` и `connection.features` {: #connection-dialect-and-features }

```python
connection = Book.get_connection()
connection.dialect.name                 # "postgresql"
connection.dialect.supports_distinct_on # True
connection.features.supports_returning  # True
```

`connection.dialect` — объект диалекта, один на диалект и общий для всех подключений, которые на нём
работают: что может выразить SQL этой базы. `connection.features` — неизменяемый `Features` этого
подключения: что поддерживают версия его сервера и драйвер; значения фиксируются после подключения.

### `Features` {: #features }

| Поле | Что означает | `sqlite` | `postgresql` | `postgresql+asyncpg` |
|---|---|---|---|---|
| `supports_transactions` | Транзакции и точки сохранения | да | да | да |
| `can_rollback_ddl` | Команды изменения схемы выполняются внутри транзакции и откатываются вместе с ней | да | да | да |
| `supports_select_for_update` | `SELECT ... FOR UPDATE` | нет | да | да |
| `supports_select_for_no_key_update` | `SELECT ... FOR NO KEY UPDATE` | нет | да | да |
| `supports_update_limit_order_by` | `UPDATE`/`DELETE` с `ORDER BY` и `LIMIT` | нет | нет | нет |
| `supports_posix_regex` | Операторы фильтра с регулярными выражениями POSIX | при `install_regexp_functions=True` | да | да |
| `supports_returning` | `INSERT ... RETURNING` | да | да | да |
| `supports_two_phase_commit` | `PREPARE TRANSACTION` / `COMMIT PREPARED` | нет | да | да |
| `supports_listen_notify` | `LISTEN` / `NOTIFY` | нет | да | да |
| `inline_comments` | Комментарии таблиц и колонок записываются внутри `CREATE TABLE` | да | нет | нет |
| `supports_positional_rows` | Строки результата читаются и по номеру колонки, и по её имени | да | да | да |
| `supports_streaming` | `QuerySet.stream()` читает строки через курсор на стороне сервера | нет | да | да |
| `execute_many_scales_poorly` | `executemany()` медленнее одной команды на много строк | нет | да | нет |
| `max_bind_parameters` | Наибольшее число параметров в одной команде | 32766 | 32767 | 32767 |
| `cascade_depth_limit` | Глубина рекурсии, на которой останавливается собственный `ON DELETE CASCADE` базы; тогда клиент даёт `CascadeDepthLimitError`, а глубокий каскад доводит до конца hare — меньшим числом уровней за раз | `SQLITE_LIMIT_TRIGGER_DEPTH` сборки (1000; 100 у официальной библиотеки 3.37.2) | нет | нет |
| `supports_nulls_distinct` | Ограничение уникальности принимает `NULLS [NOT] DISTINCT` | нет | с 15 | с 15 |
| `supports_partitioned_exclusion_constraints` | Партиционированная таблица принимает ограничение-исключение | нет | с 17 | с 17 |

Если возможности нет, операция, которой она нужна, даёт `UnSupportedError` с её названием до отправки
чего-либо: `select_for_update()` в SQLite, `Transactions.distributed()` без двухфазной фиксации,
`stream()` без потоковой передачи.

### Возможности диалекта {: #dialect-capabilities }

Атрибуты `connection.dialect`, которые различаются у встроенных диалектов:

| Атрибут | Что означает | `sqlite` | `postgresql` |
|---|---|---|---|
| `supports_schemas` | Таблицу можно указать вместе со схемой (`Meta.schema`); без этого схема не учитывается | нет | да |
| `supports_distinct_on` | `SELECT DISTINCT ON (...)` — `distinct("field")` | нет | да |
| `sorts_nulls_first` | При сортировке по возрастанию `NULL` по умолчанию идёт раньше всех значений | да | нет |
| `enforces_numeric_ranges` | Целые и десятичные колонки сами отклоняют значения вне диапазона; без этого их проверяет hare | нет | да |
| `guarantees_returning_order` | `INSERT ... RETURNING` на много строк возвращает строки в порядке вставки — `bulk_create(returning=True)` | нет | да |
| `supports_conflict_constraint_names` | `bulk_create(on_conflict_constraint=...)` | нет | да |
| `supports_conflict_where` | `bulk_create(conflict_where=...)` | нет | да |
| `supports_copy` | Массовая загрузка через протокол `COPY` — `bulk_create(use_copy=True)` | нет | да |
| `supports_virtual_generated_columns` | Вычисляемая колонка может вычисляться при чтении (`stored=False`) | да | нет |
| `isolation_levels` | Уровни изоляции, на которых выполняется транзакция, — см. [Уровень изоляции](../connections/transactions.ru.md#isolation-level) | `SERIALIZABLE` | все четыре |
| `max_identifier_length` | Наибольшая длина имени в байтах. hare держит каждое сгенерированное имя в пределах 63 байт, укорачивая более длинное с хэшем, и не регистрирует диалект с меньшим пределом | нет | 63 |
| `supports_adding_constraints` | `ALTER TABLE ... ADD CONSTRAINT`; без этого изменение ограничения позже пересоздаёт таблицу | нет | да |
| `supports_partial_indexes` | Индекс принимает условие `WHERE` | да | да |
| `supports_exclusion_constraints` | `ExclusionConstraint` | нет | да |
| `supports_deferrable_constraints` | `deferrable=True` у ограничения или триггера | нет | да |
| `supports_index_nulls_order` | Ключ индекса задаёт место `NULL` | нет | да |
| `supports_concurrent_indexes` | Операции с индексами `CONCURRENTLY` | нет | да |
| `supports_not_valid_constraints` | Ограничение добавляется как `NOT VALID` и проверяется позже | нет | да |
| `supports_statement_triggers` | `TriggerForEach.STATEMENT` | нет | да |
| `supports_extensions` | `CREATE EXTENSION` | нет | да |
| `supports_collations` | `CREATE COLLATION` | нет | да |
| `supports_foreign_keys` | Внешние ключи проверяются базой; без этого каждое действие `on_delete` выполняет hare | да | да |
| `supports_unique_constraints` | Уникальность проверяется базой | да | да |
| `minimum_server_version` | Самая старая версия сервера, с которой работает hare | 3.35.0 | 14 |

Каждый атрибут описан в `hare.dialects.base.dialect.Dialect`. Как диалект хранит таблицу — `WITHOUT
ROWID` в SQLite, табличное пространство, `UNLOGGED`, параметры хранения и
[партиционирование](../models/meta-options.ru.md#partitioning) (хеш-, списочные и диапазонные партиции,
которые добавляют и удаляют миграции) в PostgreSQL, — задаётся для каждой модели через
[`Meta.table_options`](../models/meta-options.ru.md#table_options).

## `DialectRegistry` {: #dialectregistry }

```python
from hare.dialects.registry import DialectRegistry

DialectRegistry.get_dialect("postgresql")                  # объект диалекта
DialectRegistry.get_driver("postgresql+asyncpg")           # драйвер, который называет engine
DialectRegistry.get_driver_for_url_scheme("sqlite")        # драйвер, который выбирает схема адреса
DialectRegistry.get_dialects(), DialectRegistry.get_drivers()
```

Неизвестное имя или схема дают `ConfigurationError` со списком зарегистрированных. Встроенный
диалект или драйвер импортируется и регистрируется, только когда его называет подключение или поиск,
поэтому `Hare.init()` на `sqlite://` не импортирует ничего от PostgreSQL, а `Hare.init()` на
`postgresql://` — ничего от SQLite; `get_dialects()` перечисляет встроенные диалекты первыми, всегда
в одном порядке. Имя
или схема, которых нет у встроенных драйверов, а также `get_dialects()`/`get_drivers()` загружают
каждый модуль, названный в группе точек входа `hare.dialects` установленного пакета, — сторонний
диалект регистрирует свои драйверы при импорте:

```toml
[project.entry-points."hare.dialects"]
clickhouse = "hare_clickhouse.driver"
```

После этого его `engine` и схема адреса работают так же, как встроенные. Как написать свой диалект —
в руководстве [Как написать диалект](../extending/writing-a-dialect.ru.md).

### Регистрация после `Hare.init()` {: #registering-after-init }

Диалект, драйвер, способ вывода выражения или запись типа диалекта, оператор фильтра из
`register_lookup()`, часть пути внутри значения и метод `QuerySet` от диалекта можно регистрировать в
любой момент. Каждая регистрация сбрасывает всё, что hare сохранил из реестров, — SQL запросов, планы
чтения строк, описания фильтров и сортировок — и заново собирает фильтры каждой привязанной модели,
поэтому следующий запрос уже использует новое; запрос, построенный раньше, выполняется со своим SQL.
Обычно регистрация происходит при импорте, до `Hare.init()`, когда сохранённого ещё ничего нет и
сброс ничего не стоит.

## Тесты по возможностям {: #tests-by-capability }

[`requires_features()`](../testing.ru.md#requires-features) пропускает тест, если у подключения нет
нужного ему — поля `Features`, атрибута диалекта или имени диалекта:

```python
@requires_features(supports_distinct_on=True)
async def test_latest_per_author(db): ...
```
