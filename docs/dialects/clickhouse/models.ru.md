# ClickHouse: модели

Таблица ClickHouse отсортирована по ключу, а не проиндексирована им: ничто в сервере не держит ключ
уникальным, а связь — указывающей на существующую строку. hare берёт на себя то, что объявляет модель, —
ключи, уникальность, связи, — а `ClickhouseTableOptions` задаёт, как сервер хранит строки.

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

Ключ, который генерирует база, — `IntField(primary_key=True)`, `BigIntField(primary_key=True)` — берётся
из серии чисел, которую хранит ClickHouse Keeper (`generateSerialID`, ClickHouse 25.1). До записи строк
hare одним запросом берёт столько чисел, сколько строк без ключа, и проставляет их; каждое число выдаётся
один раз — для всех процессов, которые пишут модель.

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
| Первичный ключ, `unique=True`, `UniqueConstraint`, `Index(unique=True)` | Ни у хранимой, ни у другой записываемой строки нет тех же значений; массив сравнивается по элементам. `UniqueConstraint(condition=Q(...))` охватывает строки, для которых выполняется его условие. |
| Связь (`ForeignKeyField`, `OneToOneField`) с `db_constraint=True` — по умолчанию | Строка, на которую она указывает, существует. |

Нарушение даёт `IntegrityError` — как та же запись в PostgreSQL и SQLite, — и ничего не записывается.
Поле первичного ключа, оставленное своему `db_default` (`DateField(db_default=Now())` в
`CompositePrimaryKey`), даёт `UnSupportedError` до записи: сервер не возвращает строк записи, и ключ
нельзя ни проверить на уникальность, ни прочитать обратно — задайте ему значение или `default=`,
вычисляющий его в Python. Значение `None` не проверяется, связь с `db_constraint=False` тоже. Пачка
делает один `SELECT` на каждую объявленную уникальность и каждую связь, передавая ключи внешней таблицей;
строки пачки с повторяющимся ключом находятся без запроса. `update()` проверяет только ту уникальность,
поля которой он меняет. hare проверяет уникальность полей — без условия или с условием `Q`: модель,
объявившая `Index(<expression>, unique=True)` или `UniqueConstraint`/уникальный `PartialIndex` с условием
`RawSQLTerm`, даёт `ConfigurationError` при привязке к соединению ClickHouse.

- **Таблица, хранящая версии строки** — `ReplacingMergeTree`, `CollapsingMergeTree` и другие движки,
  которые читает `final()`, — повторяет ключ по замыслу: её ключ не проверяется.
- **Проверка и запись — две команды.** Внутри одного процесса запись держит проверяемые значения, пока не
  выполнятся её команды. Одновременные вызовы `create()`, `save()`, `bulk_create()`, `update()` и
  `bulk_update()` с одним значением (задачи одного цикла событий) выполняются по очереди, и все, кроме
  первого, бросают `IntegrityError`. Другой процесс может записать строку между проверкой и записью, и
  останутся две строки с одним ключом: проверка рассчитана на один процесс, который пишет через hare, а
  не является блокировкой на сервере. Внутри транзакции значение держится до конца записи, а не до
  фиксации, и другое соединение не видит строку до фиксации.
- **Реплицируемая таблица проверяется на реплике, до которой дошёл запрос.** Строку, записанную через
  другую реплику и ещё не реплицированную на эту, проверка не видит — таблица `Distributed` поверх реплик
  читает одну реплику каждого шарда. Пишите строки одного ключа через один сервер или дождитесь реплик
  (`insert_quorum`, `SYSTEM SYNC REPLICA`) перед повторной записью.

`on_delete` выполняет сам hare — как для связи с `db_constraint=False` в любой базе.

## <a id="table-options"></a>Опции таблицы

`ClickhouseTableOptions` в `Meta.table_options`:

