# Модели ClickHouse

Таблица ClickHouse отсортирована по ключу, а не проиндексирована им: ничто в сервере не держит ключ
уникальным, а связь — указывающей на существующую строку. hare берёт на себя то, что объявляет
модель, — ключи, уникальность, связи, — а `ClickhouseTableOptions` задаёт, как сервер хранит строки.

```python
import uuid

from hare import fields
from hare.ddl import RawSQLTerm
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.models import Model


class PageView(Model):
    id = fields.UUIDField(primary_key=True, default=uuid.uuid4)
    site = fields.CharField(max_length=50)
    viewed_at = fields.DatetimeField()

    class Meta:
        table_options = [
            ClickhouseTableOptions(
                order_by=("id", "site", "viewed_at"),
                partition_by=RawSQLTerm("toYYYYMM(viewed_at)"),
                ttl=RawSQLTerm("toDateTime(viewed_at) + INTERVAL 1 YEAR"),
                settings=(("index_granularity", 8192),),
            )
        ]
```

## <a id="keys"></a>Ключи

Ключ, который генерирует база, — `IntField(primary_key=True)`, `BigIntField(primary_key=True)` —
берётся из серии чисел, которую хранит ClickHouse Keeper (`generateSerialID`, ClickHouse 25.1). До
записи строк hare одним запросом берёт столько чисел, сколько строк без ключа, и проставляет их;
каждое число выдаётся один раз — для всех процессов, которые пишут модель.

- Серия называется по модели (`<app_label>.<Model>`), а не по таблице: переименование таблицы её не
  меняет.
- Строки, записанные со своими ключами (импорт, копия из другой базы), оставляют серию позади. Первая
  запись модели в процессе сдвигает серию за наибольший ключ её таблицы; операция миграции
  `SynchronizeKeySeries("Model")` делает это сразу.
- Ключ за пределами диапазона поля даёт `ValidationError` до записи.
- На сервере старше 25.1 или без Keeper такая модель отклоняется при привязке — используйте
  `UUIDField(primary_key=True, default=uuid.uuid4)` (или `default=uuid.uuid7` в Python 3.14) либо
  задавайте ключ каждой строке.

## <a id="uniqueness-and-relations"></a>Уникальность и связи

ClickHouse не держит ни ограничений уникальности, ни внешних ключей
(`Features.checks_constraints_before_write`), поэтому перед `create()`, `save()`, `bulk_create()`,
`update()` и `bulk_update()` hare проверяет то, что объявляет модель:

| Объявлено | Проверяется |
|---|---|
| Первичный ключ, `unique=True`, `UniqueConstraint`, `Index(unique=True)` | Ни у хранимой, ни у другой записываемой строки нет тех же значений. `UniqueConstraint(condition=Q(...))` охватывает строки, для которых выполняется его условие. |
| Связь (`ForeignKeyField`, `OneToOneField`) с `db_constraint=True` — по умолчанию | Строка, на которую она указывает, существует. |

Нарушение даёт `IntegrityError` — как та же запись в PostgreSQL и SQLite, — и ничего не
записывается. Значение `None` не проверяется, связь с `db_constraint=False` тоже. Пачка делает один
`SELECT` на каждую объявленную уникальность и каждую связь, передавая ключи внешней таблицей;
строки пачки с повторяющимся ключом находятся без запроса. `update()` проверяет только ту
уникальность, поля которой он меняет.

- **Таблица, хранящая версии строки** — `ReplacingMergeTree`, `CollapsingMergeTree` и другие движки,
  которые читает `final()`, — повторяет ключ по замыслу: её ключ не проверяется.
- **Проверка и запись — две команды.** Другой клиент может записать строку между ними, и останутся две
  строки с одним ключом: проверка рассчитана на одно приложение, которое пишет через hare, а не
  является блокировкой.

`on_delete` выполняет сам hare — как для связи с `db_constraint=False` в любой базе.

## <a id="table-options"></a>Опции таблицы

`ClickhouseTableOptions` в `Meta.table_options`:

