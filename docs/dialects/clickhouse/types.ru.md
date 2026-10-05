# Типы ClickHouse

У каждого поля hare есть тип колонки ClickHouse; `hare.dialects.clickhouse.fields` добавляет типы,
которые есть только в ClickHouse, а контейнеры из `hare.fields` держат любые поля — и друг друга — на
любой глубине. `hare inspectdb` записывает каждый из них обратно своим полем, а `hare drift`
сравнивает их целыми деревьями.

## <a id="fields"></a>Поля и их колонки

| Поле | Колонка |
|---|---|
| `SmallIntField`, `IntField`, `BigIntField` | `Int16`, `Int32`, `Int64` |
| `UInt8Field`, `UInt16Field`, `UInt32Field`, `UInt64Field`, `UInt128Field`, `UInt256Field`, `Int128Field`, `Int256Field` | `UInt8` ... `Int256` — `hare.dialects.clickhouse.fields`, каждое — `ClickhouseIntegerField`, принимающее только значения своего типа |
| `FloatField`, `Float32Field` | `Float64`, `Float32` |
| `DecimalField(max_digits, decimal_places)` | `Decimal(P, S)` |
| `BooleanField` | `Bool` |
| `CharField`, `TextField` | `String` |
| `FixedStringField(length)` | `FixedString(length)` |
| `DatetimeField` | `DateTime64(6, 'UTC')` — микросекунды, хранится в UTC |
| `DateField` | `Date32` |
| `TimeField` | `String` — время суток в ISO |
| `TimeDeltaField` | `Int64` — микросекунды |
| `UUIDField` | `UUID` |
| `BinaryField` | `String` |
| `JSONField` | `JSON` с ClickHouse 25.3, `String` до него — см. [JSON](differences.ru.md#json) |
| `CharEnumField`, `IntEnumField` | `Enum8`/`Enum16` — см. [Перечисления](#enums) |
| `IPAddressField`, `IPv4AddressField` | `IPv6` (адрес IPv4 отображается в него и читается обратно как IPv4), `IPv4` |
| `ArrayField`, `MapField`, `TupleField`, `NestedField` | `Array`, `Map`, `Tuple`, `Array(Tuple(...))` — см. [Контейнеры](#containers) |
| `LowCardinalityField(field)` | `LowCardinality(T)` |
| `VariantField([...])`, `DynamicField()` | `Variant(...)`, `Dynamic` |
| `GeometryField` | `Point`, `LineString`, `Polygon`, `MultiLineString`, `MultiPolygon` — см. [Гео](#geo) |

`VariantField` и `DynamicField` требуют ClickHouse 25.3, где эти типы уже не экспериментальные, а
линейные геометрии — сервера новее 24.3: подключение читает, какие из этих типов есть у сервера, и
таблица или колонка с отсутствующим типом даёт `UnSupportedError` до DDL.

Колонка поля с `null=True` — `Nullable(...)`, но только у листа: массив, `Map` и кортеж никогда не
бывают `Nullable` (см. [Контейнеры](#containers)). Целая колонка при переполнении «заворачивается»,
поэтому hare сам проверяет диапазоны целых и десятичных значений до записи; беззнаковое 64-битное
значение выше 2⁶³ и 128- и 256-битные целые — это `int` Python.

- **`Float32Field`** читает значение как самую короткую десятичную запись того же float32 — `0.1`, а
  не `0.10000000149011612`, которое отдают драйверы; значение вне диапазона отклоняется.
- **`FixedStringField(length)`** хранит текст не длиннее `length` байт в UTF-8, дополненный нулевыми
  байтами, и читает его без них; текст, который сам заканчивается нулевым байтом, даёт
  `ValidationError` — его прочли бы без этого байта.

## <a id="enums"></a>Перечисления

`CharEnumField` и `IntEnumField` — это `Enum8`/`Enum16` из меток и чисел. ClickHouse хранит числа, а
метки читает из типа, поэтому метка сохраняет своё число навсегда:

- метки `IntEnumField` — имена членов, пронумерованные их значениями;
- метки `CharEnumField` — хранимые значения, пронумерованные по контрольной сумме самой метки:
  добавление, удаление или перестановка члена не меняет числа других меток.

Миграция меняет члены через `MODIFY COLUMN`. Перед удалением члена она проверяет, что ни одна строка
его не хранит, — сервер отказывается удалять используемую метку.

## <a id="containers"></a>Контейнеры

```python
from hare import fields


class Shipment(Model):
    tags = fields.ArrayField(fields.CharField(max_length=20))
    prices = fields.MapField(fields.CharField(max_length=3), fields.DecimalField(max_digits=10, decimal_places=2))
    point = fields.TupleField({"lon": fields.FloatField(), "lat": fields.FloatField()})
    items = fields.NestedField({"sku": fields.CharField(max_length=20), "quantity": fields.IntField()})
    history = fields.ArrayField(
        fields.MapField(fields.CharField(max_length=10), fields.ArrayField(fields.IntField(null=True)))
    )
```

| Поле | Значение | Пути | Операторы |
|---|---|---|---|
| `ArrayField(field)` | список | `tags__0` — элемент (с 0, отрицательный — с конца), `tags__0_2` — срез, `tags__len` | `__contains`, `__contained_by`, `__overlap`, `__len`, `__item=(индекс, значение)` |
| `MapField(key_field, value_field)` | словарь | `prices__eur` — значение по ключу (для отсутствующего ключа — значение типа по умолчанию), `prices__keys`, `prices__values`, `prices__len` | `__has_key`, `__has_keys`, `__has_any_keys`, `__len` |
| `TupleField([...])` / `TupleField({...})` | кортеж / словарь по именам | `point__0`, `point__lat` | операторы элемента |
| `NestedField({...})` | список словарей | `items__0` — строка, `items__sku` — колонка всех строк, `items__len` | операторы массива |

Каждое значение внутри контейнера пишется и читается полем, которое его держит, — десятичное число,
момент, UUID, перечисление на любой глубине, — а путь любой длины проходит сквозь них:
`history__0__open__1__gt=3` — второе число под ключом `open` первого словаря. Оператор в конце пути —
оператор поля на этом месте.

- **`NULL`.** Массив, `Map` и кортеж в ClickHouse никогда не бывают `Nullable`: `null=True` у
  контейнера пишет `None` как пустое значение, которое и читается пустым. `null=True` у поля внутри
  делает `Nullable` этот элемент.
- **Ключи `Map`** — целые, тексты, `FixedString`, UUID, даты, моменты и перечисления (и их
  `LowCardinality`) без `NULL`; другое поле ключа даёт `ConfigurationError` при объявлении поля.
- **Ошибки** называют путь до значения, о котором они: `tags[2]`, `prices['eur']`, `point.1`.
- **Миграция** сравнивает дерево поля целиком и меняет любой уровень через `MODIFY COLUMN`; сужение
  на любом уровне сначала проверяется по данным.

`ArrayField` работает и в PostgreSQL — вложенный `ArrayField(ArrayField(...))` там многомерный
массив; `MapField`, `TupleField` и `NestedField` есть только в ClickHouse.

## <a id="low-cardinality"></a>`LowCardinalityField`

`LowCardinalityField(fields.CharField(max_length=2))` хранит значения словарём различных — для
колонки, где их немного (страна, статус). Значения, операторы и пути — те же, что у обёрнутого поля;
`null=True` у него — это `LowCardinality(Nullable(T))`. Контейнер он не оборачивает.

## <a id="variant-and-dynamic"></a>`VariantField` и `DynamicField`

```python
from hare.dialects.clickhouse.fields import DynamicField, VariantField

reading = VariantField([fields.IntField(), fields.CharField(max_length=20)], null=True)
value = DynamicField(null=True)
```

`VariantField` хранит значение одного из типов своих полей. Значение берёт первое поле, чей тип у
него в точности, иначе первое, экземпляром типа которого оно является, иначе первое, которое его
преобразует; прочитанное значение возвращает поле его типа ClickHouse — поэтому давайте поля,
которые читаются разными типами Python. Фильтр находит только значения поля, к которому относится
значение фильтра, а `reading__0__gt=3` читает значения поля на этой позиции, `NULL` для остальных.

`DynamicField` хранит значение любого типа — с тем типом, который у его значения в Python (целое как
`Int64`, текст как `String`, список как `Array`), — и читает его таким, каким записал. `max_types`
ограничивает число типов, которые хранятся в собственных колонках. Фильтр находит только значения
типа значения фильтра (`value=5` не находит `"5"`); путь с именем типа читает значения этого типа —
`value__String__startswith="a"`, `value__Int64__gt=3`.

ClickHouse не сортирует строки по колонке `Variant` или `Dynamic` — сортируйте по её пути. Колонка
сама хранит `NULL`: её поля не принимают `null=True`.

## <a id="geo"></a>Гео

`GeometryField` из `hare.gis` — колонка `Point`, `LineString`, `Polygon`, `MultiLineString` или
`MultiPolygon` только с x и y и без SRID — прочитанное значение получает SRID поля. Точка — кортеж, у
которого нет пустого значения, поэтому поле точки с `null=True` даёт `UnSupportedError`; линия или
область, записанная как `None`, хранится пустой. Кольца многоугольника хранятся в той ориентации, в
какой их читают функции ClickHouse, — внешнее кольцо по часовой стрелке, — поэтому многоугольник,
заданный наоборот, читается с развёрнутыми кольцами.

| `hare.gis` | ClickHouse |
|---|---|
| Точка в области (`__intersects`, `__within`, `__contains`, `__covers`, `__covered_by`, `__disjoint`) | `pointInPolygon` — точка на границе считается внутри |
| Отношения двух областей | `polygonsIntersect*`, `polygonsWithin*`, `polygonsEquals*` |
| `Area`, `Perimeter`, `ConvexHull`, `Intersection`, `Union`, `SymmetricDifference` областей | `polygonArea*`, `polygonPerimeter*`, `polygonConvexHullCartesian`, `polygons*` |
| `Distance` точек | `L2Distance`; `geoDistance` в метрах для географии |
| `__dwithin` | расстояние, сравнённое с заданным |
| `Distance` областей | `polygonsDistance*` |
| `AsText` | `wkt` |

География (`GeometryField(geography=True)`) измеряется на сфере среднего радиуса WGS 84 функциями
`*Spherical` — в метрах и квадратных метрах, на больших расстояниях немного иначе, чем на эллипсоиде
PostGIS; функция, которая есть у ClickHouse только на плоскости (`ConvexHull`, `polygonsEquals`), для
неё даёт `UnSupportedError` — как и всё, для чего у ClickHouse нет функции.
