# Журнал изменений

[English](CHANGELOG.md)

Здесь записывается каждое изменение hare-orm, заметное пользователю. Формат следует
[Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), а номера версий —
[семантическому версионированию](https://semver.org/lang/ru/) так, как описано в разделе
[Версии и выпуски](README.ru.md#версии-и-выпуски).

Изменения каждого выпуска перечисляются в этих группах, в этом порядке, пустые группы опускаются:

- **Breaking changes** — публичный API, который убран, переименован или теперь ведёт себя иначе.
  Каждая запись говорит, что поменять в вашем коде.
- **Added** — новые возможности и новый публичный API.
- **Changed** — изменённое поведение, не требующее правок в вашем коде.
- **Fixed** — исправленные ошибки.
- **Removed** — возможности, убранные без замены.
- **Security** — исправленные уязвимости со ссылками на их описания.

Изменения, влитые в `dev` после последнего выпуска, собираются в разделе **Unreleased**, а при
выпуске этот раздел получает номер версии и дату.

## [Unreleased]

## [0.9.0] - 2026-10-23

Первый публичный выпуск hare-orm — асинхронной ORM на Python для PostgreSQL, SQLite и ClickHouse. В него
перенесена вся кодовая база прежней закрытой разработки проекта: hare-orm начиналась как форк
[Tortoise ORM](https://github.com/tortoise/tortoise-orm) и содержит доработанную копию построителя
запросов [PyPika](https://github.com/kayak/pypika) (см. [NOTICE](NOTICE.md)); с тех пор большая часть
обоих переписана. Всё перечисленное ниже — новое в этом выпуске.

Работает на CPython 3.12, 3.13 и 3.14 с PostgreSQL 14 и новее, SQLite 3.35.5 и новее и ClickHouse 25.8
и новее — см. [Установка](https://hare-orm.github.io/hare-orm/ru/getting-started/installation/).
Колёса с Rust-расширением собраны для Linux (glibc и musl), macOS и Windows.

### Added

- **Модели** — классы `Model` с типизированными полями, связями и опциями `Meta`: внешние ключи, связи
  один-к-одному и многие-ко-многим (в том числе через промежуточную модель), обобщённые связи из
  настоящих внешних ключей, составные первичные ключи везде — и как цель внешнего ключа — и модели без
  первичного ключа; валидаторы, `db_default` и вычисляемые столбцы, шифрованные поля со слепыми
  индексами и чувствительные поля, скрытые в ошибках и логах; модели, построенные во время работы по
  таблице — см. [Типы полей](https://hare-orm.github.io/hare-orm/ru/models/field-types/),
  [Связи](https://hare-orm.github.io/hare-orm/ru/models/relations/) и
  [Опции Meta](https://hare-orm.github.io/hare-orm/ru/models/meta-options/).
- **Запросы** — ленивый `QuerySet` с лукапами в стиле Django через связи, выражения `Q`/`F`,
  аннотации, агрегация (с наборами группировок), оконные функции, `Case`/`When`,
  `Subquery`/`Exists`/`OuterReference`, операции над множествами, CTE и рекурсивные запросы по дереву,
  пагинация по ключу, `values()`/`values_list()`, `select_related()`/`prefetch_related()` с фильтрами,
  массовые создание и обновление, upsert, `MERGE`, `select_for_update()`, готовый SQL через
  `RawSQLTerm` и построитель запросов — и ошибка вместо агрегата, который JOIN к связи «ко многим»
  молча раздул бы — см. [Методы QuerySet](https://hare-orm.github.io/hare-orm/ru/querying/queryset-methods/),
  [Фильтры](https://hare-orm.github.io/hare-orm/ru/querying/filters/) и
  [Выражения](https://hare-orm.github.io/hare-orm/ru/querying/expressions/).
- **Планы запросов** — запрос уже встречавшейся формы выполняется по SQL, построенному для первого, и
  только подставляет свои значения; повторные `get()`, фильтры, записи и сохранения экземпляров вовсе
  не строят SQL — см. [Кэш планов запросов](https://hare-orm.github.io/hare-orm/ru/querying/query-plan-cache/).
- **Схема в `Meta`** — ограничения (и `EXCLUDE`), частичные индексы и индексы по выражениям,
  триггеры, представления и материализованные представления, функции базы, последовательности,
  политики защиты строк, права доступа, секционирование таблиц и опции таблиц каждой базы; их создают
  и меняют миграции — см. [Ограничения и триггеры](https://hare-orm.github.io/hare-orm/ru/models/constraints-and-triggers/),
  [Индексы](https://hare-orm.github.io/hare-orm/ru/models/indexes/) и
  [Объекты схемы](https://hare-orm.github.io/hare-orm/ru/models/schema-objects/).
- **Поведение, объявленное в `Meta`** — мягкое удаление с восстановлением, версионные строки,
  мультиарендность по столбцу, по схеме или через защиту строк, оптимистичная блокировка,
  отслеживание изменённых полей и захват изменений в транзакционный outbox — см.
  [Мягкое удаление](https://hare-orm.github.io/hare-orm/ru/soft-delete-versions-tenants/soft-delete/),
  [Версионные модели](https://hare-orm.github.io/hare-orm/ru/soft-delete-versions-tenants/versioned-models/) и
  [Мультиарендность](https://hare-orm.github.io/hare-orm/ru/soft-delete-versions-tenants/multi-tenancy/).
- **Миграции** — миграции, найденные по изменениям моделей, записанные в файлы или выполняемые без
  них, сжатые, проверенные на операции, блокирующие нагруженную таблицу (правила миграций без
  простоя), сверенные с живой базой (`hare drift`) и построенные по существующей базе
  (`hare inspectdb`); CLI `hare` с командами-плагинами — см.
  [Миграции](https://hare-orm.github.io/hare-orm/ru/migrations/migrations/),
  [Операции](https://hare-orm.github.io/hare-orm/ru/migrations/operations/) и
  [CLI](https://hare-orm.github.io/hare-orm/ru/migrations/cli/).
- **Соединения и транзакции** — пулы с проверкой здоровья, вложенные транзакции с точками
  сохранения, уровни изоляции, транзакции только для чтения и таймауты операторов, колбэки
  `on_commit()`/`on_rollback()`, реплики для чтения и маршрутизаторы с чтением своих записей,
  несколько баз, распределённые транзакции с двухфазной фиксацией, PgBouncer в режиме транзакций —
  см. [Транзакции](https://hare-orm.github.io/hare-orm/ru/connections/transactions/) и
  [Несколько баз](https://hare-orm.github.io/hare-orm/ru/connections/multiple-databases/).
- **Подключаемые диалекты** — SQLite, PostgreSQL и ClickHouse — диалекты на публичном API
  `hare.dialects`, которым пользуется и сторонний пакет; база, где нет нужной возможности, даёт
  понятную ошибку до отправки SQL, а не неверный результат — см.
  [Диалекты и возможности](https://hare-orm.github.io/hare-orm/ru/dialects/dialects-and-features/) и
  [Как написать диалект](https://hare-orm.github.io/hare-orm/ru/extending/writing-a-dialect/).
- **PostgreSQL** — Rust-драйвер (`rust_pg`), выбираемый по умолчанию для адресов `postgresql://`,
  который разбирает строки и преобразует значения в Rust, и драйвер `asyncpg`; массивы, диапазоны,
  hstore, ltree, сетевые типы и собственные перечисления, индексы GIN/GiST/BRIN/Bloom/Hash/SP-GiST,
  поиск по триграммам и `LISTEN`/`NOTIFY` — см.
  [Поля PostgreSQL](https://hare-orm.github.io/hare-orm/ru/dialects/postgresql/fields/) и
  [LISTEN/NOTIFY](https://hare-orm.github.io/hare-orm/ru/dialects/postgresql/listen-notify/).
- **SQLite** — через `aiosqlite`, с точными десятичными числами, регулярными выражениями и
  функциями дат, текста и математики, которых нет в SQLite, зарегистрированными на соединении;
  схемы — это подключённые базы — см.
  [SQLite: что работает иначе](https://hare-orm.github.io/hare-orm/ru/dialects/sqlite/differences/).
- **ClickHouse** — по HTTP (`clickhouse-connect`) и по собственному протоколу (`clickhouse-driver`):
  движки MergeTree и их опции таблиц, кластеры с таблицами `Distributed`, словари, проекции,
  `ARRAY JOIN`, `ASOF JOIN`, `LIMIT BY`, собственные агрегаты и типы ClickHouse — см.
  [Модели ClickHouse](https://hare-orm.github.io/hare-orm/ru/dialects/clickhouse/models/) и
  [ClickHouse: что работает иначе](https://hare-orm.github.io/hare-orm/ru/dialects/clickhouse/differences/).
- **Поиск и геоданные** — полнотекстовый поиск (PostgreSQL, SQLite FTS5), векторный поиск (pgvector,
  sqlite-vec) и GIS (PostGIS, SpatiaLite, геометрические типы ClickHouse) — см.
  [Полнотекстовый поиск](https://hare-orm.github.io/hare-orm/ru/dialects/search-and-geodata/full-text-search/),
  [Векторный поиск](https://hare-orm.github.io/hare-orm/ru/dialects/search-and-geodata/vector-search/) и
  [GIS](https://hare-orm.github.io/hare-orm/ru/dialects/search-and-geodata/gis/).
- **Интеграции** — запросы из параметров HTTP-запроса, проверенные и разбитые на страницы;
  интеграции с Litestar, FastAPI и Robyn; схемы Pydantic по моделям; транзакционный outbox с
  доставкой в Kafka, RabbitMQ, Redis Streams, вебхуки и taskiq — см.
  [Запросы из параметров](https://hare-orm.github.io/hare-orm/ru/integrations/request-queries/),
  [Веб-фреймворки](https://hare-orm.github.io/hare-orm/ru/integrations/web-frameworks/) и
  [Outbox](https://hare-orm.github.io/hare-orm/ru/integrations/outbox/).
- **Наблюдаемость** — наблюдатели запросов и записей, спаны и метрики OpenTelemetry, теги-комментарии
  в SQL, детектор повторяющихся запросов (N+1) и здоровье пулов — см.
  [Наблюдатели](https://hare-orm.github.io/hare-orm/ru/observability/observers/) и
  [OpenTelemetry](https://hare-orm.github.io/hare-orm/ru/observability/opentelemetry/).
- **Тесты и проверка типов** — плагин pytest с изолированными тестовыми базами, фабрики и проверки
  числа запросов; плагин mypy, отвергающий опечатки в фильтрах и типизирующий строки `values()`, и
  заглушки для pyright — см. [Настройка тестов](https://hare-orm.github.io/hare-orm/ru/testing/setup/) и
  [Проверка типов](https://hare-orm.github.io/hare-orm/ru/querying/type-checking/).
- **Документация** на английском и русском и бенчмарки против SQLAlchemy, Tortoise ORM, yara-orm и
  Django — см. [Бенчмарки](https://hare-orm.github.io/hare-orm/ru/benchmarks/).

[Unreleased]: https://github.com/hare-orm/hare-orm/compare/v0.9.0...dev
[0.9.0]: https://github.com/hare-orm/hare-orm/releases/tag/v0.9.0
