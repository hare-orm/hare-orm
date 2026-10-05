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

Первый публичный выпуск hare-orm, он выйдет как 0.9.0. В него переносится вся кодовая база из
прежней закрытой разработки проекта: hare-orm начиналась как форк
[Tortoise ORM](https://github.com/tortoise/tortoise-orm) и содержит доработанную копию построителя
запросов [PyPika](https://github.com/kayak/pypika) (см. [NOTICE](NOTICE.ru.md)), и большая часть
обоих с тех пор переписана. Всё перечисленное ниже — новое в этом выпуске.

### Added

- **Модели и запросы** — классы `Model` с опциями `Meta`, ленивый `QuerySet` с операторами фильтра
  в стиле Django, выражения `Q`/`F`, вычисляемые значения, агрегаты, оконные функции, `Case`/`When`,
  `Subquery`/`Exists`/`OuterRef`, операции над множествами (`union()`/`intersection()`/
  `difference()`), CTE (`with_cte()`), keyset-пагинация (`after_cursor()`/`before_cursor()`),
  `select_related()`/`prefetch_related()`, массовые создание и обновление, upsert и
  `select_for_update()` — см. [QuerySet API](https://hare-orm.github.io/hare-orm/ru/reference/queryset-api/) и
  [выражения и функции](https://hare-orm.github.io/hare-orm/ru/reference/expressions-and-functions/).
- **Подключаемые диалекты** — SQLite и PostgreSQL — это диалекты на том же публичном API
  `hare.dialects`, которым пользуется сторонний пакет: SQL, типы колонок, DDL, чтение схемы базы и
  возможности версии сервера берутся из диалекта; база без транзакций, внешних ключей или
  уникальных ограничений получает явные ошибки — см. [диалекты](https://hare-orm.github.io/hare-orm/ru/reference/dialects/).
- **Драйвер PostgreSQL на Rust** (`rust_pg`), используемый по умолчанию для адресов
  `postgresql://`: строки разбираются и значения преобразуются на Rust; вместо него доступен
  драйвер `asyncpg` (`postgresql+asyncpg://`).
- **Планы запросов** — запрос уже встречавшейся структуры выполняется по тексту SQL, построенному
  для первого такого запроса, подставляя только свои значения: фильтры с любым оператором и через
  связи, вычисляемые значения и выражения всех видов, подзапросы (в том числе связанные с внешним
  запросом), CTE, операции над множествами, keyset-пагинация, `values()`/`values_list()`,
  `count()`/`exists()`/`aggregate()`, `update()`/`delete()` и собственные выражения PostgreSQL.
  Каждая часть запроса описывает свой план сама (`Plannable.get_plan_description()`) — см.
  [кэширование](https://hare-orm.github.io/hare-orm/ru/reference/caching/).
- **Составные первичные ключи** наравне с обычными — как цели внешних ключей, с табличными
  ограничениями `FOREIGN KEY`, каскадами, соединениями и `prefetch_related()`; модели без
  первичного ключа.
- **Встроенные опции `Meta`** для оптимистической блокировки, мягкого удаления, версий записей,
  разделения данных по арендаторам и отслеживания изменённых полей — см.
  [версии записей и мягкое удаление](https://hare-orm.github.io/hare-orm/ru/reference/versioning-and-soft-delete/)
  и [разделение данных по арендаторам](https://hare-orm.github.io/hare-orm/ru/reference/multi-tenancy/).
- **Декларативные ограничения и триггеры** (`Meta.constraints`, `Meta.triggers`, `EXCLUDE` и
  `CONSTRAINT TRIGGER` PostgreSQL), применяемые при выполнении миграции.
- **Система миграций и CLI в стиле Django** — `hare makemigrations`/`migrate`/`inspectdb`/...,
  миграции строятся по изменениям в моделях — см. [миграции и CLI](https://hare-orm.github.io/hare-orm/ru/reference/migrations-and-cli/).
- **Типы, операторы фильтра и индексы PostgreSQL** в `hare.dialects.postgresql` — `ArrayField`,
  поля диапазонов, `HStoreField`, `PostGISField`, `TSVectorField` и полнотекстовый поиск,
  `VectorField` (pgvector), триграммные операторы, семейство индексов
  GIN/GiST/BRIN/Bloom/Hash/SP-GiST/IVFFlat/HNSW — см.
  [типы и индексы PostgreSQL](https://hare-orm.github.io/hare-orm/ru/reference/postgres-extras/).
- **Транзакции и несколько баз** — вложенные транзакции, маршрутизаторы, распределённые
  транзакции — см. [транзакции и несколько баз](https://hare-orm.github.io/hare-orm/ru/reference/transactions-and-multi-db/).
- **Запросы из HTTP-запроса и веб-фреймворки** — `hare.contrib.request_query` превращает параметры
  HTTP-запроса в проверенный запрос с пагинацией; интеграции с Litestar, FastAPI и Robyn;
  генерация моделей pydantic; очередь исходящих событий (transactional outbox).
- **Наблюдение за запросами** — обработчики запросов, журнал медленных запросов, обнаружение
  повторяющихся запросов (N+1) и спаны OpenTelemetry — см. [наблюдение за запросами](https://hare-orm.github.io/hare-orm/ru/reference/instrumentation/).
- **Помощники для тестов** (`hare.contrib.test`) и документация на английском и русском.

[Unreleased]: https://github.com/hare-orm/hare-orm/commits/dev
