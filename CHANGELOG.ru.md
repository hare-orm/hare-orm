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

### Breaking changes

- `VersionedModel.id` и `OutboxEvent.id` требуют UUID7, в том числе в фильтрах и ссылках через связи.
  Создавайте эти идентификаторы через `Uuid7.new`; при объединении с базовой моделью, объявляющей `id`,
  ставьте `VersionedModel` первым.
- `makemigrations --rename` отклоняет ответ, который ничего не переименовывает, вместо записи миграции,
  удаляющей и заново создающей объект. Исправьте имена перед генерацией миграции.

### Added

- `UUIDField(version=...)` ограничивает значения UUID указанной версией.
- `HareDTO` поддерживает `extra_fields`, `computed` и `related_values`; `pydantic_model_creator()` —
  `extra_fields` и `related_values`, в том числе объявления на вложенных связанных моделях.
- `Tenancy.scope(...)` с настройками по моделям работает со схемами арендаторов и политиками RLS.
- Проверки безопасности миграций сообщают о добавлении обязательного поля без Python- или DB-default.

### Fixed

- Проверки вхождения в подзапрос согласованно обрабатывают NULL и ключи наборов в `Subquery`.
  Фильтры через связи передают текущий срез подзапроса при повторном использовании плана. Подсчёт,
  проверка наличия строк и операции над множествами сохраняют соединения и колонки DISTINCT,
  добавленные сортировкой; итоговые агрегаты раскрывают составные ключи группировки.
  Рекурсивные запросы, объявленные до инициализации, проверяются при повторении отложенных вызовов.
- Алиасы составных связей сохраняют операции над ключами, а проверки NULL требуют булевы значения.
  Проверка наличия строк отвергает раздутые агрегаты в HAVING. Явное возвращение значений при
  пакетном обновлении отвергается до SQL на базах без RETURNING.
- Итераторы и курсоры сохраняют повторные строки, запросы моделей без ключа, distinct без сортировки
  и аннотации конкретных СУБД. Границы учитывают хранимое представление поля, в том числе Float32,
  метки enum, JSON и явное размещение NULL рядом с NaN.
- Срезанная предзагрузка сохраняет маски частичных полей, составные ключи, JOIN, повторные строки
  и выбор соединения. Вложенная предзагрузка сохраняет нужные ключи промежуточных моделей в `only()`
  и сообщает об отсутствии ключей у частичного экземпляра через `QueryError`.
- Аннотации не смешиваются с одноимёнными путями связей и служебными колонками, включая различия
  регистра и длинные идентификаторы. Группировка, операции над множествами, типы обобщённых связей,
  отрицание фильтров связей, `latest()`/`earliest()` и `in_bulk()` сохраняют семантику запроса.
- `RETURNING` правильно читает модели без ключа и ключи с преобразованием чтения; старые значения
  сохраняют aliases. Отчёты bulk-конфликтов учитывают подготовленные defaults, арендаторов и ключи
  поиска зашифрованных полей.
- Generated-поля сохраняют преобразования и сравнения выходного поля. Defaults окон, выражения
  длительности и литералы выражений записи сохраняют семантику объявленного поля. Недоступные функции
  выбранного диалекта и JSON-агрегаты неподдерживаемых вложенных типов отклоняются до SQL.
- Кэши областей арендаторов учитывают изменения живых моделей и изолированы между контекстами приложения.
- Дополнительные поля и связанные значения Pydantic учитывают `include` и `exclude`, включая вложенные
  схемы. Объявления request query принимают структурно совместимые настройки Meta без требования точных типов.
- Сериализация миграций сохраняет aliases импортов в копируемых функциях, декораторах, defaults
  и аннотациях, включая области lambda. Проверки схемы сохраняют различные Unicode-имена SQLite;
  миграции арендаторов сохраняют однопроходные итераторы целевых миграций.
- Откат восстанавливает состояние экземпляра, изменённое записью; отменённый `BEGIN` не оставляет
  повторно используемое соединение в незавершённой транзакции. Удаление последней подписки outbox wakeup
  закрывает слушатель в том цикле, который его открыл.
- Настройка соединений переводит ошибки конфигурации; проверки drift сохраняют цели связей на
  identity-колонки вне PK, а типовые заглушки сохраняют перекрытые импорты и вложенные литеральные типы.

## [0.11.0] - 2026-10-27

### Breaking changes

- `bulk_create(ignore_conflicts=True)` возвращает вставленные объекты, и только они помечаются сохранёнными;
  объектам, ключи которых генерирует база, нужен `on_conflict=[...]`.
- `JSONField(field_type=...)`, файлы конфигурации YAML и `Faker()` требуют extras `pydantic`, `yaml` и
  `faker`.
