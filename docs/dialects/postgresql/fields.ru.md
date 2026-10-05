# Поля PostgreSQL

Всё на этой странице находится в `hare.dialects.postgresql` — собственном пакете диалекта PostgreSQL —
и работает только с подключением к PostgreSQL. Если объявить любое из этих полей в модели, которая
работает с другим диалектом (SQLite, ClickHouse), при создании команд таблицы будет `UnSupportedError`, а не
команды, не имеющие смысла для этой базы. Все поля этой страницы, кроме `ArrayField`, импортируются из `hare.dialects.postgresql.fields`;
`ArrayField` — это `hare.fields.ArrayField`, который работает и в ClickHouse, — см.
[Типы ClickHouse](../clickhouse/types.ru.md#containers):

```python
from hare.dialects.postgresql.fields import (
    CitextField, HStoreField, InetField, IntRangeField, LtreeField, NativeEnumField,
    PostGISField, Range, TSVectorField,  # ... любое поле этой страницы
)
from hare.fields import ArrayField
```

## <a id="arrayfield"></a>`ArrayField`

```python
ArrayField(base_field: Field, **kwargs)
```
Настоящий массив PostgreSQL. Тип элемента задаётся настоящим объектом поля, как в Django, а не
строкой с названием типа:

```python
class Article(Model):
    tags = ArrayField(base_field=fields.TextField())
```

Элемент `None` хранится как элемент SQL `NULL` при любом `base_field` (`[1, None, 3]` в массиве
`IntField`); каждый другой элемент проходит преобразование и проверку самого `base_field`.

Собственные операторы фильтра: `contains`, `contained_by`, `overlap`, `len`, а также пути к элементу
(`tags__0`), срезу (`tags__0_2`) или длине (`tags__len`) — см.
[Операторы фильтра и пути](#array-lookups).

### <a id="array-lookups"></a>Операторы фильтра и пути

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

## <a id="citextfield"></a>`CitextField`

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

## <a id="nativeenumfield"></a>`NativeEnumField`

```python
from hare.dialects.postgresql.fields import NativeEnumField

NativeEnumField(enum_type, *, type_name=None, **kwargs)
```

Член перечисления в колонке собственного типа `ENUM` PostgreSQL — `CREATE TYPE order_status AS ENUM
('new', 'paid', 'shipped')`. Читается и записывается как `CharEnumField` (принимается член или его
значение, возвращается член), а сама база отклоняет любую другую метку и сортирует колонку в порядке
перечисления, а не по алфавиту.

```python
class OrderStatus(Enum):
    NEW = "new"
    PAID = "paid"
    SHIPPED = "shipped"


class Order(Model):
    status = NativeEnumField(OrderStatus)
    previous_status = NativeEnumField(OrderStatus, null=True)  # тот же тип


await Order.objects.filter(status__in=[OrderStatus.NEW, "paid"]).order_by("status")  # new, paid, shipped
```

- **Тип.** Его имя — `type_name`, иначе имя класса перечисления в snake case (`OrderStatus` →
  `order_status`) — идентификатор в нижнем регистре длиной до 63 символов. Метки — значения членов
  текстом (`str(member.value)`, поэтому подходит и `IntEnum`), в порядке перечисления, каждая до 63
  байт. Поля, называющие один тип, делят его; два таких поля с разными метками дают
  `ConfigurationError`. Перечисление без членов или неверное имя дают `ConfigurationError` при
  объявлении поля.
- **Схема.** `generate_schemas()` создаёт типы до таблиц (`safe=True` оставляет существующий).
  Автоопределение миграций пишет `CreateEnumType` перед первой колонкой типа, `AlterEnumType`, когда
  меняются члены перечисления, и `DropEnumType` после того, как ушла его последняя колонка, — см.
  [Операции миграций — типы ENUM](../../migrations/operations.ru.md#enum-types). Замена `CharEnumField` на
  `NativeEnumField` преобразует колонку на месте (`ALTER COLUMN ... TYPE ... USING`).
- **Изменение членов.** Новые члены, добавленные так, что старые сохраняют порядок, становятся новыми
  метками на месте (`ALTER TYPE ... ADD VALUE`) — добавленное так значение нельзя использовать в той же
  транзакции, поэтому записывайте такие строки в следующей миграции. Удалённый или переставленный член
  заменяет тип: колонки этого типа (и колонки-массивы) и их значения по умолчанию преобразуются в новый
  тип одной командой, поэтому ни одна строка не должна хранить удалённую метку — сначала обновите такие
  строки.
- **Фильтры** — общий набор: равенство, `__in`, `__isnull` и сравнения в порядке перечисления. Метка
  вне перечисления даёт `ValidationError` до выполнения запроса.
- Только PostgreSQL (`features.supports_enum_types`); на другой базе поле даёт `UnSupportedError`.

## <a id="network-fields"></a>Сетевые поля — `InetField`, `CidrField`, `MacAddressField`

```python
from hare.dialects.postgresql.fields import CidrField, InetField, MacAddressField


class Host(Model):
    address = InetField()          # inet
    subnet = CidrField(null=True)  # cidr
    mac = MacAddressField(null=True, unique=True)  # macaddr
```

- **`InetField`** — адрес узла IPv4 или IPv6 с длиной префикса его сети, если она есть. Читается как
  `ipaddress.IPv4Address`/`IPv6Address` или как `IPv4Interface`/`IPv6Interface` (адрес и сеть), когда
  префикс короче полной длины (`10.0.1.7/16`). Принимаются текст и объекты `ipaddress` (адрес, интерфейс,
  сеть).
- **`CidrField`** — сеть IPv4 или IPv6, читается как `ipaddress.IPv4Network`/`IPv6Network`; принимаются
  текст и объекты `ipaddress`, адрес — как сеть из него одного (`/32`, `/128`). Сеть с установленными
  битами узла (`10.0.0.1/8`) даёт `ValidationError`, как и колонка её не принимает.
- **`MacAddressField`** — 6-байтовый MAC-адрес в любой привычной форме (`08:00:2B:01:02:03`,
  `08-00-2b-01-02-03`, `0800.2b01.0203`, `08002b010203`); читается и записывается как
  `08:00:2b:01:02:03`.
- Значение не того вида даёт `ValidationError` до выполнения запроса — и при записи, и в фильтре.

Все три фильтруются по равенству, `__not`, `__in`/`__not_in`, `__isnull` и сравнениям (`__gt`, ...,
`__range`, в порядке базы); текстовых операторов у них нет. `InetField` и `CidrField` фильтруются ещё и по
подсетям — значение: адрес или сеть, текстом или объектом `ipaddress`, либо `F()` другой колонки:

| Оператор | В SQL | Истинно, когда адрес или сеть колонки |
|---|---|---|
| `__net_contained` | `<<` | лежит внутри значения и не равна ему |
| `__net_contained_or_equal` | `<<=` | лежит внутри значения или равна ему |
| `__net_contains` | `>>` | содержит значение и не равна ему |
| `__net_contains_or_equal` | `>>=` | содержит значение или равна ему |
| `__net_overlaps` | `&&` | содержит значение или лежит внутри него |

```python
await Host.objects.filter(address__net_contained="10.0.0.0/8")
await Host.objects.filter(subnet__net_contains=request_ip)
await Host.objects.filter(address__net_contained_or_equal=F("subnet"))
```

`__family` (4 или 6) и `__masklen` (длина префикса) читают часть значения как целое — в фильтрах,
`values()`, `F()` и `order_by()`: `filter(address__family=6)`, `values_list("subnet__masklen")`. Только
PostgreSQL: на другой базе поля дают `UnSupportedError`.

## <a id="hstorefield"></a>`HStoreField`

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

### <a id="hstore-lookups"></a>Операторы фильтра и пути

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

## <a id="tsvectorfield"></a>`TSVectorField`

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

## <a id="ltreefield"></a>`LtreeField`

Колонка `ltree` — место узла в дереве в виде пути из меток от корня (`Top.Science.Astronomy`), в
Python — `str`. Одна колонка отвечает на вопросы «все потомки», «все предки» и на поиск по шаблону
одним условием с индексом, без рекурсивного запроса. Нужно расширение `ltree`; автодетектор миграций
добавляет `CreateExtension("ltree")` везде, где используется поле.

```python
from hare.dialects.postgresql.fields import LtreeField
from hare.dialects.postgresql.indexes import GistIndex


class Category(Model):
    path = LtreeField(unique=True)

    class Meta:
        indexes = (GistIndex(fields=("path",)),)  # обслуживает операторы дерева и шаблонов
```

Путь записывается текстом или списком/кортежем меток (`["Top", "Science"]` — это `"Top.Science"`,
`[]` и `""` — пустой путь), читается текстом. Метка состоит из букв, цифр, `_` и `-` (дефису нужен
PostgreSQL 16) и не пуста; любой другой путь — `"Top..Science"`, метка с пробелом или нелатинской
буквой, значение, которое не `str` и не список `str`, — даёт `ValidationError` до выполнения запроса.

Равенство, `__not`, `__in`/`__not_in`, `__isnull` и сравнения (`__gt`, ..., `__range` — порядок
обхода дерева в глубину, родитель перед детьми) работают как у любого поля; текстовых операторов нет.
Операторы дерева принимают путь (текстом, списком меток или `F()` другой колонки):

| Оператор | В SQL | Истинно, когда путь колонки |
|---|---|---|
| `__ancestor_of` | `@>` | равен значению или является его предком |
| `__descendant_of` | `<@` | равен значению или является его потомком |
| `__matches` | `~` | подходит под шаблон `lquery` (`str`) |
| `__matches_any` | `?` | подходит под один из шаблонов `lquery` (непустой список `str`) |
| `__matches_text` | `@` | подходит под запрос `ltxtquery` (`str`) |

```python
await Category.objects.filter(path__descendant_of="Top.Science")       # поддерево вместе с корнем
await Category.objects.filter(path__ancestor_of=category.path)        # «хлебные крошки»
await Category.objects.filter(path__matches="*.Astronomy.*")          # любой узел под Astronomy
await Category.objects.filter(path__matches="Top.*{1}")               # дети Top
await Category.objects.filter(path__matches_text="Astro*% & !pictures@")
```

Шаблон `lquery` сопоставляет метки по одной: `*` — любое число меток, `*{1}` — ровно одна, `Astro*` —
метка, начинающаяся с `Astro`, `a|b` — любая из двух, `!a` — любая, кроме `a`; запрос `ltxtquery`
соединяет слова через `&`, `|` и `!`, где `*` — префикс, `%` — слово внутри метки, разделённое `_`, а
`@` — без учёта регистра. Полный синтаксис — в
[документации ltree](https://www.postgresql.org/docs/current/ltree.html).

`path__depth` — число меток (целое) в фильтрах, `values()`, `F()` и `order_by()`:
`filter(path__depth=2)`. `Subpath(field, offset, length=None)` читает часть пути как путь — метки с
позиции `offset` (с 0, отрицательная — с конца), `length` штук или до конца:

```python
from hare.dialects.postgresql.functions import Subpath

await Category.objects.annotate(section=Subpath("path", 1, 1)).values_list("section", flat=True)
```

Только PostgreSQL: на другой базе поле даёт `UnSupportedError`.

## <a id="postgisfield"></a>`PostGISField`

```python
PostGISField()
```
Хранит `geography(Point,4326)` — широту и долготу в WGS84 с точными расчётами расстояний на стороне
базы. Нужно расширение `postgis`; автоматическое создание миграций добавляет
`CreateExtension("postgis")` везде, где используется это поле. Значение в Python — кортеж
**`(широта, долгота)`**. Другие типы геометрий, SRID, плоские колонки, пространственные операторы и
функции — см. [Геоданные (GIS)](../search-and-geodata/gis.ru.md).

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

## <a id="range-fields"></a>Поля диапазонов

**Поля диапазонов** — `RangeField` служит основой (напрямую не используется); значение в Python —
`hare.dialects.postgresql.fields.Range` или, для краткости, простой кортеж `(нижняя, верхняя)`
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

> [!WARNING]
> **Пустой диапазон и диапазон без границ**
>
> PostgreSQL приводит любой диапазон, в который не входит ни одно значение (например, с равными
> границами при полуоткрытом `[)` по умолчанию), к своему значению `'empty'`. При чтении оно даёт
> `lower=None, upper=None` — **то же самое**, что диапазон вообще без границ, если не проверять ещё и
> `is_empty`. Если записать прочитанный диапазон обратно, не сохранив `is_empty`, «ни одного значения»
> молча превратится в «все значения».

Присвоенный диапазон (при создании объекта, в `create()` или присваивании атрибуту) сразу хранится в
памяти в том виде, в каком его возвращает PostgreSQL: границы приводятся к типу элемента поля (строка
с датой становится `date`, граница даты-времени подчиняется правилам `DatetimeField` — см. ниже),
дискретный диапазон (`IntRangeField`/`BigIntRangeField`/`DateRangeField`) переписывается в вид `[)`
(`Range(1, 5, lower_inc=False, upper_inc=True)` становится `Range(2, 6)`), сторона без границы
считается невключённой, а диапазон без значений становится пустым. Нижняя граница больше верхней
даёт `ValidationError`.

Граница `DateTimeRangeField` означает то же, что значение `DatetimeField`: граница без пояса — это
время на часах в настроенном поясе при `use_timezone=True` и в местном поясе системы при `use_timezone=False`, а
читаются границы в настроенном поясе (`use_timezone=True`) или как местное время без пояса (`use_timezone=False`),
поэтому `during__contains=at` согласуется с `DatetimeField`, хранящим то же значение без пояса.

Значение `infinity`/`-infinity` (колонка `DATE`/`TIMESTAMPTZ` или граница диапазона) на обоих
драйверах читается как крайнее значение Python — `date.max`/`date.min`, `datetime.max`/`datetime.min`,
а запись такого значения обратно снова сохраняет `infinity`/`-infinity`, а не конечную дату. Граница
`DateTimeRangeField` в бесконечности при `use_timezone=True` содержит пояс UTC
(`datetime.min.replace(tzinfo=UTC)`), а при `use_timezone=False` — без пояса; граница `datetime.max`/
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

### <a id="range-lookups"></a>Операторы фильтра и пути

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

## <a id="multirange-fields"></a>Поля мультидиапазонов

Мультидиапазон хранит в одной колонке несколько непересекающихся диапазонов — занятые дни
комнаты, часы работы магазина, свободные места в ряду:

```python
from hare.dialects.postgresql.fields import DateMultiRangeField, IntMultiRangeField


class RoomSchedule(Model):
    busy_days = DateMultiRangeField(null=True)  # datemultirange
    free_seats = IntMultiRangeField(default=list)  # int4multirange
```

| Поле | Тип в PostgreSQL | Каждый диапазон — как |
|---|---|---|
| `IntMultiRangeField` | `int4multirange` | `IntRangeField` |
| `BigIntMultiRangeField` | `int8multirange` | `BigIntRangeField` |
| `DecimalMultiRangeField` | `nummultirange` | `DecimalRangeField` |
| `DateMultiRangeField` | `datemultirange` | `DateRangeField` |
| `DateTimeMultiRangeField` | `tstzmultirange` | `DateTimeRangeField` |

Каждое из них — `MultiRangeField` со своими `SQL_TYPE` и `RANGE_FIELD_CLASS` (поле его диапазонов);
мультидиапазон своего поля диапазона наследует его так же.

Значение в Python — список `Range` (каждый можно дать и кортежем `(нижняя, верхняя)`), `[]` — пустой
мультидиапазон. Каждый диапазон принимается так, как его принимает поле диапазона (границы
приводятся к типу, дискретный диапазон становится `[)`, граница `DateTimeMultiRangeField` читается в
настроенном часовом поясе), а список хранится так, как его хранит PostgreSQL: пустые диапазоны
отбрасываются, остальные сортируются по нижней границе, пересекающиеся и касающиеся сливаются в
один — в памяти сразу при присваивании, а не только после чтения:

```python
schedule = RoomSchedule(free_seats=[(8, 9), (1, 3), (3, 5), Range(4, 4)])
schedule.free_seats  # [Range(1, 5), Range(8, 9)]
```

Два диапазона, касающиеся в границе, сливаются, если её включает хотя бы один: `[1, 2)` и `[2, 3)`
становятся `[1, 3)`, а `[1, 2)` и `(2, 3)` остаются раздельными (2 не входит ни в один). Значение, не
являющееся списком диапазонов, — в том числе один `Range` или кортеж `(нижняя, верхняя)` вместо
списка — даёт `ValidationError` с именем поля и позицией неверного диапазона (`busy_days[1]: ...`);
так же — диапазон, который не принимает поле диапазона.

### <a id="multirange-lookups"></a>Операторы фильтра и пути

Операторы поля диапазона ([Операторы фильтра и пути](#range-lookups)) — `exact`, `not`, `isnull`,
`not_isnull`, `contains`, `contained_by`, `overlap`, `fully_lt`, `fully_gt`, `not_lt`, `not_gt`,
`adjacent_to`. Значение — список диапазонов или один `Range`/кортеж `(нижняя, верхняя)`, который
означает мультидиапазон из него одного; `contains` принимает и одно значение типа элемента:

```python
await RoomSchedule.objects.filter(busy_days__contains=date(2024, 1, 11))
await RoomSchedule.objects.filter(busy_days__overlap=[(start, end), (other_start, other_end)])
await RoomSchedule.objects.filter(free_seats__contains=(5, 7))
```

Пути поля диапазона читают мультидиапазон целиком: `startswith`/`endswith` — нижняя граница первого
диапазона и верхняя граница последнего, `isempty`, `lower_inc`, `lower_inf`, `upper_inc`, `upper_inf` —
его признаки; `span` — наименьший диапазон, вмещающий все (`Range`, в SQL — `range_merge()`):
`values_list("busy_days__span")`, `filter(busy_days__startswith__gte=day)`.

`RangeAgg(field)` (`RANGE_AGG` в PostgreSQL) сливает колонку диапазонов каждой группы в
мультидиапазон, который читается через поле мультидиапазона этого поля диапазона — см.
[Функции](functions.ru.md):

```python
await Booking.objects.values("room").annotate(busy=RangeAgg("during"))
# [{"room": 1, "busy": [Range(date(2024, 1, 1), date(2024, 1, 6)), ...]}, ...]
```

Мультидиапазонам нужен PostgreSQL 14 — самая старая версия, которую поддерживает hare; на другой базе
поля дают `UnSupportedError`.
