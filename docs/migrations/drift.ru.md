# Поиск расхождений

`hare drift [app_labels...] [--connection ALIAS] [--schema SCHEMA]` сравнивает текущую схему базы с
моделями: печатает каждое расхождение — операцию, которая его устранит, неизвестную таблицу или колонку,
колонку, которую не вернёт ни одна операция, — и завершается с кодом 1, если оно есть, и с кодом 0, если
нет. Без `--connection` проверяется первое настроенное подключение, без меток приложений — каждое
приложение на нём.

```bash
hare drift --connection default
```

## <a id="from-code"></a>Вызов из кода

Команда — тонкая обёртка над публичной функцией библиотеки; вызывайте её напрямую (например, из проверки
работоспособности или в CI), а не через командную строку:

```python
from hare.migrations.drift import detect_drift_for_alias

async def detect_drift_for_alias(
    apps: Apps,
    apps_config: dict[str, dict[str, Any]],
    connection_alias: str,
    *,
    schema: str | None = None,
    app_labels: list[str] | None = None,
    journal_tables: Collection[str] = (),
) -> DriftResult: ...

result = await detect_drift_for_alias(Hare.apps, apps_config, "default")
if result.has_drift:
    for operation in result.operations:
        print(operation.describe())
    print(result.untracked_tables, result.untracked_columns, result.mismatched_columns)
```

Проверяется каждое приложение на подключении `connection_alias`: приложение из `apps_config`, у которого
`default_connection` равно `connection_alias`, а также приложение, зарегистрированное только во время
работы (например, через `Hare.register_live_models()`), модели которого привязаны к `connection_alias`.
`app_labels` сужает проверку до этих приложений. Модель с `Meta.managed = False` (в том числе
зарегистрированная во время работы) никогда не сравнивается, но её таблица считается известной и поэтому
не попадает в неизвестные; в тени есть только её таблица, для связей на неё, — без её триггеров,
представлений и других объектов (триггер `INSTEAD OF` модели над представлением на таблице не
создаётся). Модель без `Meta.schema` всегда ищется в схеме подключения по умолчанию
(`current_schema()` в PostgreSQL, то есть первая существующая схема в `search_path`) — там, где её
таблицу создаёт `migrate`; модель с `Meta.schema` ищется в своей схеме — в своей подключённой базе в
SQLite, в своей базе сервера в ClickHouse. `schema` только выбирает схему, в которой ищутся неизвестные
таблицы (при `None` — схема по умолчанию), — подключённую базу в SQLite, базу сервера в ClickHouse. Схемы и
расширения, которые нужны только моделям приложений вне проверяемых, никогда не сообщаются. `journal_tables`
называет таблицы журналов миграций помимо `hare_migrations` — `MigrationRecorder(table_name=...)` миграций,
которые приложение выполняет само ([Миграции без файлов](migrations-without-files.ru.md)): они считаются известными, а не неотслеживаемыми
(`hare drift --journal-table NAME` в командной строке). Ошибка `ConfigurationError` — для неизвестного
подключения, неизвестного элемента `app_labels` или подключения, которым не пользуется ни одно
приложение.

Смотрите `DriftResult.operations`/`.untracked_tables`/`.untracked_columns`/`.mismatched_columns` или
просто свойство `.has_drift`.

## <a id="what-is-compared"></a>Что сравнивается

Схема, которую объявляют модели, строится рядом с базой — в *тени*, тем же SQL, который пишет
`generate_schemas()`, — затем база и тень читаются одним и тем же интроспектором и сравниваются по частям.
Обе стороны печатает сама база, поэтому в схеме, только что созданной `migrate` или `generate_schemas()`,
расхождений никогда нет, какими бы словами база ни переписала объявление, а любое изменение, сделанное
вручную, видно: типа колонки, допустимости `NULL` или значения по умолчанию (и даты, и документа JSON),
выражения индекса или условия частичного индекса, `CHECK`, тела или условия триггера, запроса
представления.

### <a id="shadow"></a>Тень

Тень удаляется в конце, что бы ни случилось, — и после ошибки:

| База | Тень | Нужные права |
|---|---|---|
| PostgreSQL | схема `hare_shadow_<hex>` — и `hare_shadow_<hex>_<схема>` для каждой другой схемы моделей — на отдельном подключении, первой в пути поиска, в транзакции, которая в конце откатывается: ничего из неё не фиксируется | `CREATE` на базу (`CREATE SCHEMA`) |
| SQLite | база в памяти, открытая с настройками подключения, — и база в памяти под именем каждой базы, которую подключает соединение | никаких |
| ClickHouse | база `hare_shadow_<hex>` — и `hare_shadow_<hex>_<база>` для каждой другой базы моделей — на каждом сервере `cluster`, удаляется через `DROP DATABASE ... SYNC`; см. ниже | `CREATE DATABASE` и `DROP DATABASE` |