| Опция | Что задаёт |
|---|---|
| `engine` | Движок таблицы с аргументами — `"MergeTree"`, `"ReplacingMergeTree(version)"`. По умолчанию `"MergeTree"`. Опции семейства `MergeTree` ниже с другим движком отклоняются. |
| `order_by` | По чему отсортированы строки — имена полей и выражения `RawSQLTerm`, первичный ключ первым; внешний ключ или связь один-к-одному по имени сортирует по своим колонкам ключа (`"team"` — это `team_id`). Пусто: первичный ключ или `tuple()` для модели без него. |
| `sample_by` | Выражение, по доле которого читает [`sample()`](query-modifiers.ru.md#sample), — имя поля или `RawSQLTerm`, одно из `order_by`. `PRIMARY KEY` таблицы тогда идёт от ключа модели до него. |
| `partition_by` | `RawSQLTerm` выражения, по которому строки делятся на партиции. |
| `ttl` | `RawSQLTerm` выражения `TTL` таблицы — см. [`ttl`](#ttl) |
| `settings` | `SETTINGS` таблицы — пары `(name, value)`, значение — строка или целое. |
| `column_codecs` | Сжатие колонок — пары `(field name, codecs)`: `("payload", "ZSTD(3)")`, `("created", "Delta, ZSTD")`. |
| `column_ttls` | Сколько колонки хранят значения — пары `(field name, RawSQLTerm)`, момент, когда значение сбрасывается к умолчанию. Не для колонки ключа. |
| `projections` | Проекции таблицы — см. [Объекты схемы](schema-objects.ru.md#projections). |
| `distributed_over` | Локальная таблица, которая хранит строки на каждом сервере кластера подключения, — таблица модели тогда `Distributed` поверх неё. См. [Кластер](schema-objects.ru.md#cluster). |
| `sharding_key` | С `distributed_over`: `RawSQLTerm` выражения, по которому выбирается шард строки; без него — любой шард. |
| `lightweight_updates` | Строки меняются лёгким `UPDATE` — см. [ниже](#lightweight-updates). |

### <a id="ttl"></a>`ttl`

`RawSQLTerm` выражения `TTL` таблицы. ClickHouse 24.3 хранит TTL только по `Date` или `DateTime`:
`DateField` (`Date32`) или `DatetimeField` (`DateTime64`) в нём приводится через `toDateTime(seen)`,
иначе его DDL даёт `UnSupportedError`; так же и TTL колонки. TTL по полю, допускающему `NULL`
(`null=True`), на любой версии даёт `UnSupportedError` — момент даёт
`assumeNotNull(seen) + INTERVAL 30 DAY` или `ifNull(seen, now()) + ...`.

Модель без этих опций — `MergeTree`, отсортированная по первичному ключу. Первичный ключ — это выражение
`PRIMARY KEY` движка, которое пишется после `ORDER BY`.

Миграция меняет то, что сервер меняет на месте, — `ttl`, `settings` (кроме `index_granularity` и
`index_granularity_bytes`, которые задаются при создании таблицы), `column_codecs`, `column_ttls`,
`projections`, `lightweight_updates` — через `ALTER TABLE`. Любое другое изменение (движок, `order_by`,
`sample_by`, `partition_by`, распределение) пересоздаёт таблицу: создаётся новая таблица, строки
копируются, старая удаляется, а новая переименовывается; представления и словари над ней удаляются и
создаются заново вокруг этого, а её колонки сохраняют кодеки и время жизни. На кластере таблица, которая
хранит на каждом сервере свои строки, при этом отклоняется — скопировались бы строки только одного
сервера. Колонку ключей таблицы (её первичного ключа, `order_by`, `sample_by`, `partition_by`, ключа
распределения), колонку, которую называет проекция (или любую колонку при проекции `SELECT *`), колонку,
которую читает словарь, и любую колонку таблицы с материализованным представлением нельзя переименовать
на месте: `RenameField` её поля так же пересоздаёт таблицу — поле переименовывается вместе с ней и в
словарях, и в запросах и ключах материализованных представлений — с колонкой под новым именем, как и
`AlterField`, меняющий его тип или допустимость `NULL`, — так же пересоздаётся таблица связи, которая
хранит значения ключа в колонке своих ключей, когда ключ получает новый тип; колонка, которую читает
индекс пропуска данных, получает новый тип или допустимость `NULL`, а индекс на время изменения удаляется
и создаётся снова — и общий индекс по всем колонкам ключа связи с составным ключом тоже. `hare drift`
сравнивает опции с серверными в том виде, как их форматирует сервер.

Когда миграция меняет и поля, изменённые `ttl`, кодек колонки, время жизни колонки или проекция —
выражения над колонками — снимаются до изменения полей и задаются после него: поле, ставшее NOT NULL,
получает новый `ttl` после своего изменения (ClickHouse не принимает `ttl` над колонкой `Nullable`), а
поле, ставшее допускающим `NULL`, теряет `ttl` до него. `makemigrations` записывает это как
`AlterModelOptions` по обе стороны от операций полей. Поле, которое становится NOT NULL, когда в какой-то
строке в нём NULL, даёт `IntegrityError` до изменения колонки, как в PostgreSQL: `MODIFY COLUMN` упал бы
в своей мутации, а она остаётся и роняет каждую следующую; сначала заполните NULL. Колонка поля, которое
начинает допускать `NULL`, при изменении переписывается (`UPDATE column = column`): ClickHouse 25.8
читает карту NULL у частей, сохранённых раньше, как NULL во всех значениях, и
`filter(title__isnull=True)` нашёл бы их все.

## <a id="lightweight-updates"></a>Лёгкие изменения

`ClickhouseTableOptions(lightweight_updates=True)` (ClickHouse 25.7) ставит настройки таблицы
`enable_block_number_column` и `enable_block_offset_column`; `save()`, `update()` и `bulk_update()` такой
таблицы пишут `UPDATE t SET ... WHERE ...`. Новые значения записываются рядом со строкой и сразу читаются
вместо старых — мутация же переписывает каждую часть, где есть такая строка. Колонка ключа не меняется ни
так, ни так. Миграция включает и выключает опцию через `MODIFY SETTING`/`RESET SETTING`, не пересоздавая
таблицу.

ClickHouse не меняет колонку, которую читают его ключи (`order_by`, `sample_by`, `partition_by`, ключ
распределения) или аргументы движка (версия, знак). `save()` оставляет такую колонку как есть: сначала
читает ключевые колонки строки и бросает `UnSupportedError`, если экземпляр изменил одну из них;
`update()`, `bulk_update()` и `save(update_fields=...)`, называющие такую колонку, бросают
`UnSupportedError`. Ключ сортировки начинается с первичного ключа, поэтому часть составного первичного
ключа — тоже такая колонка: `update(number=...)` по ней даёт `UnSupportedError`. Новый ключ строка
получает, если её удалить и создать снова.

## <a id="data-skipping-indexes"></a>Индексы пропуска данных

Индекс ClickHouse пропускает гранулы (по умолчанию 8192 строки), в которых не может быть строки, которую
ищет запрос. `Index(fields=...)` — индекс `minmax` с гранулярностью 1; в
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
`hare drift` читают тип и его аргументы обратно, а ключ-выражение
(`BloomFilterIndex(Lower("title"), name=...)`) — как его SQL. `MinMaxIndex` с гранулярностью 1 — тот же
индекс, что обычный `Index`. Направления у ключа нет: ключи `"-field"`, `.asc()` и `.desc()` дают
`UnSupportedError` при создании индекса. Условия у индекса нет: `PartialIndex` создаётся как индекс по
всем строкам.

Каждый тип индекса — `ClickhouseIndex` (`hare.dialects.clickhouse.indexes`) с именем и аргументами своего
типа; он создаётся и удаляется только в базе ClickHouse.

## <a id="generated-columns"></a>Вычисляемые колонки

`GeneratedField` — колонка `MATERIALIZED` при `stored=True` (вычисляется при записи строки) и колонка
`ALIAS` при `stored=False` (вычисляется при чтении). Вставка не пишет ни ту, ни другую, а колонка `ALIAS`
не может входить в `order_by`. Его `output_field` может быть типом ClickHouse
(`GeneratedField(RawSQLTerm("cityHash64(name)"), UInt64Field())`, как его пишет `inspectdb`) — миграции и
`hare drift` сравнивают его как поле этого типа.

## <a id="column-types"></a>Типы колонок

У каждого поля hare есть тип ClickHouse, а `hare.dialects.clickhouse.fields` добавляет собственные типы
ClickHouse — см. [Типы](types.ru.md).
