# Объекты схемы ClickHouse

Представления, материализованные представления, проекции и словари объявляются в модели, создаются
и меняются миграциями и сравниваются `hare drift` — как в PostgreSQL; см.
[Представления, функции, последовательности и доступ](../../models/schema-objects.ru.md). Подключение
к кластеру выполняет каждую команду схемы на всех его серверах.

## <a id="views"></a>Представления

`Meta.views` с `View(name, query)` — представление ClickHouse: таблица движка `View`, которая
переименовывается как таблица и не удаляется вместе с таблицей, которую читает. `hare drift`
сравнивает запрос представления в том виде, как его разбирает сервер, поэтому тот же запрос другими
словами — не расхождение.

## <a id="materialized-views"></a>Материализованные представления

Материализованное представление ClickHouse следует за строками, которые вставляются в таблицу его
запроса: каждый вставленный блок проходит через запрос и попадает в представление.
`ClickhouseMaterializedView` задаёт, куда идут его строки:

```python
from hare.ddl import RawSQLTerm
from hare.dialects.clickhouse.schema_objects.clickhouse_materialized_view import ClickhouseMaterializedView


class Visit(Model):
    site = fields.CharField(max_length=50)

    class Meta:
        materialized_views = [
            # Собственное хранилище, суммируемое по site при слиянии частей.
            ClickhouseMaterializedView(
                "visits_by_site",
                RawSQLTerm("SELECT site, count() AS visits FROM visit GROUP BY site"),
                engine="SummingMergeTree",
                order_by=("site",),
            ),
            # Строки пишутся в другую таблицу.
            ClickhouseMaterializedView(
                "visits_to_archive", RawSQLTerm("SELECT * FROM visit"), to="visit_archive"
            ),
            # Весь запрос заново каждый час (ClickHouse 24.10).
            ClickhouseMaterializedView(
                "top_sites",
                RawSQLTerm("SELECT site, count() AS visits FROM visit GROUP BY site ORDER BY visits DESC LIMIT 10"),
                refresh="EVERY 1 HOUR",
            ),
        ]
```

| Аргумент | Смысл |
|---|---|
| `to` | Таблица, в которую представление пишет строки, вместо собственного хранилища. |
| `engine`, `order_by`, `partition_by` | Собственное хранилище представления — по умолчанию `MergeTree`, отсортированная по `unique_columns`. |
| `refresh` | Расписание, по которому сервер выполняет весь запрос, — `"EVERY 1 HOUR"`, `"AFTER 30 MINUTE"`, с `OFFSET` и `RANDOMIZE FOR`. Представление тогда хранит строки последнего обновления. |
| `append` | С `refresh`: обновление добавляет строки, а не заменяет их. |
| `depends_on` | С `refresh`: обновляемые представления, после которых обновляется это. |
| `with_data` | Заполнить представление строками его запроса при создании. По умолчанию `True`. |

Обычное `MaterializedView` — представление с собственной `MergeTree`, отсортированной по его
`unique_columns`.

- **Заполнение.** Представление с `with_data` заполняется командой `INSERT ... SELECT` его запроса
  сразу после создания, а не через `POPULATE`, который теряет строки, вставленные во время его работы.
- **`refresh_materialized_view()`** сразу обновляет представление с `refresh` и ждёт его — строки
  заменяются разом. Любое другое представление очищается и заполняется заново — в промежутке его
  строк нет, поэтому `concurrently=True` для него даёт `UnSupportedError`.
- **Изменения.** Миграция меняет на месте запрос представления, которое пишет `to` в таблицу
  (`MODIFY QUERY`), и расписание обновляемого (`MODIFY REFRESH`); любое другое изменение удаляет
  представление и создаёт заново — собственное хранилище заполняется новым запросом, а таблица, в
  которую оно пишет, сохраняет свои строки.
- **Пересоздание таблицы** миграцией захватывает её представления: они удаляются перед копированием
  строк и создаются заново после — иначе представление приняло бы скопированные строки второй раз.

`hare drift` читает с сервера запрос, целевую таблицу, движок, сортировку, партиционирование и
расписание.

## <a id="projections"></a>Проекции

Проекция хранит строки таблицы ещё раз внутри каждой части — отсортированными или агрегированными,
как говорит её запрос; сервер читает её вместо таблицы, когда запрос ей подходит.

```python
from hare.dialects.clickhouse.schema_objects import ClickhouseProjection

class Meta:
    table_options = [
        ClickhouseTableOptions(
            projections=(ClickhouseProjection("by_site", RawSQLTerm("SELECT site, count() GROUP BY site")),)
        )
    ]
```

Запрос — `SELECT` без `FROM`, с `GROUP BY` или `ORDER BY`. Миграция добавляет проекцию через
`ADD PROJECTION` и строит её по хранимым строкам через `MATERIALIZE PROJECTION`, которая заканчивается
до продолжения миграции; удаляет — через `DROP PROJECTION`.

