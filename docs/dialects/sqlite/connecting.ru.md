# SQLite: подключение

Как hare открывает базу SQLite: URL и его параметры, подключаемые соединением базы и неисправности
отдельных библиотек SQLite, которые hare оставляет библиотеке.

## <a id="sqlite-connection-parameters"></a>Параметры подключения

Путь к файлу SQLite в `db_url` раскодируется так же, как имя базы PostgreSQL
(`sqlite+aiosqlite://my%20db.sqlite` → `my db.sqlite`). Кроме PRAGMA ниже, параметр адреса или ключ
`credentials` может быть собственной настройкой hare: `attached_databases` ([Присоединённые
базы](#attached-databases)), `install_regexp_functions` ([Регулярные
выражения](functions.ru.md#regular-expressions)), `load_sqlite_vec` ([sqlite-vec](../search-and-geodata/vector-search.ru.md#sqlite)),
`load_spatialite`, `spatialite_path`, `spatialite_proj_database` и `spatialite_metadata`
([SpatiaLite](../search-and-geodata/gis.ru.md#spatialite)) и `connect_max_retries`/`connect_retry_backoff_base_seconds` —
принимаются и не используются, чтобы `DB_URL`, общий с другой базой, тоже работал. Каждый остальной
задаёт PRAGMA соединения. Принимаются только перечисленные ниже, и каждое значение
проверяется до отправки в базу (PRAGMA не принимает значения через параметры запроса):

| Тип | PRAGMA | Значения |
|---|---|---|
| ключевое слово | `journal_mode`, `synchronous`, `temp_store`, `locking_mode`, `auto_vacuum`, `secure_delete` | слова, которые документация SQLite перечисляет для каждой из них (`journal_mode`: `DELETE`/`TRUNCATE`/`PERSIST`/`MEMORY`/`WAL`/`OFF`), регистр не важен |
| да/нет | `foreign_keys`, `case_sensitive_like`, `recursive_triggers`, `automatic_index`, `cell_size_check`, `defer_foreign_keys`, `ignore_check_constraints`, `trusted_schema`, `reverse_unordered_selects` | `true`/`false`, `1`/`0`, `yes`/`no`, `on`/`off` |
| целое число | `journal_size_limit` (`-1`..`2**40`), `cache_size`, `busy_timeout` (`0`..`86400000` мс), `mmap_size`, `page_size` (степень двойки, `512`..`65536`), `wal_autocheckpoint`, `threads` (`0`..`64`), `max_page_count`, `cache_spill` | в пределах диапазона |

Любой другой ключ (опечатка вроде `jurnal_mode`) или недопустимое значение дают `ConfigurationError`; то
же — для значения `install_regexp_functions`, которое не является булевым. Подключение без `file_path`
тоже даёт `ConfigurationError`.

`page_size` и `auto_vacuum` задают формат файла базы, поэтому SQLite принимает их только до записи
заголовка файла: hare отправляет их первыми, раньше `journal_mode=WAL`, и они действуют на новый файл.
Существующий файл сохраняет свои значения до `VACUUM`.

`timezone` проверяется в `Hare.init()`: неизвестное имя часового пояса сразу даёт `ConfigurationError`, а
не ошибку на первом значении даты и времени.

## <a id="attached-databases"></a>Присоединённые базы

В SQLite нет схем: `Meta.schema` модели называет присоединённую базу, и каждая команда пишет таблицу
модели в ней — `"archive"."article"`. Каждое соединение присоединяет базы, которые называет настройка
`attached_databases` (`ATTACH DATABASE`), — до первой команды и снова после каждого переподключения:

```python
# credentials: имя базы и её файл
{"file_path": "main.db", "attached_databases": {"archive": "archive.db", "scratch": ":memory:"}}
```

```text
sqlite+aiosqlite://main.db?attached_databases=archive:archive.db,scratch::memory:
```

В URL каждая пара `имя:файл` делится по первому двоеточию, пары — запятыми. Имя, которое SQLite держит за
собой (`main`, `temp`), пустое имя или файл, имя, названное дважды, и значение, которое не является
такими парами, дают
`ConfigurationError`. Модель базы, которую соединение не присоединяет, получает собственную ошибку SQLite
`unknown database`; миграции базу не создают (`Features.supports_schemas` выключен).

DDL пишется так, как SQLite принимает его для присоединённой базы: индекс и триггер называются в базе
(`CREATE INDEX "archive"."ix" ON "article" (...)`), а их таблица — таблица той же базы; внешний ключ
ссылается на таблицу своей базы, поэтому связь с `db_constraint=True` на модель другой схемы отклоняется
при привязке модели (`Features.references_other_schemas` выключен) — объявите её с
`db_constraint=False`. `FullTextIndex` держит свою таблицу FTS5 и триггеры в базе модели. SpatiaLite
регистрирует и индексирует колонки геометрии только основной базы: `SpatialiteIndex` модели
присоединённой базы даёт `UnSupportedError`.

## <a id="sqlite-library-faults"></a>Ошибки библиотек

hare не обходит ошибки отдельных версий библиотеки SQLite — остаётся собственный результат библиотеки:

- SQLite с 3.38.0 по 3.41.0 строит автоматические индексы без учёта правила сравнения, которому они
  служат, поэтому сравнение по правилу сравнения (`Collate()`) по присоединённой таблице может не найти
  строк. На этих версиях выключите автоматические индексы сами: `automatic_index=False` в настройках
  подключения. Исправлено в SQLite 3.41.1.
- SQLite с 3.38.0 по 3.38.5 сообщает о нарушении внешнего ключа командой с `RETURNING` как об обычной
  ошибке, а не о нарушении ограничения, — hare выбрасывает там `OperationalError`, а не `IntegrityError`.
  Исправлено в SQLite 3.39.0.
