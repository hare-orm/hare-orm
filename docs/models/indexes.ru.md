# Индексы

Индексы, которые модель объявляет в `Meta.indexes`: `Index`, `PartialIndex` и типы индексов диалекта.
Миграции создают, меняют и удаляют их вместе с моделью.

```python
from hare.ddl import Index, PartialIndex, RawSQLTerm
from hare.dialects.postgresql.indexes import GinIndex  # и другие типы индексов PostgreSQL
from hare.query.expressions import Q
from hare.query.functions import Lower


class Book(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=300)
    isbn = fields.CharField(max_length=13)
    published_at = fields.DateField(null=True)

    class Meta:
        indexes = (
            Index(fields=("-published_at", "title")),
            Index(Lower("title"), name="book_title_lower"),
            PartialIndex(fields=("isbn",), condition=Q(published_at__isnull=False), unique=True),
        )
```

## <a id="index"></a>`Index`

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
    ) -> None: ...
```

Ключи индекса — `fields` или `expressions`: одно из двух, не оба, иначе `ConfigurationError`.
`Meta.indexes` принимает и просто кортеж имён полей — индекс по их колонкам с созданным именем, как даёт
`Index(fields=...)`.

## <a id="key-order"></a>Порядок ключей и место NULL

Ключ индекса упорядочен по возрастанию, если имя поля не начинается с `-`:
`Index(fields=("-published_at", "title"))` — это `(published_at DESC, title)`; связь с ключом из
нескольких колонок задаёт направление каждой из них — `("-owner", "title")` — это
`(owner_a DESC, owner_b DESC, title)`. Чтобы явно указать, где
окажутся `NULL`, запишите ключ как сортировку —
`Index(F("rating").desc(nulls_last=True), name="book_rating_nulls_last")`,
`Index(F("rating").asc(nulls_first=True), F("price"), name=...)`. Ключи `F("a")`/`F("a").desc()` без
указания места `NULL` дают тот же индекс, что `fields=("a",)`/`("-a",)`. В индексах SQLite место `NULL`
задать нельзя (они идут первыми при возрастании и последними при убывании), а индексы ClickHouse не хранят
направления ключа вовсе: модель с таким индексом даёт `ConfigurationError` при привязке к подключению
(`Hare.init()`), до того как `makemigrations` запишет миграцию, которую не применит ни один `migrate`.
Так же отклоняется выражение, читающее связанную модель (`Coalesce(F("owner"), 0)`,
`Lower("owner__name")`): индекс читает только колонки своей таблицы — назовите колонку связи,
`Coalesce(F("owner_id"), 0)`.

## <a id="expression-keys"></a>Ключи-выражения

Ключ может быть выражением с направлением и местом `NULL`, как выше:
`Index(Lower("title").desc(nulls_last=True), name="book_title_lower_desc")`. Ключ с правилом сравнения —
тоже выражение: `Index(Collate("title", "C"), name="book_title_c")`. Каждая база пишет ключ-выражение
своим SQL — в том числе `Index(Trunc("published_at", "month"))`,
`Index(Extract("published_at", "year"))`, `Index(Cast("price", IntField()))`,
`Index(F("details__edition"))`; безымянный индекс получает имя по своим ключам — одно и то же на любой
базе.

Ключ, SQL которого требует одну из функций, что hare устанавливает на свои подключения SQLite (усечённая
или извлечённая часть даты, приведение типа), даёт `UnSupportedError` при создании индекса на SQLite:
база поддерживает индекс без hare, и любой другой клиент не смог бы писать в таблицу (см. [В ограничениях
и индексах](../dialects/sqlite/functions.ru.md#in-constraints-and-indexes)).

`UniqueConstraint` принимает имена полей; уникальность выражения задаёт
`Index(<expression>, unique=True)`.

## <a id="include"></a>Неключевые колонки: `include`

`include` перечисляет поля, которые хранятся в индексе как дополнительные, не ключевые колонки (`INCLUDE`
в PostgreSQL), поэтому запрос, читающий только их и колонки ключа, получает ответ из одного индекса. В
SQLite таких колонок нет, и индекс создаётся без них. Их принимают индексы B-tree, GiST и SP-GiST —
`GinIndex`/`BrinIndex`/`HashIndex`/`BloomIndex`/`IvfflatIndex`/`HnswIndex` дают для них
`UnSupportedError`.

```python
class Meta:
    indexes = (Index(fields=("author",), include=("title", "published_at")),)
