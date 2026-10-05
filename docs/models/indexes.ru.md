# Индексы

Индексы, которые модель объявляет в `Meta.indexes`: `Index`, `PartialIndex` и типы индексов
диалекта.

```python
from hare.ddl import Index, PartialIndex, RawSQLTerm
from hare.dialects.postgresql.indexes import GinIndex  # и другие типы индексов PostgreSQL
```

```python
class Index:
    def __init__(
        self,
        *expressions: Term | Expression | Ordering,
        fields: tuple[str, ...] | list[str] | None = None,
        name: str | None = None,
        opclasses: tuple[str, ...] | list[str] | None = None,
        unique: bool = False,
        include: tuple[str, ...] | list[str] | None = None,
    ) -> None
```

Ключ индекса упорядочен по возрастанию, если имя поля не начинается с `-`, как в Django:
`Index(fields=("-created_at", "author"))` — это `(created_at DESC, author)`. Чтобы явно указать, где
окажутся `NULL`, запишите ключ как сортировку — `Index(F("rank").desc(nulls_last=True),
name="rank_nulls_last")`, `Index(F("rank").asc(nulls_first=True), F("score"), name=...)`. Ключи
`F("a")`/`F("a").desc()` без указания места `NULL` дают тот же индекс, что `fields=("a",)`/`("-a",)`.
В индексах SQLite место `NULL` задать нельзя (они идут первыми при возрастании и последними при
убывании), поэтому там это даёт `UnSupportedError` при создании индекса. Ключ с правилом сравнения строк (collation) —
это выражение: `Index(Collate("title", "C"), name="title_c")`.

`include` перечисляет поля, которые хранятся в индексе как дополнительные, не ключевые колонки
(`INCLUDE` в PostgreSQL), поэтому запрос, читающий только их и колонки ключа, получает ответ из
одного индекса. В SQLite таких колонок нет, и индекс создаётся без них. Их принимают индексы
B-tree, GiST и SP-GiST — `GinIndex`/`BrinIndex`/`HashIndex`/`BloomIndex`/`IvfflatIndex`/`HnswIndex`
дают для них `UnSupportedError`.

```python
class Meta:
    indexes = (Index(fields=("customer",), include=("total", "created_at")),)
```

`fields` и `expressions` взаимоисключающие; нужно хотя бы одно из них. `opclasses` требует `fields`
и должен совпадать с ним по длине — например, индекс PostgreSQL, пригодный для поиска `LIKE` по
началу строки:

```python
class Meta:
    indexes = (Index(fields=("path",), opclasses=("varchar_pattern_ops",)),)
```

```python
class PartialIndex(Index):
    def __init__(
        self,
        *expressions: Term | Expression | Ordering,
        fields: tuple[str, ...] | list[str] | None = None,
        name: str | None = None,
        condition: Q | RawSQLTerm | None = None,  # Q: условие по полям модели; RawSQLTerm: готовое условие WHERE
        opclasses: tuple[str, ...] | list[str] | None = None,
        unique: bool = False,
        include: tuple[str, ...] | list[str] | None = None,
    ) -> None
```

Условие `RawSQLTerm` вставляется как есть в качестве условия `WHERE` — для того, что не выразить
через `Q` (вызов функции, оператор конкретной базы):

```python
PartialIndex(fields=("status",), condition=RawSQLTerm("status IN ('open', 'in_progress')"))
```

В PostgreSQL есть ещё несколько видов индексов в `hare.dialects.postgresql.indexes` —
`GinIndex`/`GistIndex`/`BrinIndex`/`BloomIndex`/`HashIndex`/`SpGistIndex`. Все они подклассы
`PartialIndex` с тем же конструктором, поэтому все поддерживают `condition=`. См.
[Типы индексов PostgreSQL](#postgresql-index-types).

Индекс без `name=` получает имя, составленное из имени таблицы и колонок, а также, если заданы, его
метода доступа, классов операторов, колонок `include`, параметров хранения и условия. Поэтому
несколько безымянных индексов по одним и тем же колонкам (GiST и SP-GiST, два HNSW с разными
классами операторов, обычный и частичный) получают разные имена. Имя обычного индекса-дерева
зависит только от таблицы и колонок.

## <a id="postgresql-index-types"></a>Типы индексов PostgreSQL

`hare.dialects.postgresql.indexes` — все они подклассы `PartialIndex` (см. выше) с тем же конструктором, поэтому каждый из них
поддерживает и `condition=`:

`GinIndex`, `GistIndex`, `BrinIndex`, `BloomIndex`, `HashIndex`, `SpGistIndex`.

Это индексы только PostgreSQL, как и `HnswIndex`/`IvfflatIndex`: на базе другого диалекта их создание
или удаление — через `generate_schemas()` или миграцию — даёт `UnSupportedError` до отправки SQL.
Объявите в модели индекс этой базы или поправьте миграцию. Собственные индексы SQLite — так же, только
наоборот: [`FullTextIndex`](../dialects/search-and-geodata/full-text-search.ru.md#fulltextindex) и [`SpatialiteIndex`](../dialects/search-and-geodata/gis.ru.md#spatial-index).
В ClickHouse `Index` — это индекс пропуска данных, см. [ClickHouse](../dialects/clickhouse/models.ru.md).

```python
class Widget(Model):
    values = fields.JSONField(default=list, field_type=list[str])

    class Meta:
        indexes = (GinIndex(fields=("values",)),)
```
