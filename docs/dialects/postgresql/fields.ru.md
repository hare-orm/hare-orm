# Поля PostgreSQL

Всё на этой странице находится в `hare.dialects.postgresql` — собственном пакете диалекта PostgreSQL —
и работает только с подключением к PostgreSQL. Если объявить любое из этих полей в модели, которая
работает с другим диалектом (SQLite), при создании команд таблицы будет `UnSupportedError`, а не
команды, не имеющие смысла для этой базы.

## `ArrayField` {: #arrayfield }

```python
ArrayField(base_field: Field, **kwargs)
```
Настоящий массив PostgreSQL. Тип элемента задаётся настоящим объектом поля, как в Django, а не
строкой с названием типа:

```python
class Article(Model):
    tags = fields.ArrayField(base_field=fields.TextField())
```

Элемент `None` хранится как элемент SQL `NULL` при любом `base_field` (`[1, None, 3]` в массиве
`IntField`); каждый другой элемент проходит преобразование и проверку самого `base_field`.

Собственные операторы фильтра: `contains`, `contained_by`, `overlap`, `len`, а также пути к элементу
(`tags__0`), срезу (`tags__0_2`) или длине (`tags__len`) — см.
[Операторы фильтра и пути](#array-lookups).

### Операторы фильтра и пути {: #array-lookups }

Тоже отдельный набор: `exact`, `not`, `isnull`, `not_isnull`, `contains`, `contained_by`, `overlap`,
`len`, `item`.

```python
await Article.objects.filter(tags__contains=["python"])
await Article.objects.filter(tags__overlap=["python", "orm"])
```

`item` сравнивает один элемент по позиции, отсчитываемой с 0 (как в Python); внутри она переводится в
индекс массива PostgreSQL, который отсчитывается с 1:

```python
await Article.objects.filter(tags__item=(0, "python"))  # tags[1] = 'python'
```

У вложенного массива (`ArrayField(base_field=ArrayField(...))`) `len` считает строки (первое
измерение), а `item` сравнивает строку целиком: `matrix__item=(0, [1, 2])`.

Чтобы получить элемент как вычисляемое значение, а не фильтровать по нему, используйте
[`ArrayItem`](functions.ru.md) — позиция тоже отсчитывается с 0:

```python
await Article.objects.annotate(first_tag=ArrayItem("tags", 0)).values("first_tag")
```

Путь через массив читает значение, которое принимает операторы своего типа, как в Django:

| Часть пути | Что читает | Тип |
|---|---|---|
| `tags__0` | элемент на позиции, отсчитываемой с 0 (`-1` — последний); `NULL` за пределами массива | тип элемента |
| `tags__0_2` | элементы с 0 до 2, не включая 2 | тип массива |
| `tags__len` | число элементов (строк у вложенного массива) — `0` у пустого массива | целое число |

Части пути можно продолжать, в том числе через вложенные массивы (`matrix__1__0`, `matrix__0__len`),
и те же пути работают в `F()`, `values()`/`values_list()`, `order_by()`, `group_by()` и после связи
(`articles__tags__0`):

```python
await Article.objects.filter(tags__0__startswith="py")
await Article.objects.filter(tags__len__gte=2)
await Article.objects.filter(tags__0_2__contains=["orm"])
await Article.objects.all().order_by("tags__0").values_list("tags__0", "tags__len")
```

## `CitextField` {: #citextfield }

```python
CitextField(**kwargs)
```
Колонка `CITEXT` PostgreSQL — текст без учёта регистра: сравнение, индексы и сортировка работают без
`UPPER()`/`LOWER()` с любой стороны запроса. Нужно расширение `citext`; автоматическое создание
миграций само добавляет `CreateExtension("citext")` везде, где используется это поле, — объявлять
`Meta.extensions` не нужно (см. [Операции миграций — расширения](../../migrations/operations.ru.md#extensions)).

```python
class User(Model):
    email = CitextField()

await User.objects.filter(email="Someone@Example.com").first()  # совпадёт и с "someone@example.com"
```

`__contains`/`__startswith`/`__endswith` тоже не учитывают регистр: колонка сравнивается как `citext`,
а не приводится к `VARCHAR`.

Значение с нулевым байтом даёт `ValidationError`, как и у `CharField`/`TextField`.

## `HStoreField` {: #hstorefield }

```python
HStoreField(**kwargs)
```
Колонка `hstore` PostgreSQL — плоский набор пар «строковый ключ — строка или `None`», в Python это
`dict`. Нужно расширение `hstore`; автоматическое создание миграций добавляет `CreateExtension("hstore")`
везде, где используется это поле. Ключ или значение, которые не являются строкой, записываются как их
`str()`, как в Django (`{"n": 5}` читается как `{"n": "5"}`); значение, которое не является `dict`, или
нулевой байт дают `ValidationError`.

```python
class Product(Model):
    attributes = HStoreField(null=True)

await Product.objects.filter(attributes__color="red")
await Product.objects.filter(attributes__has_keys=["color", "size"])
await Product.objects.all().values_list("attributes__color", "attributes__keys")
```

Операторы фильтра и пути — см. [Операторы фильтра и пути](#hstore-lookups).

### Операторы фильтра и пути {: #hstore-lookups }

`exact`, `not`, `in`, `not_in`, `isnull`, `not_isnull` сравнивают всё значение; `contains`/
`contained_by` принимают `dict` пар (`@>`/`<@`); `has_key` принимает ключ, `has_keys`/`has_any_keys` —
список ключей (`?`/`?&`/`?|`).

Любое другое имя после поля — это ключ: `attributes__color` — его значение (текст, `None`, если ключа
нет), оно принимает текстовые операторы (`attributes__color__startswith="bl"`). `attributes__keys` и
`attributes__values` — все ключи/значения в виде текстового массива, с операторами и путями
`ArrayField` (`attributes__keys__contains=["size"]`, `attributes__keys__len=3`). Ключ, названный как
оператор, доступен через `contains`/`has_key`. Те же пути работают в `F()`, `values()`, `order_by()` и
после связи.

```python
await Product.objects.filter(attributes__contains={"color": "red"})
await Product.objects.filter(attributes__size__in=["M", "L"])
await Product.objects.filter(attributes__keys__overlap=["size", "weight"])
```

## `TSVectorField` {: #tsvectorfield }

```python
TSVectorField(source_fields: Sequence[str] | str | None = None, config: str | None = None, weights: Sequence[str] | None = None, stored: bool = True, **kwargs)
```
При `stored=True` и заданных `source_fields` создаётся как `GENERATED ALWAYS AS (...) STORED` — значение
поддерживает сам PostgreSQL, код приложения для этого не нужен:

```python
class Article(Model):
    title = fields.CharField(max_length=300)
    body = fields.TextField()
    search_vector = TSVectorField(source_fields=("title", "body"), config="english")
```

## `PostGISField` {: #postgisfield }

```python
PostGISField()
```
Хранит `geography(Point,4326)` — широту и долготу в WGS84 с точными расчётами расстояний на стороне
базы. Нужно расширение `postgis`; автоматическое создание миграций добавляет
`CreateExtension("postgis")` везде, где используется это поле. Значение в Python — кортеж
**`(широта, долгота)`**.

```python
class Store(Model):
    location = PostGISField()

    class Meta:
        indexes = (GistIndex(fields=("location",)),)
```

Сразу есть оператор фильтра `within_km`:

```python
await Store.objects.filter(location__within_km=(55.75, 37.62, 10))  # в пределах 10 км от точки
```

Для сортировки и фильтров по расстоянию сложнее простого радиуса используйте вычисляемые значения
`STDistance`/`STDWithin` (см. [Функции PostgreSQL](functions.ru.md)).

## Поля диапазонов {: #range-fields }

**Поля диапазонов** — `RangeField` служит основой (напрямую не используется); значение в Python —
`hare.dialects.postgresql.fields.ranges.Range` или, для краткости, простой кортеж `(нижняя, верхняя)`
(полуоткрытый диапазон `[)`):

```python
@dataclass(frozen=True)
class Range(Generic[T]):
    lower: T | None = None       # None — без границы
    upper: T | None = None       # None — без границы
    lower_inc: bool = True       # нижняя граница включается ("[")
    upper_inc: bool = False      # верхняя граница включается ("]") — по умолчанию в PostgreSQL [)
    is_empty: bool = False
```

!!! warning "Пустой диапазон и диапазон без границ"
    PostgreSQL приводит любой диапазон, в который не входит ни одно значение (например, с равными
    границами при полуоткрытом `[)` по умолчанию), к своему значению `'empty'`. При чтении оно даёт
    `lower=None, upper=None` — **то же самое**, что диапазон вообще без границ, если не проверять ещё и
    `is_empty`. Если записать прочитанный диапазон обратно, не сохранив `is_empty`, «ни одного значения»
    молча превратится в «все значения».

Присвоенный диапазон (при создании объекта, в `create()` или присваивании атрибуту) сразу хранится в
памяти в том виде, в каком его возвращает PostgreSQL: границы приводятся к типу элемента поля (строка
с датой становится `date`, граница даты-времени подчиняется правилам `DatetimeField` — см. ниже),
дискретный диапазон (`IntRangeField`/`BigIntRangeField`/`DateRangeField`) переписывается в вид `[)`
(`Range(1, 5, lower_inc=False, upper_inc=True)` становится `Range(2, 6)`), сторона без границы
считается невключённой, а диапазон без значений становится пустым. Нижняя граница больше верхней
даёт `ValidationError`.

Граница `DateTimeRangeField` означает то же, что значение `DatetimeField`: граница без пояса — это
время на часах в настроенном поясе при `use_tz=True` и в местном поясе системы при `use_tz=False`, а
читаются границы в настроенном поясе (`use_tz=True`) или как местное время без пояса (`use_tz=False`),
поэтому `during__contains=at` согласуется с `DatetimeField`, хранящим то же значение без пояса.

Значение `infinity`/`-infinity` (колонка `DATE`/`TIMESTAMPTZ` или граница диапазона) на обоих
драйверах читается как крайнее значение Python — `date.max`/`date.min`, `datetime.max`/`datetime.min`,
а запись такого значения обратно снова сохраняет `infinity`/`-infinity`, а не конечную дату. Граница
`DateTimeRangeField` в бесконечности при `use_tz=True` содержит пояс UTC
(`datetime.min.replace(tzinfo=UTC)`), а при `use_tz=False` — без пояса; граница `datetime.max`/
`datetime.min` без пояса считается той же бесконечностью, каким бы ни был настроенный пояс.

| Поле | Тип PostgreSQL |
|---|---|
| `IntRangeField` | `int4range` |
| `BigIntRangeField` | `int8range` |
| `DecimalRangeField` | `numrange` |
| `DateRangeField` | `daterange` |
| `DateTimeRangeField` | `tstzrange` |

Операторы фильтра (`contains`, `overlap`, `fully_lt`, `adjacent_to`, ...) и пути к границе
(`during__startswith`) или признаку (`during__isempty`) — см.
[Операторы фильтра и пути](#range-lookups).

Поля диапазонов хорошо сочетаются с `ExclusionConstraint` для расписаний без пересечений — см.
[Ограничения](../../models/constraints-and-triggers.ru.md#constraints).

### Операторы фильтра и пути {: #range-lookups }

`exact`, `not`, `in`, `not_in`, `isnull`, `not_isnull`, а также операторы диапазонов PostgreSQL —
каждый принимает `Range` (или кортеж `(нижняя, верхняя)`), а `contains` ещё и одно значение:

| Оператор | В SQL | Подходят строки, где диапазон |
|---|---|---|
| `contains` | `@>` | содержит значение или весь диапазон |
| `contained_by` | `<@` | лежит внутри диапазона |
| `overlap` | `&&` | имеет общее значение с диапазоном |
| `fully_lt` | `<<` | лежит целиком ниже него |
| `fully_gt` | `>>` | лежит целиком выше него |
| `not_lt` | `&>` | не выходит ниже него |
| `not_gt` | `&<` | не выходит выше него |
| `adjacent_to` | <code>-&#124;-</code> | касается его, не пересекаясь |

Путь читает часть диапазона с её собственными операторами: `startswith`/`endswith` — нижняя/верхняя
граница (типа элемента — `date` у `DateRangeField`, `NULL` без границы или у пустого диапазона), а
`isempty`, `lower_inc`, `lower_inf`, `upper_inc`, `upper_inf` — логические значения. Они так же
работают в `F()`, `values()`, `order_by()` и после связи:

```python
await Booking.objects.filter(during__fully_lt=Range(start, end))
await Booking.objects.filter(during__startswith__year=2024)
await Booking.objects.filter(during__upper_inf=True)
await Booking.objects.all().order_by("during__startswith").values("id", "during__endswith")
```
