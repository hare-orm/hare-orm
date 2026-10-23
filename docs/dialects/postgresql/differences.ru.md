# PostgreSQL: что работает иначе

Там, где SQLite или ClickHouse вычисляют что-то по-своему, hare оставляет их результат как есть — см.
[SQLite: что работает иначе](../sqlite/differences.ru.md#results) и
[ClickHouse: что работает иначе](../clickhouse/differences.ru.md#results). Отличия PostgreSQL
— в том, что делает сам сервер: как он завершает транзакцию после ошибки, какой длины может быть имя, что
добавляет каждая версия сервера. Каждая строка ниже говорит, что с этим делает hare.

## <a id="transactions"></a>Транзакции

| PostgreSQL | hare |
|---|---|
| Неудачная команда прерывает транзакцию | Выход из блока без исключения откатывает её и даёт `TransactionManagementError`; вложенный `atomic()` вокруг команды сохраняет внешнюю транзакцию рабочей — см. [Неудачная команда в транзакции](../../connections/transactions.ru.md#failed-statement). |
| `CREATE INDEX CONCURRENTLY` только вне транзакции | `AddIndex(concurrently=True)` требует `atomic = False` у своей миграции — см. [Индексы](../../migrations/operations.ru.md#index-operations). |
| Ожидание блокировки без предела | `migrate(lock_timeout=...)` ограничивает, сколько миграция ждёт блокировку таблицы, — см. [Ожидание блокировок](../../migrations/migrations.ru.md#lock-timeout). |

## <a id="values-and-names"></a>Значения и имена

| PostgreSQL | hare |
|---|---|
| Часовой пояс сеанса из настроек сервера | Оба драйвера ведут каждый сеанс в UTC — см. [Часовой пояс сеанса](connecting.ru.md#session-time-zone). |
| Имена, обрезанные до 63 байт | Имя, которое создаёт hare, укладывается в 63 байта, более длинное сокращается с хэшем; явное имя длиннее 63 байт даёт `ConfigurationError` — см. [`table`](../../models/meta-options.ru.md#table). |
| Место `NULL` в сортировке | `NULL` сортируется как наибольшее значение, последним по возрастанию — см. [Порядок NULL при сортировке](../../querying/queryset-methods.ru.md#null-ordering). |
| Порядок текста по правилу сравнения базы | Текст сравнивается и сортируется по правилу сравнения базы; `Collate(...)` просит другое — см. [Функции базы данных](../../querying/functions.ru.md). |
| Диапазоны целых и десятичных типа колонки | Колонка сама отклоняет значение вне своего диапазона. |

## <a id="server-versions"></a>Версии сервера

hare читает версию сервера при подключении и включает то, что в ней есть; на более старом сервере
возможность отклоняется — см. [таблицу возможностей](../dialects-and-features.ru.md#database-features).

| Версия | Добавляет |
|---|---|
| 15 | `merge()`; `UniqueConstraint(nulls_distinct=False)` |
| 16 | `explain(generic_plan=True)` |
| 17 | `merge().returning()` и `when_not_matched_by_source()`; `JsonTable`; `ExclusionConstraint` на секционированной таблице; `explain(memory=True)`, `explain(serialize=True)` |
| 18 | `VIRTUAL` `GeneratedField` (`stored=False`); `db_default` `UuidV7()`; `without_overlaps=True`; `returning(old=...)` |

То, что более старый сервер делает сам, hare оставляет ему:

- PostgreSQL до 18 отказывается приводить JSON `null` пути `JSONField` к другому типу
  (`Cast(F("data__n"), IntField())` — *cannot cast jsonb null to type integer*); PostgreSQL 18 даёт `NULL`.
- PostgreSQL до 18 не сообщает пулеру транзакций путь поиска — опция `schema` и схема на арендатора за
  таким пулером отклоняются (см. [Пулеры подключений](../../connections/connection-poolers.ru.md#what-hare-does)).

## <a id="schema"></a>Схема и миграции

| PostgreSQL | hare |
|---|---|
| Расширения для векторов, геоданных, триграмм и некоторых типов | `Meta.extensions` объявляет их, и автодетектор миграций пишет `CreateExtension` для каждого — см. [Расширения](../../migrations/operations.ru.md#extensions). |
| Секционированные таблицы, типы индексов | См. [Индексы и таблицы](indexes-and-tables.ru.md). |
| Схемы | `Meta.schema`, схема на арендатора и построчная защита работают как написано — см. [Схема на арендатора](../../soft-delete-versions-tenants/schema-per-tenant.ru.md). |

## <a id="drivers"></a>Драйверы

`rust_pg` и `asyncpg` дают одинаковые результаты через API hare; готовый SQL различается тем, как два
драйвера привязывают и читают значения, — см.
[Готовый SQL на двух драйверах](connecting.ru.md#driver-differences).
