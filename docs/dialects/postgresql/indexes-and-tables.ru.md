# PostgreSQL: индексы и таблицы

Типы индексов PostgreSQL сверх B-дерева, которое есть у каждой базы, и секционированные таблицы.

## <a id="postgresql-index-types"></a>Типы индексов

`hare.dialects.postgresql.indexes` — все они подклассы
[`PartialIndex`](../../models/indexes.ru.md#partial-index) с тем же конструктором, поэтому каждый из них
поддерживает и `condition=`:

`GinIndex`, `GistIndex`, `BrinIndex`, `BloomIndex`, `HashIndex`, `SpGistIndex`. Их основу,
`PostgresqlIndex`, наследует индекс другого метода доступа PostgreSQL.

Это индексы только PostgreSQL, как и `HnswIndex`/`IvfflatIndex`: на базе другого диалекта их создание или
удаление — через `generate_schemas()` или миграцию — даёт `UnSupportedError` до отправки SQL. Объявите в
модели индекс этой базы или поправьте миграцию. Собственные индексы SQLite — так же, только наоборот:
[`FullTextIndex`](../search-and-geodata/full-text-search.ru.md#fulltextindex) и
[`SpatialiteIndex`](../search-and-geodata/gis.ru.md#spatial-index). В ClickHouse `Index` — это индекс
пропуска данных (см. [ClickHouse](../clickhouse/models.ru.md)).

```python
from hare.dialects.postgresql.indexes import GinIndex


class Book(Model):
    tags = fields.JSONField(default=list, field_type=list[str])

    class Meta:
        indexes = (GinIndex(fields=("tags",)),)
```

## <a id="partitioning"></a>Партиционированные таблицы

`PostgresqlTableOptions(partitioning=...)` делает таблицу партиционированной: она создаётся с
`PARTITION BY <strategy> (<key columns>)`, а каждая партиция — отдельной таблицей с именем
`<table>_<partition name>` (в пределах 63 байт, как любое имя, которое генерирует hare). Классы лежат в
`hare.dialects.postgresql.partitioning`:

```python
import datetime

from hare import fields
from hare.dialects.postgresql.partitioning import (
    HashPartitioning, ListPartition, ListPartitioning, RangeBound, RangePartition, RangePartitioning,
)
from hare.dialects.postgresql.postgresql_table_options import PostgresqlTableOptions
from hare.models import Model


class ReadingHistory(Model):
    reader_id = fields.BigIntField()
    book_id = fields.BigIntField()
    pk = fields.CompositePrimaryKey("reader_id", "book_id")

    class Meta:
        table_options = [
            PostgresqlTableOptions(
                # readinghistory_p0 ... readinghistory_p31, FOR VALUES WITH (MODULUS 32, REMAINDER i)
                partitioning=HashPartitioning(fields=("reader_id",), partition_count=32),
                storage_parameters={"autovacuum_vacuum_scale_factor": 0.02},
            )
        ]


class StoreSale(Model):
    region = fields.CharField(max_length=10)
    number = fields.IntField()
    pk = fields.CompositePrimaryKey("region", "number")

    class Meta:
        table_options = [
            PostgresqlTableOptions(
                partitioning=ListPartitioning(
                    fields=("region",),
                    partitions=[
                        ListPartition("west", values=["us", "ca"]),  # storesale_west FOR VALUES IN ('us', 'ca')
                        ListPartition("east", values=["jp"]),
                    ],
                    default_partition="other",  # storesale_other DEFAULT — все остальные регионы
                )
            )
        ]


class LoanRecord(Model):
    day = fields.DateField()
    number = fields.IntField()
    pk = fields.CompositePrimaryKey("day", "number")

    class Meta:
        table_options = [
            PostgresqlTableOptions(
                partitioning=RangePartitioning(
                    fields=("day",),
                    partitions=[
                        RangePartition("old", from_values=(RangeBound.MINVALUE,), to_values=(datetime.date(2026, 1, 1),)),
                        RangePartition(
                            "y2026", from_values=(datetime.date(2026, 1, 1),), to_values=(datetime.date(2027, 1, 1),)
                        ),
                    ],
                    default_partition="later",
                )
            )
        ]
```

| Класс | Аргументы | Партиции |
|---|---|---|
| `HashPartitioning` | `fields`, `partition_count` (от 1 до 1024) | `<table>_p0` ... `<table>_p<n-1>`, строки распределяются равномерно по хешу ключа. |
| `ListPartitioning` | `fields` (одно поле), `partitions` (`ListPartition(name, values)`), `default_partition` | Каждая партиция хранит строки с перечисленными значениями ключа; `None` среди них означает строки с ключом `NULL`. |
| `RangePartitioning` | `fields`, `partitions` (`RangePartition(name, from_values, to_values)`), `default_partition` | Каждая партиция хранит строки от нижней границы до верхней, не включая её, — по одному значению на колонку ключа. `RangeBound.MINVALUE`/`RangeBound.MAXVALUE` стоят ниже/выше любого значения. |

`fields` задаёт ключ: поля с колонкой или внешний ключ, который означает свои ключевые колонки.
`default_partition` — имя партиции для строк, которые не подходят ни одной другой; без неё строку,
которой не подходит ни одна партиция, база отвергает (`IntegrityError`). Порядок, в котором перечислены
партиции, не важен.

То, что требует PostgreSQL, проверяется до записи любого DDL — при создании таблицы и когда последующая
миграция добавляет ограничение, индекс или поле либо меняет поле (`AddConstraint`, `AddIndex`,
`AddField`, `AlterField`), — `ConfigurationError` называет проблему:

- поля ключа существуют и имеют колонки;
- первичный ключ, каждое уникальное поле, `UniqueConstraint`, уникальный `Index` и `ExclusionConstraint`
  (с `=`) включают все колонки ключа — PostgreSQL проверяет каждое из них внутри одной партиции. Модель,
  чей ключ — один генерируемый `id`, можно партиционировать только по этому `id`;
- партиционированная таблица не может быть `unlogged`;
- имена партиций различаются, значение `ListPartitioning` перечислено один раз, диапазоны
  `RangePartition` не пересекаются и дают по одному значению на колонку ключа.

`ExclusionConstraint` на партиционированной таблице требует PostgreSQL 17 (на более старом сервере —
`UnSupportedError`). Внешние ключи на партиционированную таблицу и из неё работают как у обычной.

Сама партиционированная таблица строк не хранит, поэтому `storage_parameters` задаются на каждой партиции
(при создании и при изменении), а `tablespace` — на таблице (туда попадают новые партиции) и на каждой
партиции. Индексы и ограничения создаются на таблице и распространяются на все партиции. Конкурентный
индекс (`AddIndex(concurrently=True)`) нельзя построить на партиционированной таблице целиком: hare
создаёт его только на самой таблице, строит конкурентно на каждой партиции и присоединяет каждый — он
становится действительным, когда присоединён последний; удаление индекса конкурентным не бывает.

Как изменение попадает в базу:

| Изменение | Миграция |
|---|---|
| Добавлена `ListPartition`/`RangePartition` или партиция по умолчанию | `AddPartition` — новая пустая партиция. PostgreSQL отказывает, пока в партиции по умолчанию лежат строки, которые относятся к новой. |
| Партиция убрана | `RemovePartition` — партиция отсоединяется и удаляется **вместе со строками**; `makemigrations` предупреждает. |
| Изменены значения или границы партиции | `RemovePartition` и `AddPartition` с тем же именем — её строки теряются. |
| `storage_parameters`, `tablespace` | `AlterModelOptions`, на месте на каждой партиции. |
| Партиционирование включено или снято, другая стратегия, ключ или `partition_count` | `AlterModelOptions`, который создаёт таблицу заново: строки копируются, индексы, ограничения, комментарии, триггеры и внешние ключи других таблиц, ссылающиеся на неё, ставятся снова. Вся таблица переписывается под исключительной блокировкой. |
| Изменена `Meta.table`, `RenameModel` | Партиции переименовываются вместе с таблицей. |

В SQLite (и в любом другом диалекте) запись `PostgresqlTableOptions` не используется: таблица обычная, а
операции над партициями меняют там только состояние миграций.