В ClickHouse 24.8 и новее (`Features.rebuilds_projections`) таблица с проекциями получает настройки,
которые перестраивают их при удалении строк и при слиянии, — тогда лёгкий `DELETE` и слияние
`ReplacingMergeTree` сохраняют проекции верными. На более старом сервере строки такой таблицы
удаляются мутацией `ALTER TABLE ... DELETE`.

## <a id="dictionaries"></a>Словари

Словарь держит строки таблицы в памяти сервера; значение из них читается по ключу через `DictGet`
вместо `JOIN`.
`ClickhouseDictionary` — `hare.ddl.Dictionary` для ClickHouse: от этого базового класса происходит
словарь любого диалекта.

```python
from hare.dialects.clickhouse.functions import DictGet
from hare.dialects.clickhouse.schema_objects import ClickhouseDictionary


class Country(Model):
    code = fields.CharField(max_length=2, primary_key=True)
    name = fields.CharField(max_length=50)

    class Meta:
        dictionaries = [ClickhouseDictionary("country_names", key=("code",), attributes=("name",), lifetime=300)]


await Visit.objects.annotate(
    country=DictGet("country_names", "name", "country_code", output_field=fields.CharField(max_length=50))
)
await Country.objects.reload_dictionary("country_names")
```

| Аргумент | Смысл |
|---|---|
| `key` | Поля, по которым ищется строка. |
| `attributes` | Поля, которые даёт поиск. |
| `layout` | Как сервер хранит строки, с аргументами, — `"COMPLEX_KEY_HASHED()"` (по умолчанию) принимает ключ из любых полей; `"HASHED()"`, `"FLAT()"` — ключ из одного беззнакового 64-битного целого. |
| `lifetime` | Через сколько секунд словарь загружается заново — число или `(наименьшее, наибольшее)`, между которыми сервер выбирает момент; `0` (по умолчанию) — никогда. |
| `source` | `RawSQLTerm` другого источника вместо таблицы модели — в том виде, как его принимает `SOURCE(...)`. |

Словарь загружается из таблицы модели под пользователем и паролем подключения, поэтому строка,
записанная в таблицу, читается через словарь после его следующей загрузки — `reload_dictionary()`
загружает его сразу. `DictGet(..., default=...)` даёт значение для ключа, которого в словаре нет
(`dictGetOrDefault`); без него сервер даёт значение типа атрибута по умолчанию. Ключ из нескольких
полей ищется последовательностью из них.

Пароль никогда не попадает в файл миграции: пароль подключения подставляется в команду при создании
словаря (`[HIDDEN]` в `hare sqlmigrate`), а `{env:NAME}` в `source` тогда же заменяется переменной
окружения `NAME`. Операции миграции — `AddDictionary`, `AlterDictionary`, `RemoveDictionary` и
`RenameDictionary`, их пишет `makemigrations`; словарь не удаляется вместе с таблицей, которую читает.

## <a id="cluster"></a>Кластер

Подключение с `cluster=<имя>` входит в кластер серверов:

- Каждая команда схемы — `CREATE`, `ALTER`, `DROP`, `TRUNCATE` таблиц, представлений и словарей —
  выполняется `ON CLUSTER <имя>`, на каждом сервере. База движка `Replicated` реплицирует свою схему
  сама: подключение замечает это при открытии и не пишет `ON CLUSTER`.
- Журнал применённых миграций — `ReplicatedMergeTree`, поэтому все серверы видят одни и те же
  миграции.
- Таблица, которую миграция пересоздаёт на кластере, должна быть реплицируемой или распределённой:
  таблица, которая хранит на каждом сервере свои строки, отклоняется — скопировались бы строки только
  одного сервера.

`ClickhouseTableOptions(distributed_over=...)` распределяет строки модели по серверам:

```python
class Hit(Model):
    class Meta:
        table_options = [
            ClickhouseTableOptions(
                engine="ReplicatedMergeTree",
                distributed_over="hit_local",
                sharding_key=RawSQLTerm("cityHash64(id)"),
            )
        ]
```

Миграция создаёт на каждом сервере локальную таблицу `hit_local` с движком и опциями, а таблицу
модели `hit` — как `Distributed` поверх неё. Строки читаются и пишутся через `hit`, каждая — в шард,
который выбирает её `sharding_key` (без него — в любой). Мутация, лёгкий `DELETE` и изменение
хранения идут в `hit_local` на каждом сервере, изменение колонок — в обе таблицы. Пересоздаваемая
таблица копируется через распределённую таблицу поверх новых локальных, каждая строка — в свой шард.
`mutations_sync=2` ждёт все реплики.
