# Геоданные (GIS)

`hare.gis` хранит точки, линии и области в пространственных колонках, отбирает строки по тому, как их
геометрии соотносятся с другими, и считает площади, расстояния и новые геометрии в базе. На PostgreSQL
он работает на [PostGIS](https://postgis.net/); автодетектор миграций добавляет
`CreateExtension("postgis")` везде, где используется геометрическое поле. На SQLite он работает на
[SpatiaLite](https://www.gaia-gis.it/fossil/libspatialite/) через тот же API — см.
[SQLite: SpatiaLite](#spatialite). На ClickHouse геометрическое поле — один из его геотипов (точки,
линии и области только с x и y и без SRID), и там работают операторы и функции, для которых у ClickHouse
есть функция; см. [ClickHouse: геоданные](../clickhouse/types.ru.md#geo). На базе без пространственных типов геометрическое поле даёт
`UnSupportedError`, когда у него спрашивают тип колонки, а каждый пространственный оператор, функция и
агрегат — пока строится запрос, до отправки SQL (`Features.supports_spatial`).

```python
from hare import Model, fields
from hare.dialects.postgresql.indexes import GistIndex
from hare.gis import Point, PointField, PolygonField


class Shop(Model):
    name = fields.CharField(max_length=100)
    location = PointField()                    # geometry(Point,4326)
    delivery_area = PolygonField(null=True)    # geometry(Polygon,4326)

    class Meta:
        indexes = (GistIndex(fields=("location",)), GistIndex(fields=("delivery_area",)))


await Shop.objects.create(name="Corner", location=Point(37.6173, 55.7558))
nearby = await Shop.objects.filter(location__dwithin=(Point(37.62, 55.75), 0.01))
```

Пространственные операторы используют индекс, только если он у колонки есть, — объявите `GistIndex`
(PostgreSQL) или [`SpatialiteIndex`](#spatial-index) (SQLite) для каждой геометрической колонки, по
которой фильтруете. Каждый из них — индекс своей базы: на другой он даёт `UnSupportedError` до отправки
SQL — и при генерации схемы, и в миграции, которая его добавляет или удаляет.

## <a id="geometries"></a>Значения-геометрии

Геометрическое поле хранит `hare.gis.Geometry` — неизменяемое значение, которое сравнивается по типу,
SRID и координатам:

| Класс | Пример |
|---|---|
| `Point(x=None, y=None, z=None, *, srid=None)` | `Point(37.6173, 55.7558)` — для долготы/широты x — долгота; `Point()` — пустая точка |
| `LineString(positions=(), *, srid=None)` | `LineString([(0, 0), (1, 1), (2, 0)])` — две позиции и больше |
| `Polygon(exterior=(), holes=(), *, srid=None)` | `Polygon([(0, 0), (4, 0), (4, 4), (0, 4), (0, 0)], holes=[[(1, 1), (2, 1), (2, 2), (1, 1)]])` — каждое кольцо замкнуто, четыре позиции и больше |
| `MultiPoint(members=(), *, srid=None)` | `MultiPoint([(0, 0), Point(1, 1)])` |
| `MultiLineString(members=(), *, srid=None)` | `MultiLineString([[(0, 0), (1, 1)], [(2, 2), (3, 3)]])` |
| `MultiPolygon(members=(), *, srid=None)` | `MultiPolygon([polygon, [exterior_ring, hole_ring]])` |
| `GeometryCollection(members=(), *, srid=None)` | `GeometryCollection([Point(0, 0), LineString([(0, 0), (1, 1)])])` |

Позиция — `Point` или последовательность из двух-трёх конечных чисел; у позиций одной геометрии одно
число координат. Член мульти-геометрии — геометрия её типа или её координаты; его собственный SRID
отбрасывается — действует SRID коллекции. Каждый класс проверяет аргументы: неверная форма, бесконечное
число, незамкнутое кольцо, член другого типа дают `ValidationError`. Координаты M не поддерживаются.

У каждой геометрии есть:

- `wkt` — её well-known text без SRID (`POINT Z (1 2 3)`), `ewkt` — с ним (`SRID=4326;POINT (1 2)`);
- `__geo_interface__` — объект геометрии GeoJSON (`{"type": "Point", "coordinates": [1.0, 2.0]}`),
  который читают shapely (`shapely.geometry.shape(point)`) и другие библиотеки;
- `srid`, `is_empty`, `has_z`, `get_coordinates()` (вложенные кортежи, как их вкладывает GeoJSON) и
  координаты своего класса — `point.x`, `polygon.exterior`, `polygon.holes`, `collection.members`;
- `with_srid(srid)` — копия с другим SRID (координаты не преобразуются);
- `from_coordinates(coordinates, srid=None)` — геометрия из координат в духе GeoJSON.

`hare.gis.readers` читает другие формы: `WktReader.read(text)` (WKT или EWKT), `EwkbReader.read(data)`
(байты (E)WKB или их hex-текст — расширенная форма PostGIS с SRID и ISO WKB) и
`GeoJsonReader.read(value)` (словарь GeoJSON или любой объект с `__geo_interface__`). Каждый даёт
`ValidationError` для значения, которое не является целой геометрией.

## <a id="fields"></a>Поля

```python
GeometryField(geometry_type=None, *, srid=4326, geography=False, dimensions=2, **kwargs)
```

| Аргумент | Значение |
|---|---|
| `geometry_type` | Вид геометрии в колонке — `GeometryType` (`POINT`, `POLYGON`, ...) или его имя; любой (`GEOMETRY`), если не задан |
| `srid` | Система координат, `int` от 1 до 999999 — по умолчанию 4326 (WGS 84 долгота/широта) |
| `geography` | `True` — колонка `geography`: долгота/широта на сфероиде, расстояния в метрах; `False` — плоская `geometry` |
| `dimensions` | 2 для x/y, 3 для x/y/z |

Неверный аргумент даёт `ConfigurationError` при объявлении модели. `PointField`, `LineStringField`,
`PolygonField`, `MultiPointField`, `MultiLineStringField`, `MultiPolygonField` и
`GeometryCollectionField` — это `GeometryField` одного вида. На PostgreSQL колонка —
`geometry(<Тип>[Z],<srid>)` или `geography(<Тип>[Z],<srid>)`.

Поле принимает значение в любой из этих форм — при записи, присваивании и в фильтре:

- `Geometry`;
- текст WKT или EWKT — `"POINT(1 2)"`, `"SRID=4326;POINT(1 2)"`;
- байты (E)WKB или их hex-текст;
- словарь геометрии GeoJSON или любой объект с `__geo_interface__` (геометрия shapely).

Геометрия без SRID получает SRID поля, поэтому `Point(1, 2)`, присвоенная `PointField()`, хранится как
`Point(1, 2, srid=4326)` — в памяти то же, что вернёт чтение. Записываемая геометрия должна быть вида
поля, с его числом координат и его SRID; иначе `ValidationError` до выполнения запроса. Геометрия в
другом SRID при записи не преобразуется — преобразуйте её заранее или храните в колонке её SRID.
Значения читаются объектами `Geometry` со своим SRID.

### <a id="geography"></a>geometry или geography

Колонка `geometry` считает на плоскости своего SRID: на 4326 площадь — в квадратных градусах,
расстояние — в градусах. Колонка `geography` (`geography=True`) считает на сфероиде: площади, длины и
расстояния — в квадратных метрах и метрах, для любых двух точек Земли. На geography PostGIS проверяет
только часть отношений — `intersects`, `covers`, `covered_by`, `dwithin`, `bbox_overlaps` и операторы
расстояния; любой другой оператор отношения на geography даёт `UnSupportedError`, как и
пространственные агрегаты. Функции без формы для geography (`Envelope`, `NumPoints`, пути `x`/`y`, ...)
читают долготу/широту geography как geometry. Плоский SRID в метрах (например, 3857 или местная
проекция) — альтернатива, когда все точки в одном регионе.

## <a id="lookups"></a>Операторы фильтра

У геометрического поля остаются равенство (`location=...`, `__not`) и `__isnull`/`__not_isnull`;
сравнений и текстовых операторов нет. Пространственные операторы принимают геометрию в любой форме,
которую берёт поле, или `F()` другой геометрической колонки:

| Оператор | Истинно, когда геометрия колонки |
|---|---|
| `__intersects` | имеет общую точку со значением |
| `__contains` | содержит значение, не считая его границы (точка на краю не содержится) |
| `__contains_properly` | содержит значение, не касаясь его границы |
| `__within` | лежит внутри значения |
| `__covers` | содержит значение вместе с границей |
| `__covered_by` | лежит внутри значения или на его границе |
| `__crosses` | пересекает его (линия через полигон, две линии в одной точке) |
| `__disjoint` | не имеет с ним общих точек |
| `__equals` | — то же множество точек (кольцо может начинаться с другой вершины) |
| `__overlaps` | частично перекрывает его, у каждого есть часть вне другого (одна размерность) |
| `__touches` | касается его только границами |
| `__bbox_overlaps` | имеет ограничивающий прямоугольник, пересекающий прямоугольник значения |
| `__bbox_contains` | имеет ограничивающий прямоугольник, вмещающий прямоугольник значения |
| `__bbox_contained` | имеет ограничивающий прямоугольник внутри прямоугольника значения |
| `__relate=(geometry, pattern)` | подходит под шаблон DE-9IM — девять из `0`, `1`, `2`, `T`, `F`, `*` |
| `__isvalid=True/False` | (не) корректна — замкнутые кольца, нет самопересечений, ... |

```python
await Shop.objects.filter(delivery_area__contains=customer_point)
await Shop.objects.filter(location__within=F("district__boundary"))
await Parcel.objects.filter(shape__relate=(road, "T********"))
```

Операторы расстояния принимают `(geometry, distance)` — конечное число, ноль или больше, в метрах на
geography и в единицах SRID на geometry:

| Оператор | Истинно, когда расстояние от геометрии колонки до значения |
|---|---|
| `__dwithin` | не больше `distance` — использует пространственный индекс |
| `__distance_lt`, `__distance_lte` | меньше / не больше `distance` |
| `__distance_gt`, `__distance_gte` | больше / не меньше `distance` |

```python
await City.objects.filter(center__dwithin=(Point(37.62, 55.75), 10_000))   # geography: 10 км
```

Фильтровать по радиусу нужно через `__dwithin`: сравнения расстояния считают расстояние для каждой
строки. Геометрия фильтра в другом SRID, чем у колонки, преобразуется в SRID колонки в самом запросе.
Неверное значение — не геометрия, не пара, отрицательное расстояние, неверный шаблон — даёт
`ValidationError` до выполнения запроса.

## <a id="paths"></a>Пути

`__x`, `__y`, `__z` читают координаты точки (float), `__srid` — SRID (int) — в фильтрах, `values()`,
`F()` и `order_by()`:

```python
await Shop.objects.filter(location__x__gte=37.5).values_list("location__y", flat=True)
```

## <a id="functions"></a>Функции

`hare.gis.functions` — каждая принимает первым аргументом геометрию (имя поля, `F()` или выражение);
вторая геометрия (`Distance`, `Intersection`, ...) — `F()`, выражение или геометрия из Python в любой
форме, которую берёт поле, преобразованная в SRID первой:

| Функция | Результат |
|---|---|
| `Area(geometry)` | float — квадратные метры на geography, единицы SRID на geometry |
| `Length(geometry)`, `Perimeter(geometry)` | float — длина линии, длина колец полигона |
| `Distance(geometry, other)` | float — кратчайшее расстояние |
| `Centroid`, `PointOnSurface` | точка — центр масс, точка заведомо на геометрии |
| `Envelope`, `Boundary`, `ConvexHull` | ограничивающий прямоугольник, граница, выпуклая оболочка |
| `Buffer(geometry, distance)` | область в пределах `distance` (отрицательное сжимает полигон) |
| `Intersection`, `Difference`, `SymmetricDifference`, `Union` (двух) | наложение двух геометрий |
| `Transform(geometry, srid)` | геометрия в другом SRID |
| `Simplify(geometry, tolerance)` | меньше точек, ни одна не сдвинута дальше `tolerance` |
| `MakeValid(geometry)` | корректная геометрия, покрывающая точки некорректной |
| `IsValid(geometry)` | bool |
| `AsText(geometry)` | str — WKT |
| `AsGeoJSON(geometry)` | dict — геометрия GeoJSON |
| `NumPoints(geometry)`, `NumGeometries(geometry)` | int |
| `GeometryTypeName(geometry)` | str — `POINT`, `POLYGON`, ... |

```python
from hare.gis.functions import Area, Distance, Transform

await City.objects.annotate(km=Distance("center", moscow) / 1000).order_by("km")
await Parcel.objects.annotate(square_meters=Area(Transform("shape", 3857)))
```

Геометрический результат читается как `Geometry`; расстояние `Buffer`, допуск `Simplify` и SRID
`Transform` проверяются при создании функции (`ValidationError`).

## <a id="aggregates"></a>Агрегаты

`hare.gis.aggregates` объединяют геометрии группы (`distinct=`, `_filter=` — как у любого агрегата);
только колонки geometry:

| Агрегат | Результат |
|---|---|
| `Collect(geometry)` | геометрии одной мульти-геометрией или коллекцией, без слияния |
| `UnionAggregate(geometry)` | геометрии, слитые в одну, перекрытия растворены |
| `MakeLine(point, order_by=...)` | точки одной линией, в порядке `order_by` |
| `Extent(geometry)` | ограничивающий прямоугольник `(xmin, ymin, xmax, ymax)` |

```python
from hare.gis.aggregates import Extent, MakeLine

await Shop.objects.aggregate(bounds=Extent("location"))
await Ping.objects.values("vehicle").annotate(track=MakeLine("position", order_by="created_at"))
```

## <a id="spatialite"></a>SQLite: SpatiaLite

В SQLite геометрия хранится в собственном BLOB-формате SpatiaLite, который hare пишет и читает сам:
хранение, чтение и сравнение геометрий на равенство расширения не требуют. Пространственные операторы,
пути `__x`/`__y`/`__z`/`__srid`, функции и агрегаты работают на расширении SpatiaLite, которое
загружается в каждое соединение по настройкам подключения:

```python
DB_URL = "sqlite+aiosqlite://db.sqlite3?load_spatialite=true"
```

| Настройка | Значение |
|---|---|
| `load_spatialite` | `true` загружает SpatiaLite в каждое соединение — `Features.supports_spatial` |
| `spatialite_path` | Библиотека: имя, которое находит загрузчик системы (`mod_spatialite`, по умолчанию — `libsqlite3-mod-spatialite` в Debian/Ubuntu, `libspatialite` в Homebrew), или путь. В Windows путь с каталогом загружает из этого каталога и библиотеки, нужные SpatiaLite (GEOS, PROJ, ...) |
| `spatialite_proj_database` | Путь к `proj.db` библиотеки PROJ, который читает преобразование между SRID, — нужен, когда PROJ не находит свой (сборка для Windows кладёт его рядом с библиотекой) |
| `spatialite_metadata` | Пространственные метаданные, которые hare создаёт в базе без них, когда соединение загружает SpatiaLite: `wgs84` (по умолчанию) — системы координат WGS84, долгота/широта 4326 и её зоны UTM, создаются за миллисекунды; `full` — все системы координат EPSG, известные SpatiaLite, создаются дольше; `none` — без метаданных. База, где метаданные уже есть, сохраняет свои. Они нужны geography (`Features.supports_geography`) и пространственному индексу (`Features.supports_spatial_index`) |

`spatialite_path`, `spatialite_proj_database` и `spatialite_metadata` без `load_spatialite=true` дают
`ConfigurationError`, как и библиотека, которая не загружается, и значение `spatialite_metadata` не из
этих трёх. Без `load_spatialite` каждый пространственный оператор, путь, функция и агрегат дают
`UnSupportedError` до отправки SQL.

Системы координат читаются из метаданных при открытии соединения (`Features.spatial_reference_ids`).
Geography или пространственный индекс в SRID, которой в метаданных нет, дают `UnSupportedError` до
отправки SQL — с метаданными `wgs84`, например, geography в NAD83 (4269) или пространственный индекс
колонки Web Mercator (3857): создайте базу с `spatialite_metadata=full` или добавьте в неё систему
координат (`InsertEpsgSrid()` SpatiaLite) и подключитесь заново.

- **Колонки** типизированы по типу геометрии — `POINT`, `POLYGON`, `GEOMETRY`, с `Z` для x/y/z
  (`POINTZ`). Пустой геометрии в SpatiaLite нет: её запись даёт `ValidationError`.
- **Geography** — геометрия долготы/широты, измеряемая на эллипсоиде: `Area`, `Length`, `Perimeter`,
  `Distance`, `__dwithin` и операторы расстояния — в квадратных метрах и метрах, через пространственные
  метаданные SpatiaLite — с `spatialite_metadata=none` они дают `UnSupportedError`.
  Отношения, которые PostGIS проверяет на сфероиде (`__intersects`, `__covers`, `__covered_by`,
  `__bbox_overlaps`), и `Centroid`, `Buffer`, `Intersection` для geography дают `UnSupportedError` —
  таких форм у SpatiaLite нет; сначала преобразуйте её в плоскую SRID. SpatiaLite измеряет на эллипсоиде
  через librttopo — ответвление геометрической библиотеки PostGIS.
- **Преобразование между SRID** (`Transform`, геометрия фильтра в другой SRID) называет SRID кодами EPSG
  для PROJ, поэтому таблица систем координат в базе для него не нужна.
- **`__contains_properly`** — шаблон DE-9IM `T**FF*FF*`: своей функции у SpatiaLite нет.
- **`GeometryTypeName`** даёт `POINT` для точки с z, как PostGIS (SpatiaLite пишет `POINT Z`).
- **`MakeLine(order_by=...)`** требует SQLite 3.44 или новее (`Features.supports_ordered_aggregates`).
- Таблицы пространственных метаданных SpatiaLite не принадлежат моделям — `inspectdb` и проверка
  расхождений их не видят.

### <a id="spatial-index"></a>Пространственный индекс

`SpatialiteIndex` — пространственный индекс SpatiaLite по одному геометрическому полю: R*Tree
ограничивающих прямоугольников геометрий колонки.

```python
from hare import Model, fields
from hare.dialects.sqlite.indexes import SpatialiteIndex
from hare.gis import PointField


class Shop(Model):
    id = fields.IntField(primary_key=True)
    location = PointField()

    class Meta:
        indexes = (SpatialiteIndex(fields=("location",)),)
```

- **Создание** регистрирует колонку в пространственных метаданных с типом геометрии, SRID и
  размерностью поля (`RecoverGeometryColumn()`) и создаёт её R*Tree (`CreateSpatialIndex()`),
  заполненное из уже имеющихся строк. Дальше триггеры SpatiaLite держат R*Tree в согласии с таблицей и
  отклоняют геометрию другого типа, SRID или размерности с `IntegrityError` — у строк, записанных через
  hare, такой не бывает: геометрическое поле проверяет их заранее. Миграция, добавляющая индекс к
  таблице с такой строкой (записанной другим SQL), падает с `OperationalError`.
- **Имя** задаёт SpatiaLite: таблица R*Tree — `idx_<таблица>_<колонка>`, поэтому `name` индекс не
  принимает. Один индекс на поле.
- **Модели нужен целочисленный первичный ключ** — R*Tree ключует строки по нему: неявный rowid таблицы
  без такого ключа SQLite может перенумеровать; иначе `ConfigurationError`.
- **Операторы** `__intersects`, `__contains`, `__contains_properly`, `__within`, `__covers`,
  `__covered_by`, `__crosses`, `__equals`, `__overlaps`, `__touches`, операторы `__bbox_*` и
  `__dwithin` геометрии сначала берут из R*Tree (таблица `SpatialIndex` SpatiaLite) строки, чей
  прямоугольник пересекает прямоугольник другой геометрии — для `__dwithin` расширенный на расстояние,
  — и проверяют только их; через связь (`shops__location__within=...`) тоже. `__disjoint`, `__relate`,
  операторы расстояния и `__dwithin` для geography (расстояние в метрах, прямоугольники в градусах)
  проверяют каждую строку. R*Tree читается, только если индекс объявлен в модели: база, потерявшая его
  (удалённый другим SQL), отвечает на эти операторы пустым результатом — проверка расхождений покажет,
  что индекса нет.
- **Миграции** держат его в согласии с таблицей: переименование таблицы или поля, пересборка таблицы
  (изменение другого поля, `STRICT`) и удаление модели сначала удаляют R*Tree и регистрацию колонки, а
  затем создают их заново для новой таблицы или колонки. `sqlmigrate` показывает вызовы SpatiaLite.
- **Проверка расхождений и `inspectdb`** читают его из пространственных метаданных (включённый
  пространственный индекс колонки); его таблицы R*Tree и триггеры SpatiaLite моделям не принадлежат.
- Ему нужны `features.supports_spatial_index` — SpatiaLite, загруженный с метаданными, — и SRID поля в
  метаданных; иначе `UnSupportedError` до отправки SQL. На PostgreSQL он даёт `UnSupportedError` —
  там объявите `GistIndex`.

## <a id="integrations"></a>Pydantic и inspectdb

В pydantic-модели, созданной по модели, геометрическое поле — геометрия GeoJSON: принимает словарь
GeoJSON, WKT/EWKT, (E)WKB или `Geometry`, сериализуется объектом GeoJSON, а его JSON-схема описывает
геометрию GeoJSON. `inspectdb` читает колонку PostGIS с типом и SRID как подходящее поле
(`geometry(PolygonZ,3857)` -> `PolygonField(srid=3857, dimensions=3)`); колонка `geography(Point,4326)`
остаётся [`PostGISField`](../postgresql/fields.ru.md#postgisfield) с кортежем `(широта, долгота)`, а
колонка без типа и SRID или с координатами M — неоднозначный `TextField`.