| Опция | Смысл |
|---|---|
| `engine` | Движок таблицы с аргументами — `"MergeTree"`, `"ReplacingMergeTree(version)"`. По умолчанию `"MergeTree"`. Опции семейства `MergeTree` ниже с другим движком отклоняются. |
| `order_by` | По чему отсортированы строки — имена полей и выражения `RawSQLTerm`, первичный ключ первым. Пусто: первичный ключ или `tuple()` для модели без него. |
| `sample_by` | Выражение, по доле которого читает [`sample()`](query-modifiers.ru.md#sample), — имя поля или `RawSQLTerm`, одно из `order_by`. `PRIMARY KEY` таблицы тогда идёт от ключа модели до него. |
| `partition_by` | `RawSQLTerm` выражения, по которому строки делятся на партиции. |
| `ttl` | `RawSQLTerm` выражения `TTL` таблицы. |
| `settings` | `SETTINGS` таблицы — пары `(имя, значение)`, значение — строка или целое. |
| `column_codecs` | Сжатие колонок — пары `(имя поля, кодеки)`: `("payload", "ZSTD(3)")`, `("created", "Delta, ZSTD")`. |
| `column_ttls` | Сколько колонки хранят значения — пары `(имя поля, RawSQLTerm)`, момент, когда значение сбрасывается к умолчанию. Не для колонки ключа. |
| `projections` | Проекции таблицы — см. [Объекты схемы](schema-objects.ru.md#projections). |
| `distributed_over` | Локальная таблица, которая хранит строки на каждом сервере кластера подключения, — таблица модели тогда `Distributed` поверх неё. См. [Кластер](schema-objects.ru.md#cluster). |
| `sharding_key` | С `distributed_over`: `RawSQLTerm` выражения, по которому выбирается шард строки; без него — любой шард. |
| `lightweight_updates` | Строки меняются лёгким `UPDATE` — см. [ниже](#lightweight-updates). |

Модель без этих опций — `MergeTree`, отсортированная по первичному ключу. Первичный ключ — это
выражение `PRIMARY KEY` движка, которое пишется после `ORDER BY`.

Миграция меняет то, что сервер меняет на месте, — `ttl`, `settings` (кроме `index_granularity` и
`index_granularity_bytes`, которые задаются при создании таблицы), `column_codecs`, `column_ttls`,
`projections`, `lightweight_updates` — через `ALTER TABLE`. Любое другое изменение (движок,
`order_by`, `sample_by`, `partition_by`, распределение) пересоздаёт таблицу: создаётся новая таблица,
строки копируются, старая удаляется, а новая переименовывается; представления над ней удаляются и
создаются заново вокруг этого. На кластере таблица, которая хранит на каждом сервере свои строки,
при этом отклоняется — скопировались бы строки только одного сервера. `hare drift` сравнивает опции с
серверными в том виде, как их форматирует сервер.

## <a id="lightweight-updates"></a>Лёгкие изменения

`ClickhouseTableOptions(lightweight_updates=True)` (ClickHouse 25.7) ставит настройки таблицы
`enable_block_number_column` и `enable_block_offset_column`; `save()`, `update()` и `bulk_update()`
такой таблицы пишут `UPDATE t SET ... WHERE ...`. Новые значения записываются рядом со строкой и
сразу читаются вместо старых — мутация же переписывает каждую часть, где есть такая строка. Колонка
ключа не меняется ни так, ни так. Миграция включает и выключает опцию через
`MODIFY SETTING`/`RESET SETTING`, не пересоздавая таблицу.

## <a id="data-skipping-indexes"></a>Индексы пропуска данных

Индекс ClickHouse пропускает гранулы (по умолчанию 8192 строки), в которых не может быть строки,
которую ищет запрос. `Index(fields=...)` — индекс `minmax` с гранулярностью 1; в
`hare.dialects.clickhouse.indexes` есть все типы, у каждого — `granularity`:

| Индекс | Тип | Пропускает гранулы |
|---|---|---|
| `MinMaxIndex` | `minmax` | наименьшее и наибольшее значения которых не подходят сравнению или диапазону |
| `SetIndex(max_rows=0)` | `set(max_rows)` | без единого значения, которое ищет фильтр |
| `BloomFilterIndex(false_positive=0.025)` | `bloom_filter(...)` | где точно нет значений равенства, `__in` или `__contains` массива |
| `TokenBloomFilterIndex(filter_size=256, hash_functions=2, seed=0)` | `tokenbf_v1(...)` | без слова, которое ищет равенство или поиск целого слова |
| `NgramBloomFilterIndex(ngram_size=3, ...)` | `ngrambf_v1(...)` | без n-грамм текста `__contains`, `__startswith` или равенства — текст короче n-граммы не пропускает ничего |

```python
from hare.dialects.clickhouse.indexes import BloomFilterIndex

class Meta:
    indexes = [BloomFilterIndex(fields=["user_id"], name="user_bloom", granularity=4)]
```

Индекс добавляется и удаляется через `ALTER TABLE ... ADD INDEX`/`DROP INDEX`; `hare inspectdb` и
`hare drift` читают тип и его аргументы обратно.

Каждый тип индекса — `ClickhouseIndex` (`hare.dialects.clickhouse.indexes`) с именем и аргументами
своего типа; он создаётся и удаляется только в базе ClickHouse.

## <a id="generated-columns"></a>Вычисляемые колонки

`GeneratedField` — колонка `MATERIALIZED` при `stored=True` (вычисляется при записи строки) и колонка
`ALIAS` при `stored=False` (вычисляется при чтении). Вставка не пишет ни ту, ни другую, а колонка
`ALIAS` не может входить в `order_by`.

## <a id="column-types"></a>Типы колонок

У каждого поля hare есть тип ClickHouse, а `hare.dialects.clickhouse.fields` добавляет собственные
типы ClickHouse — см. [Типы](types.ru.md).