На ClickHouse база тени — движка `Replicated` с путём Keeper `/hare_shadow_<hex>/...`, если такова база,
которую она заменяет, и таблица `Replicated*` со своим путём Keeper получает его под тем же префиксом — ни
одна таблица тени не становится репликой настоящей.

Для базы, у диалекта которой тени нет, — `UnSupportedError`.

### <a id="parts"></a>Части и операции

Каждая часть сопоставляется по своей таблице и имени — имена объектов hare даёт сам ([явные
имена](../extending/writing-a-dialect/schema.ru.md#names)), поэтому объект, переименованный вручную, — это
удаление объекта базы и добавление объявленного, а не угаданное переименование. Часть, которая есть только
в тени, только в базе или читается на них по-разному, возвращается операцией, которая сделала бы базу
такой, как объявляют модели, — построенной из объявления модели:

| Часть | Нет в базе | Есть только в базе | Читается иначе |
|---|---|---|---|
| таблица | `CreateModel` | неизвестная таблица | — |
| колонка | `AddField` | неизвестная колонка | `AlterField` |
| внешний ключ | `AlterField` его поля | `AlterField` его поля | `AlterField` его поля |
| индекс самого поля (`db_index`, `unique`) | `AlterField` | — | `AlterField` |
| запись `Meta.indexes` | `AddIndex` | — | `RemoveIndex` + `AddIndex` |
| запись `Meta.constraints` | `AddConstraint` | — | `RemoveConstraint` + `AddConstraint` |
| индекс или ограничение, которых не объявляет ни одна модель | — | `RemoveIndex`, `RemoveConstraint` для уникального индекса или ограничения | — |
| триггер, политика, последовательность | `AddTrigger`/`AddPolicy`/`AddSequence` | `RemoveTrigger`/... | `AlterTrigger`/... |
| представление, материализованное представление, функция, словарь, объявленные моделью | `AddView`/... | — | `AlterView`/... |
| право | `AddGrant` | `RemoveGrant` | — |
| партиция | `AddPartition` | `RemovePartition` | `RemovePartition` + `AddPartition` |
| остальные параметры таблицы, комментарий таблицы | — | — | `AlterModelOptions` |
| защита строк | — | — | `AlterRowLevelSecurity` |
| тип `ENUM` | `CreateEnumType` | — | `AlterEnumType` |
| схема, расширение | `CreateSchema`, `CreateExtension` | — | — |

Комментарий колонки — часть колонки: другой даёт `AlterField`; SQLite хранит комментарии таблицы и
колонок внутри `CREATE TABLE`, откуда их читают drift и `inspectdb`.
Хранение таблицы — запись диалекта подключения в [`Meta.table_options`](../models/meta-options.ru.md#table_options);
у [партиционированной таблицы](../dialects/postgresql/indexes-and-tables.ru.md#partitioning), у которой
различаются только партиции, они добавляются и удаляются, у таблицы другой стратегии или ключа —
`AlterModelOptions`. Сами партиции никогда не считаются отдельными таблицами — ни в `drift`, ни в
`inspectdb`. Таблица, партиционированная так, как не объявляет ни одно партиционирование hare (по
выражению, с партицией, партиционированной в свою очередь), отличается от любой модели: её
`AlterModelOptions`.

Промежуточная таблица `ManyToManyField`, которой управляет hare, если её нет, или в ней нет одной из
колонок ключей или её ключа `UNIQUE`, — это `AddField` поля; если она отличается иначе — индексом ключа,
`FOREIGN KEY`, — его `AlterField`.

Два различия, которые не вернёт ни одна операция, — это `ColumnMismatch` (`app_label`, `model_name`,
`table`, `column`, `detail`) в `.mismatched_columns`: таблица, `PRIMARY KEY` которой содержит не те
колонки, что первичный ключ модели («is not in the table's PRIMARY KEY, the model's primary key holds
it»; в ClickHouse — `PRIMARY KEY (id, v)` при ключе модели `id`), и внешний ключ, объявленный через
`swappable()`, который ссылается на таблицу модели, на которую настройка указывала раньше, — его строки
называют строки той таблицы.

## <a id="detect-drift"></a>Из готового состояния: `detect_drift()`

Функция, которую она оборачивает, принимает уже построенное состояние:

```python
async def detect_drift(
    connection: DatabaseClient,
    new_state: State,
    target_labels: list[str],
    schema: str | None = None,
    unmanaged_model_states: list[ModelState] | None = None,
    journal_tables: Collection[str] = (),
) -> DriftResult: ...
```