- Транзакция `postgresql+asyncpg` отказывает оператору из другого цикла событий, а база SQLite в памяти —
  другому циклу событий; оба — с `ConfigurationError`.
- `bulk_update()` у QuerySet со срезом или с `distinct(*fields)` даёт `QueryError`, как `update()`; раньше
  он записывал объекты вне среза.
- Условие ограничения или индекса, политика или `GeneratedField`, читающие связанную модель, отклоняются
  при привязке модели (`ConfigurationError`), а не её миграцией.
- Фильтр `FloatField` по `NaN` в SQLite даёт `ValidationError` — SQLite подставлял его как `NULL`.

### Added

- Outbox: `ChangeCapture(extend_many=...)` и `ChangeCapture.replace()`, `OutboxEvent.enqueue_many()`,
  шаблоны тем и `exclude_topics` у `OutboxRelay`, `WebhookDelivery` подписчикам (`target`, `allows_url`,
  `result_field`, `signature_header`).
- `Q` как значение в `annotate()`, `alias()`, `values()`, `values_list()` и `update()` (`ConditionValue`).
- Фикстуры pytest-плагина `hare_app_db` и `hare_app_transactional_db` для тестируемого приложения.
- `VersionedModel.get_last_visible_version_or_exception()`, `Uuid7`, `ClickhouseRequestQuery`,
  `Field.get_json_value()`, `prefetch_related(None)`.

### Changed

- Ключи `VersionedModel` и `OutboxEvent` — UUID версии 7; `ListenNotifyWakeup` отправляет один `NOTIFY` на
  тему в транзакции; тестовая база PostgreSQL удаляется вместе с её сессиями.

### Fixed

- Миграции outbox-моделей, параметризованного `field_type` и индекса-выражения над внешним ключом;
  `makemigrations` без `asyncpg`; события изменений полей любого типа; чистый wheel; валидаторы `UUIDField`,
  получившего текст; JSON схемы pydantic с NaN и бесконечностью; `count()`, `exists()` и страница
  запроса из параметров с предзагрузкой, `Exists()` QuerySet с предзагрузкой.
- ClickHouse: первый `atomic()` процесса на подключении с `transactions=true` и `autonomous()` с параметром,
  который шёл без транзакции; блокировка миграций в Keeper у базы с `/` в имени; серии ключей базы, чьё имя —
  начало имени другой, и серии тестовой базы, остававшиеся в Keeper; индекс `bloom_filter`, записанный без
  аргумента, — ложное расхождение схемы.
- Захват изменений: запись в открытой транзакции, чьё событие не записалось, сохраняла строку;
  `InProcessWakeup`, вызванный из потока со своим циклом событий.
- `bulk_create(ignore_conflicts=True)` сопоставлял строки по порядку ключей словаря (hstore, jsonb).
- `inspectdb` и расхождения схемы: колонки SQLite `DATETIME`/`DOUBLE`/`FLOAT` и порядок индексов; проверка
  `IN` над `varchar` и последовательность, общая для таблиц, в PostgreSQL.
- Настройки или их раздел, не являющиеся словарём, дают `ConfigurationError`, а не `TypeError`; параметр
  `transactions` интеграций приложений проверяется.

## [0.10.0] - 2026-10-26

### Breaking changes

- `DecimalField` в SQLite отклоняется.
- `Avg`, `StdDev` и `Variance` возвращают то, что считает база (`Decimal` в PostgreSQL).
- Настройки, написанные в коде, больше не проверяются.
- `MEMORY_SQLITE`/`init_memory_sqlite()` теперь `MEMORY_DATABASE`/`init_memory_database()`.
- `Order` перенесён в `hare.sql.order`; SQL и возможности одной базы перенесены в её диалект.
- `get_model()` принимает одну метку `"app.Model"` вместо `(app_label, model_name)`.

### Added

- `Meta.api_getter` запросов из HTTP-параметров, откат миграций, `<Model>Updates` и новые типы для mypy и
  `hare stubs`, `CHECK` полей-перечислений, проверки сужения в `AlterField`.
- Все API, принимающие модель, принимают и её метку `"app.Model"`.

### Fixed

- Ошибки записи, запросов, миграций, inspectdb и ClickHouse, найденные проверкой и поиском ошибок.

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
  [Проверка типов запросов](https://hare-orm.github.io/hare-orm/ru/querying/type-checking/).
- **Документация** на английском и русском и бенчмарки против SQLAlchemy, Tortoise ORM, yara-orm и
  Django — см. [Бенчмарки](https://hare-orm.github.io/hare-orm/ru/benchmarks/).

[Unreleased]: https://github.com/hare-orm/hare-orm/compare/v0.11.0...dev
[0.11.0]: https://github.com/hare-orm/hare-orm/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/hare-orm/hare-orm/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/hare-orm/hare-orm/releases/tag/v0.9.0
