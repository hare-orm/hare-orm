# SQLite: что работает иначе

SQLite — библиотека, которая пишет один файл: у неё нет сервера, писать одновременно может одно
подключение, точных десятичных типов и типов дат нет, а колонки таблицы она меняет в основном построением
таблицы заново. Каждая строка ниже говорит, что делает hare с частью SQL, которой в SQLite нет или
которую SQLite понимает по-своему. Чего в SQLite нет совсем — `MERGE`, `LATERAL`, последовательностей,
построчной защиты и остального, — см. в [таблице
возможностей](../dialects-and-features.ru.md#database-features).

## <a id="connections-and-transactions"></a>Подключения и транзакции

| SQLite | hare |
|---|---|
| Один пишущий за раз | hare держит одно соединение на имя подключения и берёт его как из пула: транзакция занимает его до конца, а запрос другой задачи на том же имени подключения ждёт её — см. [Снимок пула](../../observability/pool-health.ru.md#pool-status). Другое подключение к файлу ждёт блокировку SQLite не дольше `busy_timeout`. |
| Уровни изоляции | Каждая транзакция идёт на `SERIALIZABLE`; `atomic()` принимает любой уровень — см. [Уровень изоляции](../../connections/transactions.ru.md#isolation-level). |
| Транзакции только для чтения | На время транзакции включается `PRAGMA query_only` — см. [Транзакции только для чтения](../../connections/transactions.ru.md#read-only-and-statement-timeouts). |
| Транзакция, прерванная ошибкой | Неудачная команда оставляет транзакцию рабочей — см. [Неудачная команда в транзакции](../../connections/transactions.ru.md#failed-statement). Прерванная запись откатывает всю транзакцию вместе с точками сохранения, и hare затем считает транзакцию прерванной — см. [Транзакции только для чтения и ограничение времени команды](../../connections/transactions.ru.md#read-only-and-statement-timeouts). |
| Блокировки строк (`SELECT ... FOR UPDATE`) | `select_for_update()` при выполнении запроса бросает `UnSupportedError` — см. [`select_for_update()`](../../querying/queryset-methods.ru.md#select-for-update). |
| База в памяти, общая для подключений | Каждое подключение `:memory:` — отдельная база: `autonomous()` и второе имя подключения не видят её таблиц. |

## <a id="values"></a>Значения

| SQLite | hare |
|---|---|
| Точный десятичный тип | `DecimalField` хранится текстом и сравнивается по точному значению — см. [Десятичные числа](functions.ru.md#decimals). |
| Типы дат и времени | Даты, время и даты-время хранятся текстом ISO; усекают, извлекают и сдвигают их функции hare — см. [Функции](functions.ru.md#registered-functions). |
| Диапазоны целых типа колонки | Любое 64-битное целое помещается в любую целую колонку, поэтому hare проверяет записываемое значение по диапазону поля, в том числе в `update()` из выражения, и отклоняет целый результат арифметики вне диапазона (`integer out of range`). |
| Отрицательный ноль | `-0.0` у `FloatField` читается обратно как `0.0`. |
| Место `NULL` в сортировке | `NULL` сортируется как наименьшее значение, первым по возрастанию — см. [Порядок NULL при сортировке](../../querying/queryset-methods.ru.md#null-ordering). |
| Порядок текста по локали | Текст сравнивается по кодовым точкам. |
| `jsonb` | `JSONField` имеет тип `JSON_TEXT`; функции hare сравнивают, упорядочивают и выводят его как `jsonb` — см. [JSON](functions.ru.md#json). |

## <a id="queries"></a>Запросы

| SQLite | hare |
|---|---|
| Строки многострочного `RETURNING` в порядке вставки | `bulk_create(returning=True)` требует pk у каждого объекта и сопоставляет строки по нему — см. [`bulk_create()`](../../querying/queryset-methods.ru.md#bulk-create). |
| `DISTINCT ON` | `distinct(*fields)` выбирает те же строки по номеру строки — см. [`distinct()`](../../querying/queryset-methods.ru.md#distinct). |
| Наборы группировок | `Rollup`, `Cube`, `GroupingSets` и `Grouping()` бросают `UnSupportedError` — см. [Несколько группировок](../../querying/aggregation.ru.md#grouping-sets). |
| Регулярные выражения POSIX | `__posix_regex` и `__iposix_regex` требуют `install_regexp_functions=true` — см. [Регулярные выражения](functions.ru.md#regular-expressions). |
| Больше 64 таблиц в одном запросе | Запрос падает с `OperationalError`, который говорит об этом; сократите пути по связям. |
| Длинные списки `__in` как параметры | Начиная с 20 значений список передаётся одним массивом JSON, который читает `json_each()` — см. [Длинные списки `__in`](../../querying/filters.ru.md#large-in-lists). |

## <a id="schema"></a>Схема и миграции

| SQLite | hare |
|---|---|
| `ALTER TABLE` для большинства изменений колонок и ограничений | Таблица перестраивается: копия создаётся по модели, заполняется из старой таблицы и переименовывается на её место — см. [Перестройка таблицы (SQLite)](../../migrations/migrations.ru.md#sqlite-table-rebuild). |
| Проверка внешних ключей во время перестройки | Каждая миграция идёт с `PRAGMA foreign_keys = OFF` и проверяет строки через `PRAGMA foreign_key_check` — см. [Внешние ключи во время миграции (SQLite)](../../migrations/migrations.ru.md#sqlite-foreign-key-check). |
| Схемы | `Meta.schema` не учитывается: таблица создаётся и запрашивается без схемы. Схема на арендатора и построчная защита бросают `UnSupportedError` — см. [Схема на арендатора](../../soft-delete-versions-tenants/schema-per-tenant.ru.md). |
| Триггеры на команду, отложенные триггеры и ограничения, выключенный триггер | `TriggerForEach.STATEMENT`, `deferrable=True` и `enabled=False` бросают `UnSupportedError` — см. [Триггеры](../../models/constraints-and-triggers.ru.md#triggers). |
| `NULLS NOT DISTINCT` | `UniqueConstraint(nulls_distinct=False)` бросает `ConfigurationError` при привязке модели. |
| Колонки `INCLUDE`, место `NULL` в ключе индекса | Индекс создаётся без колонок `include`; ключ, задающий место `NULL`, бросает `UnSupportedError` — см. [Индексы](../../models/indexes.ru.md). |
| `LIKE` в условии ограничения или индекса | Текстовый оператор фильтра записывается как `GLOB` и находит те же строки с учётом регистра — см. [Ограничения](../../models/constraints-and-triggers.ru.md#constraints). |
| Функции hare вне hare | Ограничение или индекс, которым нужна такая функция, бросают `UnSupportedError` — см. [В ограничениях и индексах](functions.ru.md#in-constraints-and-indexes). |
| DDL правил сравнения и расширений | `CreateCollation`/`RemoveCollation` и `CreateExtension`/`RemoveExtension` пропускаются с предупреждением — см. [Правила сравнения](../../migrations/operations.ru.md#raw-operations). |

## <a id="search-and-geodata"></a>Поиск и геоданные

| SQLite | hare |
|---|---|
| Конфигурации текстового поиска | `FullTextIndex` поддерживает таблицу FTS5 триггерами — см. [Полнотекстовый поиск](../search-and-geodata/full-text-search.ru.md). |
| Векторные типы | sqlite-vec, с `load_sqlite_vec=true` — см. [Векторный поиск](../search-and-geodata/vector-search.ru.md). |
| Пространственные типы | SpatiaLite, с `load_spatialite=true` — см. [ГИС](../search-and-geodata/gis.ru.md). |
