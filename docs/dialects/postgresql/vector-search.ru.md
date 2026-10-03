# Поиск по векторам (`hare.dialects.postgresql.fields.vector`, pgvector)

Нужно расширение PostgreSQL [pgvector](https://github.com/pgvector/pgvector) (расширение базы, а не
пакет Python — в `pyproject.toml` ничего добавлять не нужно). Автоматическое создание миграций само
добавляет `CreateExtension("vector")` везде, где используется `VectorField`, — объявлять
`Meta.extensions` не нужно (см. [Операции миграций — расширения](../../migrations/operations.ru.md#extensions)).

```python
VectorField(dimensions: int, **kwargs)
```

Колонка `vector(N)` постоянной длины. Значение в Python — обычный `list[float]`; каждый элемент должен
быть конечным числом, помещающимся в `float4` (иначе `ValidationError`), и на обоих драйверах читается
как кратчайшая десятичная запись своего `float4` (`0.1`, а не `0.10000000149011612`):

```python
from hare.dialects.postgresql.fields.vector import VectorField
from hare.dialects.postgresql.functions.vector import CosineDistance, InnerProduct, L2Distance
from hare.dialects.postgresql.indexes import HnswIndex

class Item(Model):
    embedding = VectorField(dimensions=1536)

    class Meta:
        indexes = (HnswIndex(fields=("embedding",), m=16, ef_construction=64, opclasses=("vector_cosine_ops",)),)
```

Три выражения расстояния, которые можно использовать в `.annotate()`/`.order_by()`/`.filter()` как любое
вычисляемое значение; они сравнивают поле (имя или уже построенный `Term`/`Expression`) с вектором
запроса (обычный `list[float]` или другое поле или выражение):

| Выражение | Оператор | Что означает |
| --- | --- | --- |
| `L2Distance(field, vector)` | `<->` | Евклидово расстояние |
| `CosineDistance(field, vector)` | `<=>` | Косинусное расстояние |
| `InnerProduct(field, vector)` | `<#>` | Скалярное произведение **со знаком минус** — pgvector определяет его так, чтобы меньшее значение по-прежнему означало «более похожий», как у двух других |

```python
results = (
    await Item.objects.annotate(dist=L2Distance("embedding", query_vector))
    .order_by("dist")
    .limit(10)
)
```

Есть и сокращённый оператор фильтра `nearby` — то же, что вычисляемое значение `L2Distance` с фильтром
`__lte`, — для частого случая «всё в пределах некоторого расстояния» без явного `annotate`:

```python
await Item.objects.filter(embedding__nearby=(query_vector, max_distance))
```

**Индексы** — `IvfflatIndex(*, lists: int = 100, ...)` и `HnswIndex(*, m: int = 16,
ef_construction: int = 64, ...)` вместе с общими индексами PostgreSQL (см.
[Индексы](../../models/indexes.ru.md#postgresql-index-types)); оба принимают `opclasses=`,
чтобы выбрать, какое расстояние поддерживает индекс (`vector_l2_ops`, `vector_cosine_ops`,
`vector_ip_ops`). `HnswIndex(fields=...)` его требует — у pgvector нет класса операторов HNSW по
умолчанию, поэтому без него будет `ConfigurationError`; у `IvfflatIndex` по умолчанию
`vector_l2_ops`. Параметры хранения проверяются по ограничениям pgvector — `lists` от 1 до 32768, `m`
от 2 до 100, `ef_construction` от 4 до 1000 и не меньше `2 * m`; всё остальное (`bool`, строка, число
вне диапазона) даёт `ConfigurationError`:

```python
class Meta:
    indexes = (IvfflatIndex(fields=("embedding",), lists=100, opclasses=("vector_l2_ops",)),)
```

!!! warning "WITH перед WHERE"
    Если такой индекс ещё и частичный (`condition=`), *параметры хранения*
    (`WITH (lists=...)` / `WITH (m=..., ef_construction=...)`) должны идти в команде `CREATE INDEX`
    **раньше** *условия* (`WHERE (...)`) — это жёсткое требование синтаксиса PostgreSQL, а не вопрос
    стиля. `IvfflatIndex`/`HnswIndex` уже сами расставляют их правильно; это примечание объясняет
    *почему*, а не требует от вас действий: если вы расширяете один из этих классов,
    `PartialIndex.__init__` только *добавляет в конец* своё условие `WHERE`, поэтому всё, что добавляет
    свои параметры хранения, должно вставлять их *в начало*.
