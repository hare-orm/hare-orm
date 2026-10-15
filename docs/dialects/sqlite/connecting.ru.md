# SQLite: подключение

Как hare открывает базу SQLite: URL и его параметры, и неисправности отдельных библиотек SQLite, которые
hare обходит.

## <a id="sqlite-connection-parameters"></a>Параметры подключения

Путь к файлу SQLite в `db_url` раскодируется так же, как имя базы PostgreSQL
(`sqlite+aiosqlite://my%20db.sqlite` → `my db.sqlite`). Каждый остальной параметр адреса или ключ
`credentials` задаёт PRAGMA соединения. Принимаются только перечисленные ниже, и каждое значение
проверяется до отправки в базу (PRAGMA не принимает значения через параметры запроса):

| Тип | PRAGMA | Значения |
|---|---|---|
| ключевое слово | `journal_mode`, `synchronous`, `temp_store`, `locking_mode`, `auto_vacuum`, `secure_delete` | слова, которые документация SQLite перечисляет для каждой из них (`journal_mode`: `DELETE`/`TRUNCATE`/`PERSIST`/`MEMORY`/`WAL`/`OFF`), регистр не важен |
| да/нет | `foreign_keys`, `case_sensitive_like`, `recursive_triggers`, `automatic_index`, `cell_size_check`, `defer_foreign_keys`, `ignore_check_constraints`, `trusted_schema`, `reverse_unordered_selects` | `true`/`false`, `1`/`0`, `yes`/`no`, `on`/`off` |
| целое число | `journal_size_limit` (`-1`..`2**40`), `cache_size`, `busy_timeout` (`0`..`86400000` мс), `mmap_size`, `page_size` (степень двойки, `512`..`65536`), `wal_autocheckpoint`, `threads` (`0`..`64`), `max_page_count`, `cache_spill` | в пределах диапазона |

Любой другой ключ (опечатка вроде `jurnal_mode`) или недопустимое значение дают `ConfigurationError`; то
же — для значения `install_regexp_functions`, которое не является булевым, и для включённого
`automatic_index` на SQLite с 3.38.0 по 3.41.0, чьи автоматические индексы не учитывают правило сравнения
(на этих версиях он по умолчанию выключен — см. [ошибки библиотек SQLite](#sqlite-library-faults)).
Подключение без `file_path` тоже даёт `ConfigurationError`.

`page_size` и `auto_vacuum` задают формат файла базы, поэтому SQLite принимает их только до записи
заголовка файла: hare отправляет их первыми, раньше `journal_mode=WAL`, и они действуют на новый файл.
Существующий файл сохраняет свои значения до `VACUUM`.

`timezone` проверяется в `Hare.init()`: неизвестное имя часового пояса сразу даёт `ConfigurationError`, а
не ошибку на первом значении даты и времени.

## <a id="sqlite-library-faults"></a>Ошибки библиотек

Две ошибки отдельных версий библиотеки SQLite hare обходит по её версии:

- SQLite с 3.38.0 по 3.41.0 строит автоматические индексы без учёта правила сравнения, которому они
  служат, поэтому сравнение десятичных значений или времени по присоединённой таблице — hare сравнивает
  их по своим правилам сравнения — не находит строк. Соединение на такой библиотеке работает с
  `PRAGMA automatic_index = OFF`, а `automatic_index=True` в его настройках даёт `ConfigurationError`.
  Исправлено в SQLite 3.41.1.
- SQLite с 3.38.0 по 3.38.5 сообщает о нарушении внешнего ключа командой с `RETURNING` как об обычной
  ошибке, а не о нарушении ограничения. hare выбрасывает его как `IntegrityError`, как и на остальных
  версиях. Исправлено в SQLite 3.39.0.
