# Типы полей

## Базовый класс `Field` {: #the-base-field-class }

Каждое поле наследует `hare.fields.base.Field[TValue]`. Аргументы его конструктора есть у всех
типов полей ниже:

```python
def __init__(
    self,
    source_field: str | None = None,
    generated: bool = False,
    primary_key: bool | None = None,
    null: bool = False,
    default: Any = None,
    db_default: Any = DB_DEFAULT_NOT_SET,
    unique: bool = False,
    db_index: bool | None = None,
    description: str | None = None,
    model: Model | None = None,
    validators: list[Validator | Callable[[Any], None]] | None = None,
    sensitive: bool = False,
    **kwargs: Any,
) -> None
```

| Аргумент | Что задаёт |
|---|---|
| `source_field` | Имя колонки в базе (по умолчанию — имя атрибута поля в Python). Если два поля одной модели указывают на одну колонку, будет `ConfigurationError`. |
| `generated` | Значение колонки создаёт сама база (`SERIAL`, `AUTOINCREMENT` и подобные). Если класс поля так не умеет, будет `ConfigurationError`. Генерируемое целочисленное поле, которое не является первичным ключом, может только описывать уже существующую колонку (например, колонку `IDENTITY` в PostgreSQL): создание таблиц и миграции для него дают `ConfigurationError`, поэтому у такой модели должно быть `Meta.managed = False`. |
| `primary_key` | Колонка — первичный ключ таблицы (с индексом и уникальностью через ограничение `PRIMARY KEY`). `null=True` вместе с `primary_key=True` даёт `ConfigurationError`. |
| `null` | Колонка допускает `NULL`. |
| `default` | Значение по умолчанию на стороне Python: само значение, обычная функция или **асинхронная** функция; вычисляется при `save()`. В схему базы не попадает. Значение (или результат функции) преобразуется так же, как значение, переданное явно, поэтому в памяти оказывается тот же тип, что вернёт последующее чтение: `default="red"` у `CharEnumField` — это элемент перечисления, `default=1.5` у `DecimalField` — `Decimal("1.50")`, `datetime` без часового пояса получает настроенный пояс, `default=5_000_000` у `TimeDeltaField` — `timedelta(seconds=5)`. |
| `db_default` | Настоящее значение по умолчанию в базе (`DEFAULT`): постоянное значение или объект `SqlDefault`/`Now`/`RandomHex` (`hare.fields.db_defaults`). Функцией быть не может. Если у поля заданы и `default=`, и `db_default=`, выдаётся предупреждение `RedundantDbDefaultWarning`: при заданном `default` значение всегда подставляется до отправки `INSERT`, и `db_default` не применяется никогда. Исключение — `ForeignKeyField`/`OneToOneField` с `on_delete=SET_DEFAULT` и настоящим ограничением в базе: там база сама берёт `db_default` при удалении, и предупреждения нет. |
| `unique` | Ограничение уникальности. |
| `db_index` | Отдельный индекс по этой колонке. У `ForeignKeyField` включён по умолчанию — см. [Связи](relations.ru.md#index-on-the-key-column). |
| `description` | Записывается комментарием к колонке в базе. |
| `model` | Модель, которой принадлежит поле. Её задаёт сам hare, когда строит класс модели; вручную не передаётся. |
| `validators` | Список объектов `Validator` и/или обычных функций — см. [Проверки значений](validators.ru.md). |
| `sensitive` | Помечает секретные данные (учётные данные, токены, персональные данные) — см. [Чувствительные поля](encrypted-and-sensitive-fields.ru.md#sensitive-fields). На схему базы не влияет. |

Аргумент, которого поле не принимает, даёт `TypeError` с его именем.

Кроме того, у каждого поля есть: `to_db_value(value, instance)`, `from_db_value(value)`,
`validate(value)`, `required` (свойство: `True`, если поле не допускает `NULL`, у него нет значения
по умолчанию и `db_default` и его не генерирует база), `deconstruct()` (путь класса поля и
аргументы конструктора — то, что записывает файл миграции и сравнивает автоматическое создание
миграций), `get_annotation()` (тип Python значения поля для схемы: перечисление у поля-перечисления,
`| None`, если поле допускает `NULL`, — его читают создание моделей pydantic, DTO Litestar и
параметры request-query) и `constraints` — словарь
ограничений в стиле JSON Schema, например `max_length`/`ge`/`le`.

Что это за поле, можно узнать без проверки его класса:

| Атрибут | Значение |
|---|---|
| `relation_type` | Вид связи — `RelationType` (`hare.fields`): `FOREIGN_KEY`, `ONE_TO_ONE`, `MANY_TO_MANY`, `BACKWARD_FOREIGN_KEY`, `BACKWARD_ONE_TO_ONE`; `None` у поля с обычным значением, в том числе у колонки ключа прямой связи (`author_id`). |
| `encrypted` | `True` у [зашифрованного поля](encrypted-and-sensitive-fields.ru.md#encrypted-fields): в его колонке лежит шифротекст, поэтому его нельзя сравнивать по значению, сортировать, группировать и использовать в агрегатах. |
| `enum_type` | Класс перечисления у `IntEnumField`/`CharEnumField`; `None` у остальных полей. |
| `model` / `model_field_name` | Модель, к которой привязано поле, и имя его атрибута в ней. |

### Значения по умолчанию в базе (`hare.fields.db_defaults`) {: #db-default-helpers }

```python
class MyModel(Model):
    counter = fields.IntField(db_default=SqlDefault("0"))    # SQL как есть, без изменений
    created_at = fields.DatetimeField(db_default=Now())      # STATEMENT_TIMESTAMP() в PostgreSQL
    tracking_id = fields.CharField(max_length=36, db_default=RandomHex())  # случайная hex-строка, своя для каждого диалекта
```

`SqlDefault(sql: str)` вставляет строку `sql` как есть в команды создания таблиц — в
`generate_schemas()` и в миграции. **Никогда не собирайте её из данных, пришедших извне.** В SQLite
выражение, которое не является литералом или числом со знаком (`SqlDefault("40 + 2")`), берётся в
скобки — этого требует `DEFAULT` в SQLite. `Now()` и `RandomHex()` — готовые варианты для двух
самых частых случаев, одинаково работающие на разных диалектах. `RandomHex()` сам выбирает
выражение для диалекта: `(lower(hex(randomblob(16))))` в SQLite, `md5(random()::text)` в PostgreSQL.
В SQLite `Now()` записывает такой же текст, каким хранится значение `DatetimeField`, записанное из
Python (UTC с суффиксом `+00:00` при `use_tz=True`, местное время без пояса при `use_tz=False`),
поэтому фильтры по точному значению, `__in`, по диапазону и `get_or_create()` находят строку,
заполненную этим значением. У `DateField` и `TimeField` `Now()` записывает текущую дату или время
на часах в настроенном поясе (в местном поясе системы при `use_tz=False`) на любом диалекте — то же
значение, которое записала бы из Python `Timezone.localtime()`; у `TimeField` — с тем же смещением,
которое получает время без пояса. Поэтому `filter(day=today)` находит строку. Пояс берётся в момент
создания команд таблицы. В SQLite пояс с переходом на летнее время применяет функция
`hare_local_now`, которую hare регистрирует в своих соединениях, поэтому строки, вставленные в такую
таблицу другим клиентом SQLite, должны указывать значение колонки явно.

Постоянное значение `db_default` для даты-времени или времени записывается так же, как то же
значение, записанное из Python: в SQLite значение по умолчанию `DatetimeField` хранится текстом в
UTC (`2020-01-02 03:00:00+00:00`); в PostgreSQL значение `DatetimeField` без пояса при
`use_tz=False` получает местное смещение системы, а значение `TimeField` — своё смещение (время без
пояса получает стандартное смещение настроенного пояса при `use_tz=True` и UTC при `use_tz=False`).

## Поля с данными (`hare.fields`) {: #data-fields }

| Поле | Тип в Python | Дополнительные аргументы | Тип в SQL (по умолчанию / PostgreSQL) |
|---|---|---|---|
| `IntField` | `int` | с `primary_key` по умолчанию `generated=True` | `INT` / `INTEGER`, `SERIAL` при генерации — ограничения `ge=-2^31, le=2^31-1` |
| `BigIntField` | `int` | — | `BIGINT` / `BIGSERIAL` при генерации — границы int64 |
| `SmallIntField` | `int` | — | `SMALLINT` / `SMALLSERIAL` при генерации — границы int16 |
| `PositiveSmallIntField` | `int` | — | ограничения `ge=0, le=наибольшее int16` |
| `PositiveIntField` | `int` | — | ограничения `ge=0, le=наибольшее int32` |
| `PositiveBigIntField` | `int` | — | ограничения `ge=0, le=наибольшее int64` |
| `CharField` | `str` | **`max_length: int`** (обязателен, ≥1) | `VARCHAR(max_length)`; сам добавляет `MaxLengthValidator` |
| `TextField` | `str` | — | `TEXT`; допускает `unique=True`/`db_index=True` и может входить в `UniqueConstraint`/`Meta.indexes` (и PostgreSQL, и SQLite индексируют `TEXT` сами) |
| `BooleanField` | `bool` | — | `BOOL` (SQLite: `INT`) |
| `DecimalField` | `Decimal` | **`max_digits: int`, `decimal_places: int`** (оба обязательны, `max_digits ≥ 1`, `0 ≤ decimal_places ≤ max_digits`) | `DECIMAL(max_digits, decimal_places)` |
| `DatetimeField` | `datetime.datetime` | `auto_now: bool`, `auto_now_add: bool` (только один из двух) | `TIMESTAMP` (PostgreSQL: `TIMESTAMPTZ`) |
| `DateField` | `datetime.date` | — | `DATE` |
| `TimeField` | `datetime.time` | `auto_now`, `auto_now_add` (местное время на часах со стандартным смещением настроенного пояса — тем же, что получает время без пояса) | `TIME` (PostgreSQL: `TIMETZ`) |
| `TimeDeltaField` | `datetime.timedelta` | — | `BIGINT` (хранится в микросекундах); обычное целое число при любой записи считается числом микросекунд |
| `FloatField` | `float` | — | `DOUBLE PRECISION` (SQLite: `REAL`) |
| `JSONField[T]` | `dict` / `list` (или класс модели Pydantic) | `encoder`, `decoder`, `field_type` (для документации OpenAPI) | `JSON` (PostgreSQL: `JSONB`); обычный индекс-дерево (btree: `unique=True`/`db_index=True`/`Index(fields=...)`) не допускается, но индекс другого вида в `Meta.indexes`, например `GinIndex(fields=[...])`, принимается |
| `UUIDField` | `uuid.UUID` | — (`primary_key=True` без явного `default` сам задаёт `default=uuid4`) | `CHAR(36)` (PostgreSQL: `UUID`); читается как обычный `uuid.UUID` на любом драйвере |
| `BinaryField` | `bytes` | — | `BLOB` (PostgreSQL: `BYTEA`); не индексируется, фильтровать и обновлять через `update()` по нему нельзя |

```python
class Widget(Model):
    id = fields.UUIDField(primary_key=True)
    name = fields.CharField(max_length=200)
    price = fields.DecimalField(max_digits=10, decimal_places=2)
    tags = fields.JSONField(default=list, field_type=list[str])
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
```

Целочисленное поле принимает `float`/`Decimal` без дробной части (`5.0`, `Decimal("5")`) как
целое, а число с дробной частью (`5.7`) при любой записи даёт `ValidationError`, а не обрезается. В
фильтре такое число сравнивается точно: `number=1.5` и `number__in=[1.5, 3]` никогда не совпадут с
`1`.

`max_digits` и `decimal_places` у `DecimalField` проверяются для каждого значения при `save()`, а
не только при объявлении поля: `Decimal("12345.67")` в поле с `max_digits=6` даёт
`ValidationError`, а не тихое обрезание или неожиданную ошибку базы.

**`DecimalField` в SQLite.** В SQLite нет точного десятичного типа: колонка хранится текстом
(`VARCHAR(40)`, в записи с фиксированной точкой и полным числом знаков после запятой — `2.0000`,
`0.0000000001` — так, как печатает PostgreSQL). Сравнения (`exact`, `gt`, `lt`, `in`, `range`,
`not` — со значением или с другим `DecimalField` через `F()`), сортировка, `Min`/`Max` и простое
копирование через `F()` (`update(price=F("price"))`, `annotate(copy=F("price"))`) работают с этим
текстом как с точным десятичным числом, каким бы ни было `max_digits`. Фильтр по вычисляемому
значению, сравниваемому с `Decimal` (`annotate(top=Max("price")).filter(top__gt=Decimal("10"))`,
`__in`, `__range`), тоже точен. `contains`, `startswith`, `endswith`, `iexact` (и варианты без
учёта регистра) сравнивают тот же текст, что и PostgreSQL: `2.0000` у поля с `decimal_places=4` и
кратчайшую запись у `FloatField` (`2`, а не `2.0`). Арифметика (`F("price") * 2`, `Sum`, `Avg`),
`Coalesce`/`Case` над `DecimalField` и вычисляемое значение, сравниваемое со смесью `Decimal` и
других чисел, проходят через 64-битное число с плавающей точкой: сохраняется около 15 значащих
цифр. В PostgreSQL используется настоящий `NUMERIC` произвольной точности без такого ограничения;
для денежных расчётов, близких к `max_digits`, берите его.

**Дата и время.** `DatetimeField` принимает `datetime`, `date` (её первый момент в настроенном
поясе при `use_tz=True` — полночь или конец пропущенного часа при переводе часов, если он начинается
в полночь, как 2024-09-08 в `America/Santiago`; так же и при сравнении даты с `DatetimeField`),
строку ISO 8601, начинающуюся с полной даты (`2024-05-01`, `2024-05-01T10:00:00+03:00`), или целое
число секунд от начала эпохи Unix. `DateField` принимает `date`, `datetime` (берётся его дата) или
такую строку. Всё остальное — `float`, просто `"2024"`, неразборчивый текст, число секунд за
пределами диапазона `datetime` — даёт `ValidationError` при создании объекта и при любой записи
(`create()`, `bulk_create()`, `save()`, `update()`, `bulk_update()`, `update_or_create()`), одинаково
на всех диалектах. Так же ведёт себя `datetime`, который выходит за пределы диапазона после перевода
в настроенный пояс или в UTC. Операторы фильтра вроде `__year="2024"` сравнивают извлечённое число и
этим не затронуты. `TimeField` работает и в SQLite: значение хранится текстом ISO (`12:30:45`, у
времени с поясом — со своим смещением, `12:30:45+03:00`), а сравнения, сортировка и `Min`/`Max`
упорядочивают его так же, как PostgreSQL упорядочивает `TIMETZ`, — по времени в UTC (`10:00+03:00`
раньше `08:00+00:00`), причём два значения равны, только если совпадают и время на часах, и
смещение; время без пояса считается временем в UTC. Это относится и к уже сохранённым строкам,
поскольку сохранённый текст не меняется. При `use_tz=False` дата-время раньше 1970 года переводится
в местное время системы и обратно даже в Windows, где `datetime.astimezone()` этого не умеет (там
используется пояс, который называет необязательный пакет `tzlocal`, а без него — смещение системы
на 1970-01-02). В PostgreSQL значение без пояса при `use_tz=False` передаётся как момент, который
соответствует этому местному времени на часах, поэтому `asyncpg` и `rust_pg` записывают и читают
одно и то же. `DatetimeField`, описывающий уже существующую колонку PostgreSQL типа `timestamp` (без
пояса), читает её как время на часах в UTC и записывает время значения на часах в UTC на обоих
драйверах — так же, как сам PostgreSQL переводит `timestamp` в `timestamptz` в сеансе hare, где
действует пояс UTC.

**Крайние даты в PostgreSQL.** В PostgreSQL (и через `asyncpg`, и через `rust_pg`) значения
`datetime.min`/`datetime.max` — в том числе тот же момент, записанный в другом поясе, — и
`date(1, 1, 1)`/`date(9999, 12, 31)` хранятся как `-infinity`/`infinity` и читаются обратно теми же
значениями Python. Сортировка и сравнения (`__lt`, `__gte`, `order_by()`) считают их самым малым и
самым большим значением, но `__year`, `__month`, `__day` и другие операторы по части даты никогда с
ними не совпадают, а `F("dt") ± timedelta(...)` оставляет их бесконечными. SQLite хранит их как
обычные даты, поэтому там эти операторы и арифметика работают. Не используйте такие значения как
настоящие даты — только как отметку «без границы», либо используйте `NULL`.

`JSONField` даёт `ValidationError` (а не просто `ValueError`/`TypeError`), если значение не удаётся
преобразовать в JSON, если из колонки прочитан испорченный JSON или если значение не соответствует
объявленному `field_type`. Объявленный `field_type` (модель Pydantic, `list[Model]`, `list[str]` и
т. п.) применяется через `TypeAdapter` из Pydantic при присваивании, при `save()` и при чтении:
словарь (или список словарей) сразу становится моделью, поэтому в памяти лежит то же значение, что
вернёт последующее чтение, а элементы списка проверяются по одному. `pydantic_model_creator()`
добавляет этот тип в схему. Без `field_type` (в том числе с обобщённым `JSONField[T]`, который
действует только для проверки типов) значения остаются обычным JSON.

Значение, которое JSON не может хранить, даёт `ValidationError` при любой записи (`create()`,
`save()`, `update()`, `bulk_create()`, в том числе с `use_copy=True`, `bulk_update()`): число `NaN`
или `Infinity` где угодно внутри значения, а также нулевой байт в строке или ключе. Целое число
любого размера сохраняется и читается точно — число вне 64-битного диапазона, с которым работает
orjson, кодирует и раскодирует стандартная библиотека. `datetime`, `date`, `time` без пояса и `UUID`
внутри значения записываются одинаковым текстом ISO/UUID по обоим путям, в том числе когда orjson
не установлен вовсе.

И в SQLite, и в PostgreSQL `exact`/`not` у `JSONField` сравнивают значения JSON: порядок ключей и
разница между `1` и `1.0` не важны.

В SQLite колонка `JSONField` объявляется как `JSON_TEXT` (текстовое сродство), поэтому число на
верхнем уровне хранится своим текстом JSON: `12345678901234567890` и `1.0` читаются без изменений.

### Поля-перечисления {: #enum-fields }

Это не классы, а функции, которые возвращают объект внутреннего подкласса, поэтому
`type(Model.status) is not IntEnumField`:

```python
def IntEnumField(enum_type: type[IntEnum], description: str | None = None, **kwargs) -> IntEnumType
def CharEnumField(enum_type: type[Enum], description: str | None = None, max_length: int = 0, **kwargs) -> CharEnumType
```

- `IntEnumField` построено на `SmallIntField` — значения перечисления должны помещаться в `int16`
  (1..32767 при `generated=True`, иначе весь диапазон `int16`).
- `CharEnumField` построено на `CharField` — при `max_length=0` длина берётся по самому длинному
  значению перечисления. **Если позже вы добавите более длинное значение, увеличьте `max_length`
  сами** — оно не пересчитывается. Перечисление с нестроковыми значениями (например, `ONE = 1`)
  тоже работает: в колонку пишется `str(member.value)` (`"1"`), а при чтении текст превращается
  обратно в элемент перечисления.
- Оба сами составляют `description` в виде `"ИМЯ: значение"` со всеми элементами, если вы не
  передали своё.

```python
from enum import Enum

class Status(Enum):
    DRAFT = "draft"
    PUBLISHED = "published"

class Post(Model):
    status = fields.CharEnumField(Status, default=Status.DRAFT)
```

### `CompositePrimaryKey` {: #compositeprimarykey }

```python
class CompositePrimaryKey:
    def __init__(self, *field_names: str) -> None
```

Это не настоящее поле — своей колонки у него нет. Оно объявляет первичным ключом таблицы два или
больше уже объявленных полей:

```python
class ArticleVersion(Model):
    id = fields.UUIDField()
    version = fields.IntField()
    pk = CompositePrimaryKey("id", "version")
```

Каждое названное поле должно существовать и само **не** должно быть `primary_key=True` или
`generated=True`. `Model.pk` у такой модели — кортеж. `clone()` вычисляет `default=`/`db_default=`
каждой части ключа отдельно, поэтому в `pk=(...)` явно указываются только части, у которых нет ни
того, ни другого.

!!! note
    Внешний ключ **может** ссылаться на модель с составным первичным ключом — см.
    [Связи](relations.ru.md#targeting-a-composite-primary-key) — с настоящим ограничением
    `FOREIGN KEY` на уровне таблицы, каскадами, соединением таблиц и `prefetch_related()`.

## Поля только для PostgreSQL {: #postgresql-only-fields }

`ArrayField`, `HStoreField`, `TSVectorField`, `PostGISField`, `CitextField`, `VectorField`
(pgvector) и поля диапазонов находятся в `hare.dialects.postgresql` — см.
[Поля PostgreSQL](../dialects/postgresql/fields.ru.md).

## Свой класс поля {: #writing-your-own-field }

См. [Свои поля](../extending/custom-fields.ru.md).
