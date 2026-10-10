# Поиск по векторам

Векторные колонки и поиск похожих — один API на каждом диалекте с поиском по векторам; каждый диалект
хранит векторы и пишет расстояния по-своему. В ClickHouse через hare его нет
(`Features.supports_vector_search` выключен): расстояние между векторами и `__nearby` дают там `UnSupportedError`.

| | PostgreSQL | SQLite |
| --- | --- | --- |
| Движок | расширение [pgvector](https://github.com/pgvector/pgvector) | расширение [sqlite-vec](https://github.com/asg017/sqlite-vec) |
| Колонка | `vector(N)` | `BLOB` float32 формата sqlite-vec |
| Размерность | от 1 до 16000 | от 1 до 8192 |
| Расстояния | операторы pgvector `<->`, `<=>`, `<#>` | `vec_distance_l2()`, `vec_distance_cosine()`, скалярное произведение со знаком минус — функция hare |
| Индексы | `HnswIndex`, `IvfflatIndex` (ANN) | нет — просматривается каждая строка |
| Подготовка | не нужна — автоматическое создание миграций само добавляет `CreateExtension("vector")` | `pip install hare-orm[sqlite-vec]` и `load_sqlite_vec=true` в соединении |

Расстояниям и `__nearby` нужен `Features.supports_vector_search`; на соединении без него они дают
`UnSupportedError` до отправки SQL.

```python
from hare.vectors import CosineDistance, InnerProduct, L2Distance, VectorField


class Item(Model):
    embedding = VectorField(dimensions=384)


await Item.objects.annotate(distance=CosineDistance("embedding", query_vector)).order_by("distance")[:10]
await Item.objects.filter(embedding__nearby=(query_vector, 0.5))
```

## <a id="the-field"></a>Поле

```python
VectorField(dimensions: int, **kwargs)
```

Список чисел фиксированной длины. `dimensions` — `int` от 1 до 16000 (иначе `ConfigurationError`);
диалект, который принимает меньше (SQLite — 8192), даёт `UnSupportedError` при создании колонки или
записи значения. Значение в Python — обычный `list[float]`; каждый элемент должен быть конечным числом,
помещающимся во float32 (`float4` в PostgreSQL), а значение другой длины тоже даёт `ValidationError`.

В PostgreSQL значение на обоих драйверах читается как кратчайшая десятичная запись своего `float4`
(`0.1`, а не `0.10000000149011612`). В SQLite значения пишутся и читаются без расширения — оно нужно
только расстояниям.

## <a id="distances"></a>Расстояния

Три выражения расстояния, которые можно использовать в `.annotate()`/`.order_by()`/`.filter()` как
любое вычисляемое значение; они сравнивают поле (имя или уже построенный `Term`/`Expression`) с вектором
запроса (обычный `list[float]` или другое поле или выражение). У каждого меньше — значит ближе:

| Выражение | Что означает |
| --- | --- |
| `L2Distance(field, vector)` | Евклидово расстояние |
| `CosineDistance(field, vector)` | Косинусное расстояние |
| `InnerProduct(field, vector)` | Скалярное произведение **со знаком минус** — чтобы меньшее значение по-прежнему означало «более похожий», как у двух других |

```python
results = (
    await Item.objects.annotate(dist=L2Distance("embedding", query_vector))
    .order_by("dist")
    .limit(10)
)
```

Вектор запроса преобразуется так, как диалект хранит поле, с которым его сравнивают.

Все три — `VectorDistanceExpression` (`hare.vectors`), каждое объявляет свой `VectorDistanceType`
(`L2`, `COSINE`, `NEGATIVE_INNER_PRODUCT`) — то, что диалект превращает в свой оператор или функцию.

## <a id="nearby"></a>`__nearby`

`field__nearby=(vector, max_distance)` — строки в пределах L2-расстояния от `vector`, то же, что
вычисляемое значение `L2Distance` с фильтром `__lte`. Значение, которое не пара, или расстояние, которое
не конечное число, дают `ValidationError`:

```python
await Item.objects.filter(embedding__nearby=(query_vector, max_distance))
```

## <a id="postgresql"></a>PostgreSQL: pgvector

pgvector — расширение базы, а не пакет Python: в `pyproject.toml` ничего добавлять не нужно.
Автоматическое создание миграций само добавляет `CreateExtension("vector")` везде, где используется
`VectorField`, — объявлять `Meta.extensions` не нужно (см.
[Операции миграций — расширения](../../migrations/operations.ru.md#extensions)).

**Индексы** — `IvfflatIndex(*expressions, lists: int = 100, ...)` и `HnswIndex(*expressions, m: int = 16,
ef_construction: int = 64, ...)` из `hare.dialects.postgresql.indexes` вместе с общими индексами
PostgreSQL (см. [Индексы](../../models/indexes.ru.md#postgresql-index-types)); оба принимают `opclasses=`,
чтобы выбрать расстояние, которое поддерживает индекс (`vector_l2_ops`, `vector_cosine_ops`,
`vector_ip_ops`). `HnswIndex(fields=...)` его требует — у pgvector нет класса операторов HNSW по
умолчанию, поэтому без него будет `ConfigurationError`; у `IvfflatIndex` по умолчанию `vector_l2_ops`.
Параметры хранения проверяются по ограничениям pgvector — `lists` от 1 до 32768, `m` от 2 до 100,
`ef_construction` от 4 до 1000 и не меньше `2 * m`; всё остальное (`bool`, строка, число вне
диапазона) даёт `ConfigurationError`:

```python
from hare.dialects.postgresql.indexes import HnswIndex


class Item(Model):
    embedding = VectorField(dimensions=1536)

    class Meta:
        indexes = (HnswIndex(fields=("embedding",), m=16, ef_construction=64, opclasses=("vector_cosine_ops",)),)
```

> [!WARNING]
> **WITH перед WHERE**
>
> Если такой индекс ещё и частичный (`condition=`), *параметры хранения*
> (`WITH (lists=...)` / `WITH (m=..., ef_construction=...)`) должны идти в команде `CREATE INDEX`
> **раньше** *условия* (`WHERE (...)`) — это жёсткое требование синтаксиса PostgreSQL, а не вопрос
> стиля. `IvfflatIndex`/`HnswIndex` уже сами расставляют их правильно; это примечание объясняет
> *почему*, а не требует от вас действий: если вы расширяете один из этих классов,
> `PartialIndex.__init__` только *добавляет в конец* своё условие `WHERE`, поэтому всё, что добавляет
> свои параметры хранения, должно вставлять их *в начало*.

## <a id="sqlite"></a>SQLite: sqlite-vec

Установите extra `sqlite-vec` (`pip install hare-orm[sqlite-vec]`) и загрузите расширение в
соединение:

```python
DB_URL = "sqlite+aiosqlite://db.sqlite3?load_sqlite_vec=true"
```

- `load_sqlite_vec=true` загружает расширение в каждое соединение пула; без установленного пакета
  настройка соединения даёт `ConfigurationError`. Без этой настройки у соединения нет
  `supports_vector_search`, поэтому расстояние или `__nearby` дают `UnSupportedError` до отправки SQL.
- `L2Distance` и `CosineDistance` — это `vec_distance_l2()`/`vec_distance_cosine()` sqlite-vec;
  `InnerProduct` считает функция, которую регистрирует hare, — в sqlite-vec её нет. Расстояние равно
  NULL, если один из векторов NULL.
- sqlite-vec просматривает каждую строку (индекса ANN нет) — подходит для объёмов до нескольких сотен
  тысяч векторов.
