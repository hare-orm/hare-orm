# Как написать диалект

hare работает с базой через два объекта. **Диалект** — язык базы: синтаксис SQL, типы колонок, команды
создания и изменения схемы, системный каталог. **Драйвер** к ней подключается: схемы адреса, учётные
данные и класс клиента, который выполняет команды. У одного диалекта может быть несколько драйверов — у
PostgreSQL это `asyncpg` и `rust_pg`.

Новая база добавляется отдельным пакетом; в самом hare при этом ничего не меняется.

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
| Полнотекстовый поиск | `hare.dialects.base.search.text_search.TextSearch` | `text_search` / `build_text_search()` | SQL запросов, векторов, рангов и разметки [`hare.search`](../../dialects/search-and-geodata/full-text-search.ru.md). Необязательно: без него каждое выражение даёт `UnSupportedError`. |
| Редактор схемы | `hare.dialects.base.schema.base_schema_editor.BaseSchemaEditor` | `schema_editor_class` / `build_schema_editor_class()` | Все команды схемы — и для `generate_schemas()`, и для миграций. |
| Чтение схемы | `hare.inspectdb.introspection.SchemaIntrospector` | `introspector_class` / `build_introspector_class()` | Читает существующую схему для `inspectdb` и `hare drift`. Необязательно. |
| Тень базы | `hare.dialects.base.schema.shadow.shadow_database.ShadowDatabase` | `shadow_database_class` / `build_shadow_database_class()` | Где `hare drift` строит рядом с базой схему, которую объявляют модели, — см. [чтение схемы](schema.ru.md#introspector). Необязательно: без неё поиск расхождений даёт `UnSupportedError`. |
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
даёт `UnSupportedError` — команда отклоняется до отправки SQL. То, что несколько баз пишут одинаково
сверх стандарта, — тоже класс `hare.dialects.base`, названный по содержимому:
`LimitReturningConflictQueryClauses` (`LIMIT`/`OFFSET`, `RETURNING`, `ON CONFLICT`, `UPDATE ... FROM`),
от которого наследуются диалекты PostgreSQL и SQLite. Сторонний диалект такой базы начинает с него, любой
другой — с базовых частей.

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
`"engine": "colstore+colstore-client"` его используют. Второй драйвер или диалект под уже занятым именем
или схемой адреса даёт `ConfigurationError`. Регистрация диалекта один раз вызывает его `install()` —
место, где регистрируется то, что он добавляет к собственным классам hare (метод `QuerySet`, часть пути у
поля ядра).

## <a id="checklist"></a>Проверочный список

- [ ] Подкласс `Dialect`: имя, `otel_system_name`, `features` и части, которые база записывает иначе, —
  `build_literals()`, `build_parameters()`, `build_renderers()`, `build_clauses()`,
  `build_transactions()`, `build_types()`, `build_filter_operators()`.
- [ ] Класс запроса с `SQL_CONTEXT = dialect.sql_context`.
- [ ] Клиент: команды, перевод ошибок, `features` из возможностей диалекта и драйвера; транзакции, если
  они есть у базы.
- [ ] Драйвер: имя, схемы, учётные данные, классы клиентов; регистрация при импорте.
- [ ] Точка входа `hare.dialects`.
- [ ] Шаблоны и методы класса редактора схемы, параметры хранения таблицы, чтение схемы — сколько нужно
  базе.
- [ ] Набор тестов hare проходит с `HARE_TEST_DB`, включая `tests/dialects/test_dialect_contract.py`.