```

## <a id="opclasses"></a>Классы операторов: `opclasses`

`opclasses` задаёт класс операторов каждому полю из `fields`, поэтому требует `fields` и по одному классу
на поле (иначе `ConfigurationError`) — например, индекс PostgreSQL для `LIKE` по началу строки:

```python
class Meta:
    indexes = (Index(fields=("isbn",), opclasses=("varchar_pattern_ops",)),)
```

## <a id="partial-index"></a>`PartialIndex`

```python
class PartialIndex(Index):
    def __init__(
        self,
        *expressions: Term | Expression | Ordering,
        fields: tuple[str, ...] | list[str] | None = None,
        name: str | None = None,
        # condition: Q — условие по полям модели; RawSQLTerm — готовое условие WHERE
        condition: Q | RawSQLTerm | None = None,
        opclasses: tuple[str, ...] | list[str] | None = None,
        unique: bool = False,
        include: tuple[str, ...] | list[str] | None = None,
    ) -> None: ...
```

Индекс строк, подходящих под `condition`. Условие `Q` читает собственные поля модели, как условие
ограничения, — см. [Ограничения](constraints-and-triggers.ru.md#constraints). Условие `RawSQLTerm`
вставляется как есть в качестве условия `WHERE` — для того, что не выразить через `Q` (вызов функции,
оператор конкретной базы):

```python
PartialIndex(fields=("title",), condition=RawSQLTerm("length(title) > 100"))
```

База без частичных индексов (`Features.supports_partial_indexes` — ClickHouse) получает индекс по всем
строкам: он обслуживает те же запросы, что и частичный, и `hare drift` принимает его за объявленный
индекс. Уникальный индекс или `UniqueConstraint` там отклоняет модель при привязке, а миграцию, которая
его добавляет, — до её первой команды: ClickHouse ничего не хранит уникальным; см. [Модели
ClickHouse](../dialects/clickhouse/models.ru.md#uniqueness-and-relations).

## <a id="index-types"></a>Типы индексов диалекта

В PostgreSQL есть семейство типов индексов в `hare.dialects.postgresql.indexes` —
`GinIndex`/`GistIndex`/`BrinIndex`/`BloomIndex`/`HashIndex`/`SpGistIndex`. Все они подклассы
`PartialIndex` с тем же конструктором, поэтому все принимают и `condition=`. Уникальным бывает только
обычный индекс: `unique=True` у типа индекса даёт `UnSupportedError`. См. [Типы индексов
PostgreSQL](../dialects/postgresql/indexes-and-tables.ru.md#postgresql-index-types).

## <a id="index-names"></a>Имена индексов

Индекс без `name=` получает имя, составленное из имени таблицы и колонок, а также, если заданы, его
метода доступа, классов операторов, колонок `include`, параметров хранения и условия. Поэтому несколько
безымянных индексов по одним и тем же колонкам (GiST и SP-GiST, два HNSW с разными классами операторов,
обычный и частичный) получают разные имена. Имя обычного индекса-дерева зависит только от таблицы и
колонок.

Модель, индексы которой получили бы одно имя, вызывает `ConfigurationError` при объявлении: один и тот же
безымянный индекс объявлен дважды, или одно `name=` дано двум индексам — либо индексу и ограничению
уникальности или исключения, которые база тоже хранит как индексы. Так же — уникальный индекс рядом с
`UniqueConstraint` по тем же полям и с тем же условием (или без условия): одно правило объявлено дважды.
С разными условиями (`PartialIndex(fields=["isbn"], condition=Q(in_print=True), unique=True)` и
`UniqueConstraint(fields=["isbn"], condition=Q(archived=False))`) это два правила, и остаются оба.
