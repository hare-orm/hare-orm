# ClickHouse: модели

Таблица ClickHouse отсортирована по ключу, а не проиндексирована им: ничто в сервере не держит ключ
уникальным, а связь — указывающей на существующую строку. Ключ, который модель объявляет порождаемым, даёт
`generateSerialID()` самого сервера (см. [Ключи](#keys)), а `on_delete` связей hare выполняет сам; уникальность или
связь с `db_constraint=True` отклоняется при привязке модели (см. [Уникальность и связи](#uniqueness-and-relations)).
`ClickhouseTableOptions` задаёт, как сервер хранит строки.

```python
import uuid

from datetime import timedelta

from hare import fields
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.models import Model
from hare.query.expressions import F
from hare.query.functions import Trunc


class PageView(Model):
    id = fields.UUIDField(primary_key=True, default=uuid.uuid4)
    site = fields.CharField(max_length=50)
    viewed_at = fields.DatetimeField()

    class Meta:
        table_options = [
            ClickhouseTableOptions(
                order_by=("id", "site", "viewed_at"),
                partition_by=Trunc("viewed_at", "month"),
                ttl=F("viewed_at") + timedelta(days=365),
                settings=(("index_granularity", 8192),),
            )
        ]
```

## <a id="keys"></a>Ключи

Ключ, который генерирует база, — `IntField(primary_key=True)`, `BigIntField(primary_key=True)` — берётся
из серии чисел, которую хранит ClickHouse Keeper (`generateSerialID`). До записи строк
hare одним запросом берёт столько чисел, сколько строк без ключа, и проставляет их; каждое число выдаётся
один раз — для всех процессов, которые пишут модель.

- Серия называется по базе и модели (`<database>.<app_label>.<Model>`), а не по таблице:
  переименование таблицы её не меняет, а у каждой базы сервера своя серия.
- Сервер держит не больше `max_autoincrement_series` серий (по умолчанию 1000), а удаление базы оставляет
  её серии в Keeper. Если `keeper_hosts` называет Keeper сервера, удаление базы (`db_delete()` — тестовая
  база в конце своих тестов) удаляет и её серии.
- Строки, записанные со своими ключами (импорт, копия из другой базы), оставляют серию позади. Первая
  запись модели в процессе сдвигает серию за наибольший ключ её таблицы; операция миграции
  `SynchronizeKeySeries("Model")` делает это сразу.
- Ключ за пределами диапазона поля даёт `ValidationError` до записи.
- На сервере без Keeper такая модель отклоняется при привязке — используйте
  `UUIDField(primary_key=True, default=uuid.uuid4)` (или `default=uuid.uuid7` в Python 3.14) либо
  задавайте ключ каждой строке.

## <a id="uniqueness-and-relations"></a>Уникальность и связи

ClickHouse не хранит ни ограничений уникальности, ни внешних ключей (`Features.supports_unique_constraints`
и `Features.supports_foreign_keys` выключены), поэтому модель, привязанная к подключению ClickHouse, не
объявляет ни того, ни другого, а модель, которая объявляет, при привязке даёт `ConfigurationError`:

| Объявлено | При привязке модели |
|---|---|
| `unique=True` у поля, кроме первичного ключа, `UniqueConstraint`, `Index(unique=True)`, уникальный `PartialIndex` | `ConfigurationError` — ClickHouse ничего не хранит уникальным |
| Связь (`ForeignKeyField`, `ManyToManyField` с автоматической промежуточной таблицей) с `db_constraint=True`, по умолчанию | `ConfigurationError` — объявите её с `db_constraint=False` |
| `OneToOneField` с любым `db_constraint` — её ключ уникален | `ConfigurationError` — ClickHouse ничего не хранит уникальным; объявите `ForeignKeyField` с `db_constraint=False` |

Миграция, объявляющая уникальность, — `CreateModel` или `AddField` поля с `unique=True`, `AlterField`,
делающий поле уникальным, `AddIndex`/`AddConstraint` уникального объекта — отклоняется так же:
`UnSupportedError` до её первой команды.

```python
class Order(Model):
    id = fields.BigIntField(primary_key=True)
    customer = fields.ForeignKeyField("models.Customer", db_constraint=False)
```

Что из этого следует:

- **Первичный ключ не уникален.** Ключ `ORDER BY` ClickHouse сортирует строки; `create()` с уже
  хранимым ключом пишет вторую строку с ним. Таблица, хранящая версии строки, — `ReplacingMergeTree`,
  `CollapsingMergeTree` и другие движки, которые читает `final()`, — сливает их по своему устройству.
- **`get_or_create()` и `update_or_create()`** читают строку, затем пишут её: два одновременных вызова
  могут создать её оба, как в любой базе без уникального индекса.
- **`in_bulk(field_name=...)`** принимает только первичный ключ (`"pk"`, по умолчанию); другое поле даёт
  `QueryError`, потому что уникальных полей нет.
- **`add()` связи многие-ко-многим** читает уже связанные пары перед вставкой; два
  одновременных `add()` одной пары могут вставить её оба.
- **`on_delete`** выполняет сам hare, как для связи с `db_constraint=False` в любой базе.

## <a id="table-options"></a>Опции таблицы

`ClickhouseTableOptions` в `Meta.table_options`:

| Опция | Что задаёт |
|---|---|
| `engine` | Движок таблицы с аргументами — `"MergeTree"`, `"ReplacingMergeTree(version)"`. По умолчанию `"MergeTree"`. Опции семейства `MergeTree` ниже с другим движком отклоняются. |
| `order_by` | По чему отсортированы строки — имена полей и [выражения](#expressions), первичный ключ первым; внешний ключ или связь один-к-одному по имени сортирует по своим колонкам ключа (`"team"` — это `team_id`). Пусто: первичный ключ или `tuple()` для модели без него. Поле с `null=True` в нём или в `partition_by` требует `settings=(("allow_nullable_key", 1),)` — иначе `ConfigurationError`. |
| `sample_by` | Выражение, по доле которого читает [`sample()`](query-modifiers.ru.md#sample), — одно из `order_by`. `PRIMARY KEY` таблицы тогда идёт от ключа модели до него. |
| `partition_by` | Выражение, по которому строки делятся на партиции, — `Trunc("viewed_at", "month")`. |
| `ttl` | `TTL` таблицы — см. [`ttl`](#ttl) |
| `settings` | `SETTINGS` таблицы — пары `(name, value)`, значение — строка или целое. |
| `column_codecs` | Сжатие колонок — пары `(field name, codecs)`: `("payload", "ZSTD(3)")`, `("created", "Delta, ZSTD")`. |
| `column_ttls` | Сколько колонки хранят значения — пары `(field name, expression)`, момент, когда значение сбрасывается к умолчанию. Не для колонки ключа. |
| `projections` | Проекции таблицы — см. [Объекты схемы](schema-objects.ru.md#projections). |
| `distributed_over` | Локальная таблица, которая хранит строки на каждом сервере кластера подключения, — таблица модели тогда `Distributed` поверх неё. См. [Кластер](schema-objects.ru.md#cluster). |
| `sharding_key` | С `distributed_over`: выражение, по которому выбирается шард строки; без него — любой шард. Ключ, читающий поле с `null=True`, даёт `ConfigurationError` — ClickHouse распределяет строки по целому числу без NULL. |
| `lightweight_updates` | Строки меняются лёгким `UPDATE` — см. [ниже](#lightweight-updates). |

### <a id="expressions"></a>Выражения

Ключ, время жизни и ключ распределения — это имя поля, выражение hare над полями модели — `F`, функции,
арифметика: `Trunc("viewed_at", "month")`, `F("viewed_at") + timedelta(days=30)` — или `RawSQLTerm` с
готовым SQL. Выражение hare пишется SQL ClickHouse, миграция, переименовывающая поле, которое оно читает,
переименовывает его и здесь, а hare знает колонки, которые оно читает, — колонки, которые он не обновляет, и
те, что миграция меняет пересозданием таблицы (ниже). Готовый SQL пишется как есть: hare не переименовывает в
нём поле и не читает его колонки, поэтому обновление или изменение колонки, которую читает только готовый
ключ, отклоняет сервер.

### <a id="ttl"></a>`ttl`

`TTL` таблицы: выражение момента, когда строка удаляется, или `RawSQLTerm` целого выражения с другим
действием (`RawSQLTerm("viewed_at + INTERVAL 1 MONTH TO VOLUME 'cold'")`). TTL по полю, допускающему `NULL`
(`null=True`), даёт `UnSupportedError` — момент даёт `RawSQLTerm("assumeNotNull(seen) + INTERVAL 30 DAY")`;
так же и TTL колонки.

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
распределения — имени поля или выражения hare), колонку, которую читает проекция (колонки её набора
запроса, `read_columns` сырой проекции), колонку, которую читает словарь, и любую колонку таблицы с
материализованным представлением нельзя переименовать на месте: `RenameField` её поля так же пересоздаёт
таблицу — поле переименовывается вместе с ней в атрибутах словарей и в ключах материализованных представлений,
их готовые запросы остаются как написаны, — с колонкой под новым именем, как и
`AlterField`, меняющий его тип или допустимость `NULL`, — так же пересоздаётся таблица связи, которая
хранит значения ключа в колонке своих ключей, когда ключ получает новый тип; колонка, которую читает
индекс пропуска данных, получает новый тип или допустимость `NULL`, а индекс на время изменения удаляется
и создаётся снова — и общий индекс по всем колонкам ключа связи с составным ключом тоже. `hare drift`
сравнивает опции с серверными в том виде, как их форматирует сервер.

Когда миграция меняет и поля, изменённые `ttl`, кодек колонки, время жизни колонки или проекция —
выражения над колонками — снимаются до изменения полей и задаются после него: поле, ставшее NOT NULL,
получает новый `ttl` после своего изменения (ClickHouse не принимает `ttl` над колонкой `Nullable`), а
поле, ставшее допускающим `NULL`, теряет `ttl` до него. Изменённый ключ (`order_by`, `partition_by`,
`sample_by`, ключ шардирования) или движок держит старое значение, пока поля добавляются, и получает
новое после них; удаляемые поля удаляются последними. `makemigrations` записывает это как
`AlterModelOptions` по обе стороны от операций полей. Поле, которое становится NOT NULL, когда в какой-то
строке в нём NULL, даёт `IntegrityError` до изменения колонки, как в PostgreSQL: `MODIFY COLUMN` упал бы
в своей мутации, а она остаётся и роняет каждую следующую; сначала заполните NULL. Поле, которое начинает
допускать `NULL`, на ClickHouse до 26.3 — см. [Ошибки версии сервера](differences.ru.md#server-faults).
Перед сменой типа колонки строки, которые лёгкий `DELETE`
только пометил, удаляются (`APPLY DELETED MASK`): `MODIFY COLUMN` преобразует и их, и такая строка со
значением, которое новый тип не вмещает, уронила бы его, а проверки оставшихся строк её не видят.

## <a id="lightweight-updates"></a>Лёгкие изменения

`ClickhouseTableOptions(lightweight_updates=True)` ставит настройки таблицы
`enable_block_number_column` и `enable_block_offset_column`; `save()`, `update()` и `bulk_update()` такой
таблицы пишут `UPDATE t SET ... WHERE ...`. Новые значения записываются рядом со строкой и сразу читаются
вместо старых — мутация же переписывает каждую часть, где есть такая строка. Колонка ключа не меняется ни
так, ни так. Миграция включает и выключает опцию через `MODIFY SETTING`/`RESET SETTING`, не пересоздавая
таблицу.

ClickHouse не меняет колонку, которую читают его ключи (`order_by`, `sample_by`, `partition_by`, ключ
распределения — имя поля или выражение hare; колонки сырого ключа сервер отклоняет сам) или аргументы
движка (версия, знак). `save()` оставляет такую колонку как есть: сначала
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
`hare drift` читают тип и его аргументы обратно — `bloom_filter`, записанный без аргумента, как индекс с
умолчанием ClickHouse 0.025, — а ключ-выражение (`BloomFilterIndex(Lower("title"), name=...)`) — как его
SQL. `MinMaxIndex` с гранулярностью 1 — тот же
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
