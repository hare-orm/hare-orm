# PostgreSQL: поля

Всё на этой странице находится в `hare.dialects.postgresql` — собственном пакете диалекта PostgreSQL — и
работает только с подключением к PostgreSQL. Если объявить любое из этих полей в модели, которая работает
с другим диалектом (SQLite, ClickHouse), при создании команд таблицы будет `UnSupportedError`, а не
команды, не имеющие смысла для этой базы. Все поля этой страницы, кроме `ArrayField`, импортируются из
`hare.dialects.postgresql.fields`; `ArrayField` — это `hare.fields.ArrayField`, который работает и в
ClickHouse, — см. [Типы ClickHouse](../clickhouse/types.ru.md#containers):

```python
from hare.dialects.postgresql.fields import (
    CitextField, FixedCharField, HStoreField, InetField, IntervalField, IntRangeField, LtreeField,
    NaiveDatetimeField, NaiveTimeField, NativeEnumField, PostGISField, Range, TSVectorField,
    # ... любое поле этой страницы
)
from hare.fields import ArrayField
```

## <a id="arrayfield"></a>`ArrayField`

```python
class ArrayField:
    def __init__(self, base_field: Field[Any], **kwargs: Any) -> None: ...
```
Настоящий массив PostgreSQL. Тип элемента задаётся настоящим объектом поля, а не строкой с названием
типа:

```python
class Book(Model):
    keywords = ArrayField(base_field=fields.TextField())
```

Элемент `None` хранится как элемент SQL `NULL` при любом `base_field` (`[1, None, 3]` в массиве
`IntField`); каждый другой элемент проходит преобразование и проверку самого `base_field`. Для массива с
элементами `None` объявляйте `base_field` с `null=True`:
[pydantic-схема](../../integrations/pydantic.ru.md) типизирует элемент по нему (`list[int]` или
`list[int | None]`), а ClickHouse хранит элемент `None` только в `Nullable`.

Собственные операторы фильтра: `contains`, `contained_by`, `overlap`, `len`, а также пути к элементу
(`keywords__0`), срезу (`keywords__0_2`) или длине (`keywords__len`) — см.
[Операторы фильтра и пути](#array-lookups).

### <a id="array-lookups"></a>Операторы фильтра и пути

Тоже отдельный набор: `exact`, `not`, `isnull`, `not_isnull`, `contains`, `contained_by`, `overlap`,
`len`, `item`.

```python
await Book.objects.filter(keywords__contains=["sea"])
await Book.objects.filter(keywords__overlap=["sea", "whaling"])
```

`item` сравнивает один элемент по позиции, отсчитываемой с 0 (как в Python); внутри она переводится в
индекс массива PostgreSQL, который отсчитывается с 1:

```python
await Book.objects.filter(keywords__item=(0, "sea"))  # keywords[1] = 'sea'
```

У вложенного массива (`ArrayField(base_field=ArrayField(...))`) `len` считает строки (первое измерение),
а `item` сравнивает строку целиком: `matrix__item=(0, [1, 2])`. Массив PostgreSQL прямоугольный:
подмассивы разной длины на одной глубине (`[[1, 2], [3]]`) или значение рядом с подмассивом
(`[[1, 2], None]`) дают `ValidationError` до отправки. Массив пустых подмассивов (`[[]]`, `[[], []]`)
PostgreSQL сохраняет как пустой массив, и он читается как `[]`; ClickHouse сохраняет его как есть.

Чтобы получить элемент как вычисляемое значение, а не фильтровать по нему, используйте
[`ArrayItem`](functions.ru.md) — позиция тоже отсчитывается с 0:

```python
await Book.objects.annotate(first_keyword=ArrayItem("keywords", 0)).values("first_keyword")
```

Путь через массив читает значение, которое принимает операторы своего типа:

| Часть пути | Что читает | Тип |
|---|---|---|
| `keywords__0` | элемент на позиции, отсчитываемой с 0 (`-1` — последний); `NULL` за пределами массива | тип элемента |
| `keywords__0_2` | элементы с 0 до 2, не включая 2 | тип массива |
| `keywords__len` | число элементов (строк у вложенного массива) — `0` у пустого массива | целое число |

Части пути можно продолжать, в том числе через вложенные массивы (`matrix__1__0`, `matrix__0__len`), и те
же пути работают в `F()`, `values()`/`values_list()`, `order_by()`, `group_by()` и после связи
(`books__keywords__0`):

```python
await Book.objects.filter(keywords__0__startswith="se")
await Book.objects.filter(keywords__len__gte=2)
await Book.objects.filter(keywords__0_2__contains=["whaling"])
await Book.objects.all().order_by("keywords__0").values_list("keywords__0", "keywords__len")
```

## <a id="wall-clock-fields"></a>`NaiveDatetimeField`, `NaiveTimeField`, `IntervalField`, `FixedCharField`

```python
class NaiveDatetimeField:
    def __init__(self, **kwargs: Any) -> None: ...


class NaiveTimeField:
    def __init__(self, **kwargs: Any) -> None: ...


class IntervalField:
    def __init__(self, **kwargs: Any) -> None: ...


class FixedCharField:
    def __init__(self, max_length: int, **kwargs: Any) -> None: ...
```

Типы PostgreSQL, которые не пишут собственные поля hare: `DatetimeField` и `TimeField` хранят момент
(`timestamptz`, `timetz`), `TimeDeltaField` — `bigint` микросекунд, `CharField` — `varchar`.

- **`NaiveDatetimeField`** — `timestamp`: дата и время по часам без часового пояса, хранится как наивный
  `datetime`, что бы ни говорил `use_timezone`. Принимает наивный datetime, дату (её полночь) или строку
  ISO 8601; datetime с поясом называет момент, а не время по часам, и даёт `ValidationError`. У поля те
  же части даты, что у `DatetimeField` (`__year`, `__hour`, ...), его `__date` и `__time`, и оно
  принимает `Extract()`/`Trunc()` и `values("at__date")` — всё читается с часов как есть, без пояса;
  `Trunc(..., "time")` даёт наивный `time`. `F("at") ± timedelta` даёт время по часам, а
  `F("at") - F("at2")` двух таких — `timedelta`; время по часам и момент (`DatetimeField`, `datetime` с
  поясом) не смешиваются в арифметике, в `Coalesce()`, `Case()`, `Greatest()`, `Least()` и `NullIf()`, и
  `update()` не присваивает одно другому (`update(at=F("moment"))`) — `FieldError`: они различаются поясом.
  Наивная константа `datetime` рядом со временем по часам (`Coalesce(F("at"), datetime(2026, 7, 5, 9, 15))`)
  — это то же время по часам, в любом настроенном поясе; константа внутри другого выражения из одних констант
  (`Coalesce(F("at"), Case(When(..., then=datetime(...))))`) там — момент настроенного пояса, и рядом со
  временем по часам она отклоняется.
- **`NaiveTimeField`** — `time`: время суток по часам, хранится как наивный `time`; время с `tzinfo` даёт
  `ValidationError`. У поля есть `__hour`, `__minute`, `__second`, и оно принимает `Extract()` и
  `Trunc()` времени суток.
- **`IntervalField`** — `interval`, хранится как `timedelta`. PostgreSQL хранит месяцы интервала отдельно
  от дней: интервал, записанный через SQL (`'1 month'`), читается как 30 дней на месяц и 365 дней на год.
- **`FixedCharField(max_length)`** — `character(max_length)`: PostgreSQL дополняет текст пробелами до
  `max_length`, поэтому он читается дополненным (`"ab"` из `character(4)` читается как `"ab  "`) и
  сравнивается без хвостовых пробелов (`filter(code="ab")` его находит). Проверяется как `CharField`.

`inspectdb` сопоставляет колонки `timestamp`, `time`, `interval` и `character(n)` с этими полями.

## <a id="citextfield"></a>`CitextField`

```python
class CitextField:
    def __init__(self, **kwargs: Any) -> None: ...
```
Колонка `CITEXT` PostgreSQL — текст без учёта регистра: сравнение, индексы и сортировка работают без
`UPPER()`/`LOWER()` с любой стороны запроса. Нужно расширение `citext`; автоматическое создание миграций
само добавляет `CreateExtension("citext")` везде, где используется это поле, — объявлять
`Meta.extensions` не нужно (см. [Операции миграций —
расширения](../../migrations/operations.ru.md#extensions)).

```python
class Reader(Model):
    email = CitextField()

await Reader.objects.filter(email="Someone@Example.com").first()  # совпадёт и с "someone@example.com"
```

`__contains`/`__startswith`/`__endswith` тоже не учитывают регистр: колонка сравнивается как `citext`, а
не приводится к `VARCHAR`.

Значение с нулевым байтом отклоняет сам PostgreSQL, как и у `CharField`/`TextField` и `JSONField`, —
hare поднимает на это `ValidationError` (при записи, обновлении и в фильтре), а не `OperationalError`.

## <a id="nativeenumfield"></a>`NativeEnumField`

```python
from hare.dialects.postgresql.fields import NativeEnumField

def NativeEnumField(
    enum_type: type[NativeEnumType],
    *,
    type_name: str | None = None,
    type_schema: str | None = None,
    **kwargs: Any,
) -> NativeEnumType: ...
```

Член перечисления в колонке собственного типа `ENUM` PostgreSQL —
`CREATE TYPE loan_status AS ENUM ('lent', 'overdue', 'returned')`. Читается и записывается как
`CharEnumField` (принимается член или его значение, возвращается член), а сама база отклоняет любую
другую метку и сортирует колонку в порядке перечисления, а не по алфавиту.

```python
class LoanStatus(Enum):
    LENT = "lent"
    OVERDUE = "overdue"
    RETURNED = "returned"


class Loan(Model):
    status = NativeEnumField(LoanStatus)
    previous_status = NativeEnumField(LoanStatus, null=True)  # тот же тип


await Loan.objects.filter(status__in=[LoanStatus.LENT, "overdue"]).order_by("status")  # lent, overdue, returned
```

- **Тип.** Его имя — `type_name`, иначе имя класса перечисления в snake case (`LoanStatus` →
  `loan_status`) — идентификатор в нижнем регистре длиной до 63 символов. Метки — значения членов текстом
  (`str(member.value)`, поэтому подходит и `IntEnum`), в порядке перечисления, каждая до 63 байт. Поля,
  называющие один тип в одной схеме, делят его; два таких поля с разными метками дают
  `ConfigurationError`. Тип живёт в схеме `type_schema`, иначе в схеме подключения по умолчанию, какой бы
  ни была `Meta.schema` модели, — поэтому в двух схемах могут быть одноимённые типы со своими метками,
  каждый со своей `type_schema`.
  Перечисление без членов, неверное имя или неверная схема дают `ConfigurationError` при объявлении поля.
- **Схема.** `generate_schemas()` создаёт типы до таблиц (`safe=True` оставляет существующий).
  Автоопределение миграций пишет `CreateEnumType` перед первой колонкой типа, `AlterEnumType`, когда
  меняются члены перечисления, и `DropEnumType` после того, как ушла его последняя колонка, — см.
  [Операции миграций — типы ENUM](../../migrations/operations.ru.md#enum-types). Схема, которую называет
  `type_schema`, создаётся так же, как `Meta.schema` модели (`CreateSchema`). Замена `CharEnumField` на
  `NativeEnumField` или перенос колонки на другой тип (другие `type_name` или `type_schema`) преобразует её
  на месте через текст (`ALTER COLUMN ... TYPE ... USING`).
- **Изменение членов.** Новые члены, добавленные так, что старые сохраняют порядок, становятся новыми
  метками на месте (`ALTER TYPE ... ADD VALUE`) — добавленное так значение нельзя использовать в той же
  транзакции, поэтому записывайте такие строки в следующей миграции. Удалённый или переставленный член
  заменяет тип: колонки этого типа (и колонки-массивы) и их значения по умолчанию преобразуются в новый
  тип одной командой, поэтому ни одна строка не должна хранить удалённую метку — сначала обновите такие
  строки: миграция, нашедшая такую строку, бросает `FieldNarrowingDataLossError` с именем таблицы и
  колонки, ничего не изменив.
- **Фильтры** — общий набор: равенство, `__in`, `__isnull` и сравнения в порядке перечисления. Метка вне
  перечисления даёт `ValidationError` до выполнения запроса.
- Только PostgreSQL (`features.supports_enum_types`); на другой базе поле даёт `UnSupportedError`.

## <a id="network-fields"></a>Сетевые поля: `InetField`, `CidrField`, `MacAddressField`

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
`__range`, в порядке базы); текстовых операторов у них нет. `InetField` и `CidrField` фильтруются ещё и
по подсетям — значение: адрес или сеть, текстом или объектом `ipaddress`, либо `F()` другой колонки:

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
class HStoreField:
    def __init__(self, **kwargs: Any) -> None: ...
```
Колонка `hstore` PostgreSQL — плоский набор пар «строковый ключ — строка или `None`», в Python это
`dict`. Нужно расширение `hstore`; автоматическое создание миграций добавляет `CreateExtension("hstore")`
везде, где используется это поле. Ключ или значение, которые не являются строкой, записываются как их
`str()` (`{"n": 5}` читается как `{"n": "5"}`); значение, которое не является `dict`, или нулевой байт
дают `ValidationError`.

```python
class Edition(Model):
    attributes = HStoreField(null=True)

await Edition.objects.filter(attributes__binding="hardcover")
await Edition.objects.filter(attributes__has_keys=["binding", "format"])
await Edition.objects.all().values_list("attributes__binding", "attributes__keys")
```

Операторы фильтра и пути — см. [Операторы фильтра и пути](#hstore-lookups).

### <a id="hstore-lookups"></a>Операторы фильтра и пути

`exact`, `not`, `in`, `not_in`, `isnull`, `not_isnull` сравнивают всё значение; `contains`/
`contained_by` принимают `dict` пар (`@>`/`<@`); `has_key` принимает ключ, `has_keys`/`has_any_keys` —
список ключей (`?`/`?&`/`?|`).

Любое другое имя после поля — это ключ: `attributes__binding` — его значение (текст, `None`, если ключа
нет), оно принимает текстовые операторы (`attributes__binding__startswith="hard"`). `attributes__keys` и
`attributes__values` — все ключи/значения в виде текстового массива, с операторами и путями `ArrayField`
(`attributes__keys__contains=["format"]`, `attributes__keys__len=3`). Сегмент с именем любого оператора
(`in`, `contains`, `gt`, ...) — это оператор, а не ключ: `attributes__in=[...]` сравнивает всё значение, а
`attributes__gt=...` даёт `FieldError`, как на любом поле без `__gt`; такой ключ доступен через
`contains`/`has_key`. Те же пути работают в `F()`,
`values()`, `order_by()` и после связи.

```python
await Edition.objects.filter(attributes__contains={"binding": "hardcover"})
await Edition.objects.filter(attributes__format__in=["A5", "B5"])
await Edition.objects.filter(attributes__keys__overlap=["format", "weight"])
```

## <a id="tsvectorfield"></a>`TSVectorField`

```python
class TSVectorField:
    def __init__(
        self,
        source_fields: Sequence[str] | str | None = None,
        config: str | None = None,
        weights: Sequence[str] | None = None,
        stored: bool = True,
        **kwargs: Any,
    ) -> None: ...
```
При `stored=True` и заданных `source_fields` создаётся как `GENERATED ALWAYS AS (...) STORED` — значение
поддерживает сам PostgreSQL, код приложения для этого не нужен:

```python
class Book(Model):
    title = fields.CharField(max_length=300)
    description = fields.TextField()
    search_vector = TSVectorField(source_fields=("title", "description"), config="english")
```

Миграция, меняющая тип колонки поля-источника, удаляет колонку вектора перед изменением и добавляет её
после — PostgreSQL не меняет колонку, которую читает генерируемая; векторы вычисляются заново.

## <a id="ltreefield"></a>`LtreeField`

Колонка `ltree` — место узла в дереве в виде пути из меток от корня (`Top.Science.Astronomy`), в Python —
`str`. Одна колонка отвечает на вопросы «все потомки», «все предки» и на поиск по шаблону одним условием
с индексом, без рекурсивного запроса. Нужно расширение `ltree`; автодетектор миграций добавляет
`CreateExtension("ltree")` везде, где используется поле.

```python
from hare.dialects.postgresql.fields import LtreeField
from hare.dialects.postgresql.indexes import GistIndex


class Category(Model):
    path = LtreeField(unique=True)

    class Meta:
        indexes = (GistIndex(fields=("path",)),)  # обслуживает операторы дерева и шаблонов
```

Путь записывается текстом или списком/кортежем меток (`["Top", "Science"]` — это `"Top.Science"`, `[]` и
`""` — пустой путь), читается текстом. Метка состоит из букв, цифр, `_` и `-` (дефису нужен
PostgreSQL 16) и не пуста; любой другой путь — `"Top..Science"`, метка с пробелом или нелатинской буквой,
значение, которое не `str` и не список `str`, — даёт `ValidationError` до выполнения запроса.

Равенство, `__not`, `__in`/`__not_in`, `__isnull` и сравнения (`__gt`, ..., `__range` — порядок обхода
дерева в глубину, родитель перед детьми) работают как у любого поля; текстовых операторов нет. Операторы
дерева принимают путь (текстом, списком меток или `F()` другой колонки):

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
соединяет слова через `&`, `|` и `!`, где `*` — префикс, `%` — слово внутри метки, разделённое `_`, а `@`
— без учёта регистра. Полный синтаксис — в [документации
ltree](https://www.postgresql.org/docs/current/ltree.html).

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
class PostGISField:
    def __init__(self, **kwargs: Any) -> None: ...
```
Хранит `geography(Point,4326)` — широту и долготу в WGS84 с точными расчётами расстояний на стороне базы.
Нужно расширение `postgis`; автоматическое создание миграций добавляет `CreateExtension("postgis")`
везде, где используется это поле. Значение в Python — кортеж **`(latitude, longitude)`**. Другие типы
геометрий, SRID, плоские колонки, пространственные операторы и функции — см.
[Геоданные (GIS)](../search-and-geodata/gis.ru.md).

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

Радиус — конечное число не меньше нуля (`int`, `float` или `Decimal`), точка проверяется так же, как
записываемое значение. Иначе — `ValidationError` до отправки запроса.

Для сортировки и фильтров по расстоянию сложнее простого радиуса используйте вычисляемые значения
`STDistance`/`STDWithin` (см. [Функции PostgreSQL](functions.ru.md)).

## <a id="range-fields"></a>Поля диапазонов

**Поля диапазонов** — `RangeField` служит основой (напрямую не используется); значение в Python —
`hare.dialects.postgresql.fields.Range` или, для краткости, простой кортеж `(lower, upper)` (полуоткрытый
диапазон `[)`), или словарь его атрибутов (`{"lower": 1, "upper": 10}`) — форма, в которой его пишет
pydantic-схема модели (она проверяет границы как тип колонки), поэтому `create(**schema.model_dump())` его
принимает:

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
> PostgreSQL приводит любой диапазон, в который не входит ни одно значение (например, с равными границами
> при полуоткрытом `[)` по умолчанию), к своему значению `'empty'`. При чтении оно даёт
> `lower=None, upper=None` — **то же самое**, что диапазон вообще без границ, если не проверять ещё и
> `is_empty`. Если записать прочитанный диапазон обратно, не сохранив `is_empty`, «ни одного значения»
> молча превратится в «все значения».

Присвоенный диапазон (при создании объекта, в `create()` или присваивании атрибуту) сразу хранится в
памяти в том виде, в каком его возвращает PostgreSQL: границы приводятся к типу элемента поля (строка с
датой становится `date`, граница даты-времени подчиняется правилам `DatetimeField` — см. ниже),
дискретный диапазон (`IntRangeField`/`BigIntRangeField`/`DateRangeField`) переписывается в вид `[)`
(`Range(1, 5, lower_inc=False, upper_inc=True)` становится `Range(2, 6)`), сторона без границы считается
невключённой, а диапазон без значений становится пустым. Нижняя граница больше верхней даёт
`ValidationError`, как и граница целочисленного диапазона вне его типа или с дробной частью
(`Range(1.9, 3)`, `span__contains=1.5`) или типа `bool` — так же её отклоняет `IntField`, — и граница NaN
у `DecimalRangeField`, которая не упорядочивается ни с какой другой.

Миграция, которая меняет поле диапазона на диапазон другого типа (`IntRangeField` на `BigIntRangeField`
или `DecimalRangeField`, так же мультидиапазон или массив диапазонов), преобразует колонку через текст
каждого значения (`USING span::text::int8range`): PostgreSQL не приводит один тип диапазона к другому.
Граница, которую новый тип не вмещает, останавливает миграцию ошибкой PostgreSQL.

Граница `DateTimeRangeField` означает то же, что значение `DatetimeField`: граница без пояса — это время
на часах в настроенном поясе при `use_timezone=True` и в местном поясе системы при `use_timezone=False`,
а читаются границы в настроенном поясе (`use_timezone=True`) или как местное время без пояса
(`use_timezone=False`), поэтому `during__contains=at` согласуется с `DatetimeField`, хранящим то же
значение без пояса.

Значение `infinity`/`-infinity` (колонка `DATE`/`TIMESTAMPTZ` или граница диапазона) на обоих драйверах
читается как крайнее значение Python — `date.max`/`date.min`, `datetime.max`/`datetime.min`, а запись
такого значения обратно сохраняет конечный день или момент, которым оно является (см. [Крайние
даты](../../models/field-types.ru.md#extreme-dates)). Граница
`DateTimeRangeField`, прочитанная в бесконечности, при `use_timezone=True` содержит пояс UTC
(`datetime.min.replace(tzinfo=UTC)`), а при `use_timezone=False` — без пояса; граница `datetime.max`/
`datetime.min` без пояса считается тем же моментом UTC, каким бы ни был настроенный пояс.

| Поле | Тип PostgreSQL |
|---|---|
| `IntRangeField` | `int4range` |
| `BigIntRangeField` | `int8range` |
| `DecimalRangeField` | `numrange` |
| `DateRangeField` | `daterange` |
| `DateTimeRangeField` | `tstzrange` |

Операторы фильтра (`contains`, `overlap`, `fully_lt`, `adjacent_to`, ...) и пути к границе
(`during__startswith`) или признаку (`during__isempty`) — см. [Операторы фильтра и пути](#range-lookups).

Поля диапазонов хорошо сочетаются с `ExclusionConstraint` для расписаний без пересечений — см.
[Ограничения](../../models/constraints-and-triggers.ru.md#constraints).

### <a id="range-lookups"></a>Операторы фильтра и пути

`exact`, `not`, `in`, `not_in`, `isnull`, `not_isnull`, а также операторы диапазонов PostgreSQL — каждый
принимает `Range` (или кортеж `(lower, upper)`), а `contains` ещё и одно значение:

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
`isempty`, `lower_inc`, `lower_inf`, `upper_inc`, `upper_inf` — логические значения. Они так же работают
в `F()`, `values()`, `order_by()` и после связи:

```python
await Booking.objects.filter(during__fully_lt=Range(start, end))
await Booking.objects.filter(during__startswith__year=2024)
await Booking.objects.filter(during__upper_inf=True)
await Booking.objects.all().order_by("during__startswith").values("id", "during__endswith")
```

## <a id="multirange-fields"></a>Поля мультидиапазонов

Мультидиапазон хранит в одной колонке несколько непересекающихся диапазонов — занятые дни комнаты, часы
работы библиотеки, свободные места в ряду:

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

Значение в Python — список `Range` (каждый можно дать и кортежем `(lower, upper)`, и словарём его
атрибутов, как его пишет схема модели), `[]` — пустой мультидиапазон. Каждый диапазон принимается так,
как его принимает поле диапазона (границы приводятся к типу, дискретный диапазон становится `[)`, граница
`DateTimeMultiRangeField` читается в настроенном часовом поясе), а список хранится так, как его хранит
PostgreSQL: пустые диапазоны отбрасываются, остальные сортируются по нижней границе, пересекающиеся и
касающиеся сливаются в один — в памяти сразу при присваивании, а не только после чтения:

```python
schedule = RoomSchedule(free_seats=[(8, 9), (1, 3), (3, 5), Range(4, 4)])
schedule.free_seats  # [Range(1, 5), Range(8, 9)]
```

Два диапазона, касающиеся в границе, сливаются, если её включает хотя бы один: `[1, 2)` и `[2, 3)`
становятся `[1, 3)`, а `[1, 2)` и `(2, 3)` остаются раздельными (2 не входит ни в один). Значение, не
являющееся списком диапазонов, — в том числе один `Range` или кортеж `(lower, upper)` вместо списка —
даёт `ValidationError` с именем поля и позицией неверного диапазона (`busy_days[1]: ...`); так же —
диапазон, который не принимает поле диапазона.

### <a id="multirange-lookups"></a>Операторы фильтра и пути

Операторы поля диапазона ([Операторы фильтра и пути](#range-lookups)) — `exact`, `not`, `isnull`,
`not_isnull`, `contains`, `contained_by`, `overlap`, `fully_lt`, `fully_gt`, `not_lt`, `not_gt`,
`adjacent_to`. Значение — список диапазонов или один `Range`/кортеж `(lower, upper)`, который означает
мультидиапазон из него одного; `contains` принимает и одно значение типа элемента:

```python
await RoomSchedule.objects.filter(busy_days__contains=date(2024, 1, 11))
await RoomSchedule.objects.filter(busy_days__overlap=[(start, end), (other_start, other_end)])
await RoomSchedule.objects.filter(free_seats__contains=(5, 7))
```

Пути поля диапазона читают мультидиапазон целиком: `startswith`/`endswith` — нижняя граница первого
диапазона и верхняя граница последнего, `isempty`, `lower_inc`, `lower_inf`, `upper_inc`, `upper_inf` —
его признаки; `span` — наименьший диапазон, вмещающий все (`Range`, в SQL — `range_merge()`):
`values_list("busy_days__span")`, `filter(busy_days__startswith__gte=day)`.

`RangeAgg(field)` (`RANGE_AGG` в PostgreSQL) сливает колонку диапазонов каждой группы в мультидиапазон,
который читается через поле мультидиапазона этого поля диапазона — см. [Функции](functions.ru.md):

```python
await Booking.objects.values("room").annotate(busy=RangeAgg("during"))
# [{"room": 1, "busy": [Range(date(2024, 1, 1), date(2024, 1, 6)), ...]}, ...]
```

Мультидиапазонам нужен PostgreSQL 14 — самая старая версия, которую поддерживает hare; на другой базе
поля дают `UnSupportedError`.

## <a id="postgresql-validators"></a>Проверки значений

`hare.dialects.postgresql.validators`, для `validators=[...]` поля:

| Проверка | Что проверяет |
|---|---|
| `KeysValidator(keys, strict=False, message=None)` | в словаре `HStoreField` есть каждый ключ из `keys` — а при `strict=True` нет никаких других |
| `RangeMinValueValidator(limit_value, message=None)` | нижняя граница диапазона не меньше `limit_value` (сторона без границы не проходит) |
| `RangeMaxValueValidator(limit_value, message=None)` | верхняя граница диапазона не больше `limit_value` (сторона без границы не проходит) |

Пустой диапазон проходит обе проверки диапазона. Число элементов `ArrayField` проверяют
`MaxLengthValidator`/`MinLengthValidator`, которые принимают списки.

```python
class Edition(Model):
    attributes = HStoreField(validators=[KeysValidator(["binding", "format"])])
    reader_ages = IntRangeField(validators=[RangeMinValueValidator(0), RangeMaxValueValidator(100)])
```
