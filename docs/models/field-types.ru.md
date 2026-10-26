# Типы полей

Поля `hare.fields`: аргументы, которые принимает любое поле, поля с обычными значениями,
поля-перечисления, вычисляемые колонки и составные первичные ключи, — и где находятся поля одной базы.

## <a id="the-base-field-class"></a>Базовый класс `Field`

Каждое поле наследует `hare.fields.Field[TValue]`. Аргументы его конструктора есть у всех типов полей
ниже:

```python
class Field:
    def __init__(
        self,
        *,
        source_field: str | None = None,
        generated: bool = False,
        primary_key: bool | None = None,
        null: bool = False,
        default: Any = None,
        db_default: Any = NOT_PROVIDED,
        unique: bool = False,
        db_index: bool | None = None,
        description: str | None = None,
        validators: list[Validator | Callable[[Any], None]] | None = None,
        sensitive: bool = False,
        **kwargs: Any,
    ) -> None: ...
```

| Аргумент | Что задаёт |
|---|---|
| `source_field` | Имя колонки в базе (по умолчанию — имя атрибута поля в Python). Если два поля одной модели указывают на одну колонку, будет `ConfigurationError`. |
| `generated` | Значение колонки создаёт сама база (`SERIAL`, `AUTOINCREMENT` и подобные) — см. [`generated`](#generated) |
| `primary_key` | Колонка — первичный ключ таблицы (с индексом и уникальностью через ограничение `PRIMARY KEY`). `null=True` вместе с `primary_key=True` даёт `ConfigurationError`. |
| `null` | Колонка допускает `NULL`. |
| `default` | Значение по умолчанию на стороне Python: само значение, обычная функция или **асинхронная** функция — см. [`default`](#default) |
| `db_default` | Настоящее значение по умолчанию в базе (`DEFAULT`): постоянное значение или объект `SqlDefault`/`Now`/`RandomHex`/`UuidV7` (`hare.fields.db_defaults`) — см. [`db_default`](#db-default) |
| `unique` | Ограничение уникальности. |
| `db_index` | Отдельный индекс по этой колонке. У `ForeignKeyField` включён по умолчанию — см. [Связи](relations.ru.md#index-on-the-key-column). |
| `description` | Записывается комментарием к колонке в базе. |
| `validators` | Список объектов `Validator` и/или обычных функций — см. [Проверки значений](validators.ru.md). |
| `sensitive` | Помечает секретные данные (учётные данные, токены, персональные данные) — см. [Чувствительные поля](encrypted-and-sensitive-fields.ru.md#sensitive-fields). На схему базы не влияет. |

Аргумент, которого поле не принимает, даёт `TypeError` с его именем. Аргументы, которые попадают в
схему, проверяются: `description` и `source_field` не текстом и `max_length` не `int` дают
`ConfigurationError`. Флаг (`null`, `unique`, `generated`, `primary_key`, `db_index`, `sensitive`)
читается по истинности, как его читает Python, а `validators` — это список; ни то, ни другое не
проверяется. Имя поля не может
содержать `__`, которое разделяет части пути поиска (`author__name`), и не может оканчиваться на `_` —
`from___gt` читалось бы как поле `from` и оператор `_gt`: такое поле или такой же `related_name` дают
`ConfigurationError` при объявлении модели, как и пустое имя колонки или таблицы либо имя с нулевым
байтом, а также `description`, текстовый `default` или `db_default` с нулевым байтом: SQL-текст его
не вмещает, и миграция упала бы только там, где его пишет. Колонка с именем ключевого слова сохраняет имя
через `source_field` (`from_field = fields.IntField(source_field="from")`), как пишет его
[`inspectdb`](../migrations/inspectdb.ru.md).

Кроме того, у каждого поля есть: `to_db_value(value, instance)`, `from_db_value(value)`,
`validate(value)`, `required` (свойство: `True`, если поле не допускает `NULL`, у него нет значения по
умолчанию и `db_default` и его не генерирует база), `deconstruct()` (путь класса поля и аргументы
конструктора — то, что записывает файл миграции и сравнивает автоматическое создание миграций),
`get_annotation()` (тип Python значения поля для схемы: перечисление у поля-перечисления, `| None`, если
поле допускает `NULL`, — его читают создание моделей pydantic, DTO Litestar и параметры request-query) и
`constraints` — словарь ограничений в стиле JSON Schema, например `max_length`/`ge`/`le`.

Что это за поле, можно узнать без проверки его класса:

| Атрибут | Значение |
|---|---|
| `relation_type` | Вид связи — `RelationType` (`hare.fields`): `FOREIGN_KEY`, `ONE_TO_ONE`, `MANY_TO_MANY`, `BACKWARD_FOREIGN_KEY`, `BACKWARD_ONE_TO_ONE`; `None` у поля с обычным значением, в том числе у колонки ключа прямой связи (`author_id`). |
| `encrypted` | `True` у [зашифрованного поля](encrypted-and-sensitive-fields.ru.md#encrypted-fields): в его колонке лежит шифротекст, поэтому его нельзя сравнивать по значению, сортировать, группировать и использовать в агрегатах. |
| `enum_type` | Класс перечисления у `IntEnumField`/`CharEnumField`; `None` у остальных полей. |
| `model` / `model_field_name` | Модель, к которой привязано поле, и имя его атрибута в ней. |

### <a id="generated"></a>`generated`

Значение колонки создаёт сама база (`SERIAL`, `AUTOINCREMENT` и подобные). Если класс поля так не умеет,
будет `ConfigurationError`. Генерируемое целочисленное поле, которое не является первичным ключом, может
только описывать уже существующую колонку (например, колонку `IDENTITY` в PostgreSQL): создание таблиц и
миграции для него дают `ConfigurationError`, поэтому у такой модели должно быть `Meta.managed = False`.

### <a id="default"></a>`default`

Значение по умолчанию на стороне Python: само значение, обычная функция или **асинхронная** функция;
вычисляется при `save()`. В схему базы не попадает. Значение (или результат функции) преобразуется так
же, как значение, переданное явно, поэтому в памяти оказывается тот же тип, что вернёт последующее
чтение: `default="red"` у `CharEnumField` — это элемент перечисления, `default=1.5` у `DecimalField` —
`Decimal("1.50")`, `datetime` без часового пояса получает настроенный пояс.

Файл миграции записывает значение кодом: литерал, список, кортеж, словарь, множество или frozenset из них,
`Decimal`, `UUID`, дату или время, IP-адрес или сеть (`ipaddress`), элемент перечисления, функцию или класс
уровня модуля либо объект с `deconstruct()`. Любое другое значение — `Fraction`, `deque`, экземпляр
собственного класса — заставляет `makemigrations` бросить `ConfigurationError`, а не записать файл, который
не загрузится; дайте такому значению `deconstruct()` или укажите по умолчанию функцию, которая его возвращает.

### <a id="db-default"></a>`db_default`

Настоящее значение по умолчанию в базе (`DEFAULT`): постоянное значение или объект
`SqlDefault`/`Now`/`RandomHex`/`UuidV7` (`hare.fields.db_defaults`). Функцией быть не может. Если у поля
заданы и `default=`, и `db_default=`, выдаётся предупреждение `RedundantDbDefaultWarning`: при заданном
`default` значение всегда подставляется до отправки `INSERT`, и `db_default` не применяется никогда.
Исключение — `ForeignKeyField`/`OneToOneField` с `on_delete=SET_DEFAULT` и настоящим ограничением в базе:
там база сама берёт `db_default` при удалении, и предупреждения нет.

### <a id="db-default-helpers"></a>Значения по умолчанию в базе (`hare.fields.db_defaults`)

```python
class Book(Model):
    copies = fields.IntField(db_default=SqlDefault("0"))    # SQL как есть, без изменений
    added_at = fields.DatetimeField(db_default=Now())      # STATEMENT_TIMESTAMP() в PostgreSQL
    barcode = fields.CharField(max_length=36, db_default=RandomHex())  # случайная hex-строка, своя для каждого диалекта
```

`SqlDefault(sql: str)` вставляет строку `sql` как есть в команды создания таблиц — в `generate_schemas()`
и в миграции. **Никогда не собирайте её из данных, пришедших извне.** В SQLite выражение, которое не
является литералом или числом со знаком (`SqlDefault("40 + 2")`), берётся в скобки — этого требует
`DEFAULT` в SQLite. `Now()` и `RandomHex()` — готовые варианты для двух самых частых случаев, одинаково
работающие на разных диалектах. `RandomHex()` сам выбирает выражение для диалекта:
`(lower(hex(randomblob(16))))` в SQLite, `md5(random()::text)` в PostgreSQL. Свой подкласс — его SQL
пишет рендерер, который диалект для него регистрирует, как у `RandomHex`, — записывается в миграцию своим
классом и вызывается без аргументов; подкласс с аргументами переопределяет `deconstruct()`. В SQLite
`Now()` записывает такой же текст, каким хранится значение `DatetimeField`, записанное из Python (UTC с
суффиксом `+00:00` при `use_timezone=True`, местное время без пояса при `use_timezone=False`), поэтому
фильтры по точному значению, `__in`, по диапазону и `get_or_create()` находят строку, заполненную этим
значением. У `DateField` и `TimeField` `Now()` записывает текущую дату или время на часах в настроенном
поясе (в местном поясе системы при `use_timezone=False`) на любом диалекте — то же значение, которое
записала бы из Python `Timezone.localtime()`; у `TimeField` — с тем же смещением, которое получает время
без пояса. Поэтому `filter(day=today)` находит строку. Пояс берётся в момент создания команд таблицы. В
SQLite пояс с переходом на летнее время применяет функция `hare_local_now`, которую hare регистрирует в
своих соединениях, поэтому строки, вставленные в такую таблицу другим клиентом SQLite, должны указывать
значение колонки явно.

`UuidV7()` заполняет `UUIDField` UUID версии 7, который база генерирует для каждой строки, —
упорядоченным по времени: строка, вставленная позже, получает больший UUID, и индекс по первичному
ключу-UUID растёт с конца, а не в случайных местах:

```python
class Review(Model):
    id = fields.UUIDField(primary_key=True, db_default=UuidV7())
```

PostgreSQL 18 записывает его как `uuidv7()`; у более старого сервера PostgreSQL и у SQLite такой функции
нет, и создание колонки даёт `UnSupportedError` до любых команд DDL (`Features.supports_uuid_v7`). После
`create()` в экземпляре тот UUID, который сгенерировала база.

UUID версии 7 можно получить и в Python, на любой базе: `default=uuid.uuid7` в Python 3.14 или функция
библиотеки в 3.12 и 3.13 — `uuid_utils.uuid7`, `uuid6.uuid7`. UUID создаётся до `INSERT`, поэтому он есть
в экземпляре ещё до возврата из `create()`, а база без генерируемых ключей — ClickHouse — принимает его
как любое другое значение. ClickHouse упорядочивает UUID сначала по младшим 64 битам, поэтому ключ версии
7 там не сортируется по времени; сортируйте по колонке с датой. Миграция записывает функцию по пути импорта:

```python
import uuid

class Review(Model):
    id = fields.UUIDField(primary_key=True, default=uuid.uuid7)
```

Постоянное значение `db_default` для даты-времени или времени записывается так же, как то же значение,
записанное из Python: в SQLite значение по умолчанию `DatetimeField` хранится текстом в UTC
(`2020-01-02 03:00:00+00:00`); в PostgreSQL значение `DatetimeField` без пояса при `use_timezone=False`
получает местное смещение системы, а значение `TimeField` — своё смещение (время без пояса получает
стандартное смещение настроенного пояса при `use_timezone=True` и UTC при `use_timezone=False`).

Поле с `db_default` без собственного значения держит в новом объекте `DatabaseDefault` (`hare.fields`):
`INSERT` не пишет его колонку, и её заполняет база. Это значение ложно;
`isinstance(value, DatabaseDefault)` отличает его от других ложных значений. `bulk_create()` не пишет
колонку, которую все объекты оставляют базе, и бросает `QueryError` для колонки, которую оставляют только
некоторые.

## <a id="data-fields"></a>Поля с данными (`hare.fields`)

| Поле | Тип в Python | Дополнительные аргументы | Тип в SQL (по умолчанию / PostgreSQL) |
|---|---|---|---|
| `IntField` | `int` | с `primary_key` по умолчанию `generated=True` | `INT` / `INTEGER`, `SERIAL` при генерации — ограничения `ge=-2^31, le=2^31-1` |
| `BigIntField` | `int` | — | `BIGINT` / `BIGSERIAL` при генерации — границы int64 |
| `SmallIntField` | `int` | — | `SMALLINT` / `SMALLSERIAL` при генерации — границы int16 |
| `PositiveSmallIntField` | `int` | — | ограничения `ge=0, le=int16 max` |
| `PositiveIntField` | `int` | — | ограничения `ge=0, le=int32 max` |
| `PositiveBigIntField` | `int` | — | ограничения `ge=0, le=int64 max` |
| `CharField` | `str` | **`max_length: int`** (обязателен, ≥1) | `VARCHAR(max_length)`; сам добавляет `MaxLengthValidator` |
| `TextField` | `str` | — | `TEXT`; допускает `unique=True`/`db_index=True` и может входить в `UniqueConstraint`/`Meta.indexes` (и PostgreSQL, и SQLite индексируют `TEXT` сами) |
| `EmailField` | `str` | `max_length=254`, `lowercase: bool` | `VARCHAR(max_length)`; адрес почты, домен записывается в нижнем регистре — см. [ниже](#format-fields) |
| `URLField` | `str` | `max_length=2048`, `schemes=("http", "https")` | `VARCHAR(max_length)`; URL одной из схем `schemes` — см. [ниже](#format-fields) |
| `SlugField` | `str` | `max_length=50`, `allow_unicode: bool` | `VARCHAR(max_length)`, с индексом по умолчанию; буквы, цифры, `-` и `_` — см. [ниже](#format-fields) |
| `PhoneField` | `str` | `region: str` | `VARCHAR(16)`; номер телефона, записывается в виде E.164 — см. [ниже](#format-fields) |
| `BooleanField` | `bool` | — | `BOOL` (SQLite: `INT`); принимает только `bool` — `0`/`1` или текст дают `ValidationError` |
| `DecimalField` | `Decimal` | **`max_digits: int`, `decimal_places: int`** (оба обязательны, `max_digits ≥ 1`, `0 ≤ decimal_places ≤ max_digits`) | `DECIMAL(max_digits, decimal_places)` в PostgreSQL и ClickHouse, в SQLite колонки нет ([ниже](#decimal-field-sqlite)); текст разбирается, `bool` даёт `ValidationError` |
| `DatetimeField` | `datetime.datetime` | `auto_now: bool`, `auto_now_add: bool` (только один из двух) | `TIMESTAMP` (PostgreSQL: `TIMESTAMPTZ`) |
| `DateField` | `datetime.date` | — | `DATE` |
| `TimeField` | `datetime.time` | `auto_now`, `auto_now_add` (местное время на часах со стандартным смещением настроенного пояса — тем же, что получает время без пояса) | `TIME` (PostgreSQL: `TIMETZ`); `timedelta` или текст `24:00` дают `ValidationError` |
| `TimeDeltaField` | `datetime.timedelta` | — | `BIGINT` (хранится в микросекундах); число вместо `timedelta` даёт `ValidationError` |
| `FloatField` | `float` | — | `DOUBLE PRECISION` (SQLite: `REAL`) |
| `JSONField[T]` | `dict` / `list` (или класс модели Pydantic) | `encoder`, `decoder`, `field_type` (для документации OpenAPI) | `JSON` (PostgreSQL: `JSONB`); обычный индекс-дерево (btree: `unique=True`/`db_index=True`/`Index(fields=...)`) не допускается, но индекс другого вида в `Meta.indexes`, например `GinIndex(fields=[...])`, принимается |
| `UUIDField` | `uuid.UUID` | — (`primary_key=True` без явного `default` сам задаёт `default=uuid4`) | `CHAR(36)` (PostgreSQL: `UUID`); читается как обычный `uuid.UUID` на любом драйвере |
| `BinaryField` | `bytes` (и `bytearray`, `memoryview`; иное даёт `ValidationError`) | — | `BLOB` (PostgreSQL: `BYTEA`); фильтруется по равенству, `__in`, `__isnull` и сравнениям (порядок байтов) — текстовый lookup (`__contains`, ...) даёт `FieldError`, — обновляется через `update()`, принимает `unique=True`/`db_index=True` (индекс B-tree — в PostgreSQL значение больше примерно 2,7 КБ проиндексировать нельзя) |

```python
class Book(Model):
    id = fields.UUIDField(primary_key=True)
    title = fields.CharField(max_length=300)
    price = fields.DecimalField(max_digits=10, decimal_places=2)
    tags = fields.JSONField(default=list, field_type=list[str])
    added_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
```

`auto_now` и `auto_now_add` проставляет запись, один момент на вызов: все объекты одного
`bulk_create()`/`bulk_update()` и все строки одного `update()`/`merge()`/`insert_from()` получают один и
тот же момент. `auto_now` ставит каждая запись, `auto_now_add` — вставка объекта, у которого значения ещё
нет. Объект получает отметку до того, как пишутся его значения: неудачная запись возвращает прежнее
значение, а объект, не загрузивший поле `auto_now` (`.only()`/`.defer()`), получает отметку всё равно.
Запись заданных значений (`update()`, `merge()`, `insert_from()`) сохраняет значение, данное такому полю.

### <a id="text-values"></a>Текст

Текстовое поле (`CharField`, `TextField`, `SlugField`, `EncryptedTextField`, текстовые поля PostgreSQL
`CIText` и фиксированной длины, текстовые `FixedString`/`LowCardinality` ClickHouse) принимает только
`str`: число, `UUID`, `bytes` или любой другой объект при любой записи и в фильтре дают `ValidationError`
— иначе записался бы или сравнивался его `str()`. Текстовый оператор фильтра (`__contains`, `__icontains`,
`__startswith`, `__endswith`, `__iexact`, `__posix_regex`, `__search` и др.) у поля любого типа принимает
только `str`: `number__contains=5` даёт ошибку, а `number__contains="5"` ищет в тексте числа. Значение,
прочитанное из колонки, берётся таким, каким его вернул драйвер.

### <a id="numbers"></a>Числа

Целочисленное поле принимает только `int`: `bool`, `float` или `Decimal` (и `5.0` тоже) и текст (`"5"`)
при любой записи и в фильтре на равенство или вхождение (`=`, `__not`, `__in`, `__not_in`) дают
`ValidationError` — `int()` прочитал бы `"1_000"` или `"٥"` как числа, а дробь не бывает значением
колонки. Сравнение (`__gt`, `__gte`, `__lt`, `__lte`, `__range`) принимает и `float` или `Decimal` и
сравнивает точно: `number__gt=2.5` совпадёт с `3`.

`FloatField` принимает `int`, `float` и `Decimal`; `bool`, текст, а также `int` или конечный `Decimal` за
пределом диапазона float (`Decimal("1e500")`) дают `ValidationError`. `NaN` он
отклоняет в SQLite при любой записи — SQLite сохранил бы его как NULL; PostgreSQL и ClickHouse его
хранят. Отрицательный ноль
(`-0.0`) сохраняет знак в PostgreSQL и ClickHouse и читается как `0.0` в SQLite, который так его хранит;
они везде равны друг другу.

`max_digits` и `decimal_places` у `DecimalField` проверяются для каждого значения при `save()`, а не
только при объявлении поля: `Decimal("12345.67")` в поле с `max_digits=6` даёт `ValidationError`, а не
тихое обрезание или неожиданную ошибку базы. Значение с большим числом знаков после запятой на любой базе
округляется от нуля: `Decimal("2.345")` в поле с `decimal_places=2` — `Decimal("2.35")`. `update()` из
выражения записывает то, что из него делает база: PostgreSQL округляет от нуля, ClickHouse отбрасывает
лишние знаки. Отрицательный ноль (`Decimal("-0.00")`) читается как `Decimal("0.00")`: у
нуля в десятичной колонке знака нет.

### <a id="decimal-field-sqlite"></a>`DecimalField` (SQLite)

В SQLite нет точного десятичного типа, поэтому колонки для `DecimalField` там нет: модель с таким полем
даёт `UnSupportedError` при привязке к соединению SQLite, как и запрос к SQLite со значением `Decimal`
или с `Cast()` к десятичному типу — до любого SQL. Храните деньги в целых младших единицах (`IntField`
или `BigIntField` копеек) в SQLite либо используйте PostgreSQL или ClickHouse, которые хранят точные
десятичные числа. [inspectdb](../migrations/inspectdb.ru.md) читает колонку SQLite `DECIMAL` или `NUMERIC`
как `FloatField`: у такой колонки в SQLite числовое сродство (NUMERIC affinity), которое хранит целое
или 64-битное число с плавающей точкой.

### <a id="date-and-time-values"></a>Дата и время

`DatetimeField` принимает `datetime`, `date` (её первый момент в настроенном поясе при `use_timezone=True` —
полночь или конец пропущенного часа при переводе часов, если он начинается в полночь, как 2024-09-08 в
`America/Santiago`; так же и при сравнении даты с `DatetimeField`), строку ISO 8601, начинающуюся с полной
даты (`2024-05-01`, `2024-05-01T10:00:00+03:00`). `DateField` принимает `date`, `datetime` (берётся его дата)
или такую строку. Всё остальное — число (секунды от начала эпохи Unix тоже), просто `"2024"`, неразборчивый
текст — даёт `ValidationError` при создании объекта и при любой записи (`create()`, `bulk_create()`, `save()`,
`update()`, `bulk_update()`, `update_or_create()`), одинаково на всех диалектах. Так же ведёт себя `datetime`,
который выходит за пределы диапазона после перевода в настроенный пояс или в UTC. Операторы фильтра вроде
`__year=2024` сравнивают извлечённое число с `int` (текст, `float` или `Decimal` дают `UnSupportedError`, как
любой оператор фильтра, получивший аргумент другого вида). `TimeField` работает и в SQLite и ClickHouse:
значение хранится текстом ISO (`12:30:45`, у времени с поясом — со своим смещением, `12:30:45+03:00`), а
сравнения, сортировка и `Min`/`Max` сравнивают этот текст, как это делает база: там `10:00+03:00` идёт после
`08:00+00:00`, хотя это более ранний момент, который `TIMETZ` PostgreSQL упорядочивает по времени в UTC. Чтобы
упорядочивать моменты, храните их в `DatetimeField`.
При `use_timezone=False` дата-время раньше 1970 года переводится в местное время системы и обратно даже в
Windows, где `datetime.astimezone()` этого не умеет (там используется пояс, который называет необязательный
пакет `tzlocal`, а без него — смещение системы на 1970-01-02). В PostgreSQL значение без пояса при
`use_timezone=False` передаётся как момент, который соответствует этому местному времени на часах, поэтому
`asyncpg` и `rust_pg` записывают и читают одно и то же. `DatetimeField`, описывающий уже существующую колонку
PostgreSQL типа `timestamp` (без пояса), читает её как время на часах в UTC и записывает время значения на
часах в UTC на обоих драйверах — так же, как сам PostgreSQL переводит `timestamp` в `timestamptz` в сеансе
hare, где действует пояс UTC.

### <a id="extreme-dates"></a>Крайние даты (PostgreSQL)

`datetime.min`/`datetime.max` и `date(1, 1, 1)`/`date(9999, 12, 31)` хранятся на любой базе как те дни и
моменты, которыми они являются, — и в PostgreSQL (через `asyncpg` и через `rust_pg`), а не как
`-infinity`/`infinity`, — поэтому `__year`/`__month`/`__day`, `Extract`/`Trunc` и
`F("dt") ± timedelta(...)` с ними работают. `infinity`/`-infinity` в PostgreSQL, записанные иначе,
читаются как те же крайние значения Python: `date.max`/`date.min`, `datetime.max`/`datetime.min`. В поясе
восточнее UTC `datetime.max` (UTC) при показе в нём выходит за диапазон datetime и отклоняется с
`ValidationError`.

### <a id="json-values"></a>Значения JSON

`JSONField` даёт `ValidationError` (а не просто `ValueError`/`TypeError`), если значение не удаётся
преобразовать в JSON, если из колонки прочитан испорченный JSON или если значение не соответствует
объявленному `field_type`. Объявленный `field_type` (модель Pydantic, `list[Model]`, `list[str]` и т. п.)
применяется через `TypeAdapter` из Pydantic при присваивании, при `save()` и при чтении: словарь (или
список словарей) сразу становится моделью, поэтому в памяти лежит то же значение, что вернёт последующее
чтение, а элементы списка проверяются по одному. `pydantic_model_creator()` добавляет этот тип в схему.
Без `field_type` (в том числе с обобщённым `JSONField[T]`, который действует только для проверки типов)
значения остаются обычным JSON.

Значение, которое JSON не может хранить, даёт `ValidationError` при любой записи (`create()`, `save()`,
`update()`, `bulk_create()`, в том числе с `use_copy=True`, `bulk_update()`): число `NaN` или `Infinity` где
угодно внутри значения. Нулевой байт внутри значения JSON хранится так, как его хранит база, там, где она его
вмещает; PostgreSQL его не принимает, и hare даёт `ValidationError`. Целое число любого размера сохраняется и
читается точно — число вне 64-битного диапазона, с которым работает orjson, кодирует и раскодирует стандартная
библиотека. `datetime`, `date`, `time` без пояса и `UUID` внутри значения записываются одинаковым текстом
ISO/UUID по обоим путям, в том числе когда orjson не установлен вовсе.

`exact`/`not` по всему `JSONField` сравнивают так, как сравнивает база: PostgreSQL сравнивает значения
`jsonb` (порядок ключей и разница между `1` и `1.0` не важны), SQLite сравнивает хранимый текст (они
важны), колонку ClickHouse `JSON` сравнивает сам ClickHouse после приведения значения к `JSON` (порядок
ключей не важен), а колонка `String` с текстом JSON сравнивается как текст.

В SQLite колонка `JSONField` объявляется как `JSON_TEXT` (текстовое сродство), поэтому число на верхнем
уровне хранится своим текстом JSON: `12345678901234567890` и `1.0` читаются без изменений.

`JSONB` в PostgreSQL хранит число как `numeric` и пишет его без экспоненты, поэтому вещественное число в
экспоненциальной записи без дробной части читается обратно как `int` того же значения: `1e22` — это
`10000000000000000000000`, `1e300` — `int` из 301 цифры. `1.5e-7` и `3.0` читаются как вещественные.

### <a id="generatedfield"></a>`GeneratedField`

```python
class GeneratedField:
    def __init__(
        self,
        expression: Expression | RawSQLTerm | dict[str, RawSQLTerm],
        output_field: Field[Any],
        stored: bool = True,
        **kwargs: Any,
    ) -> None: ...
```

Колонка, которую база вычисляет по выражению над строкой, — `GENERATED ALWAYS AS (...) STORED` или
`VIRTUAL`, — поэтому её значение верно после любой записи, которую видит база, в том числе сделанной не
через hare:

```python
class Book(Model):
    price = fields.DecimalField(max_digits=10, decimal_places=2)
    quantity = fields.IntField()
    stock_value = fields.GeneratedField(
        F("price") * F("quantity"), output_field=fields.DecimalField(max_digits=12, decimal_places=2)
    )
```

- `expression` — выражение hare над полями модели (`F`, функции, арифметика): DDL каждой базы пишет его
  своим SQL, миграция, переименовывающая поле, которое оно читает, переименовывает его и здесь, а миграция,
  меняющая тип колонки, которую оно читает, удаляет вычисляемую колонку до изменения и добавляет после.
  Или `RawSQLTerm` с SQL, который записывается в команды DDL как есть (**никогда не собирайте его из
  данных, пришедших извне**), или своё выражение для каждого диалекта:
  `{"sqlite": RawSQLTerm(...), "postgresql": RawSQLTerm(...)}`; диалект, которого нет в словаре, даёт
  `ConfigurationError`, как и обычный текст. Готовый SQL — ваш: hare не переименовывает в нём поле и не
  читает, какие колонки он использует, — миграцию, меняющую такую колонку, отклоняет база там, где она не
  меняет колонку под вычисляемой.
- `output_field` — поле, задающее тип колонки и то, как её значение читается, сравнивается, фильтруется и
  описывается в схеме.
- `stored=True` вычисляет значение при записи; `stored=False` — колонка `VIRTUAL`, вычисляемая при
  чтении, — работает в SQLite и PostgreSQL 18, а более старый сервер PostgreSQL даёт `UnSupportedError`
  (`Features.supports_virtual_generated_columns`).

hare никогда не пишет в эту колонку: `INSERT` читает вычисленное значение обратно, `UPDATE` читает его заново
(на базе с `RETURNING`). Значение для этого поля даёт `QueryError` — в конструкторе, `create()`,
`get_or_create()`, `update_or_create(defaults=...)`, `save(update_fields=[...])` и `update()`; копию
экземпляра делает `obj.clone()`. У нового экземпляра — и у копии — значения этого поля нет, пока его строка не
записана: чтение даёт `NoValuesFetched`, как и чтение после записи, которая ничего не читает обратно
(`bulk_create()` без `returning=True`, база без `RETURNING`), пока его не прочитает `refresh_from_db()`.
Значение, присвоенное загруженному экземпляру, при `save()` заменяется
вычисленным; `bulk_update()` перечитывает новое значение только с `returning=True`. Оно не может быть
первичным ключом и не принимает `validators=` (`ConfigurationError` — задайте их у `output_field`);
`sensitive` оно наследует от `output_field`, если не передано своё значение.

### <a id="format-fields"></a>Поля адреса почты, URL, слага и телефона

```python
class Author(Model):
    email = fields.EmailField(unique=True)
    homepage = fields.URLField(null=True)
    slug = fields.SlugField(unique=True)
    phone = fields.PhoneField(null=True, region="US")
```

```python
class EmailField:
    def __init__(self, max_length: int = 254, *, lowercase: bool = False, **kwargs: Any) -> None: ...

class URLField:
    def __init__(
        self,
        max_length: int = 2048,
        *,
        schemes: Sequence[str] = ("http", "https"),
        **kwargs: Any,
    ) -> None: ...

class SlugField:
    def __init__(self, max_length: int = 50, *, allow_unicode: bool = False, **kwargs: Any) -> None: ...

class PhoneField:
    def __init__(self, *, region: str | None = None, **kwargs: Any) -> None: ...
```

Каждое из них — `CharField` (`VARCHAR(max_length)`, `MaxLengthValidator`), которое проверяет формат
значения и приводит его к одному виду записи на каждом пути записи — `create()`, `save()`, `update()`,
`bulk_create()` (и с `use_copy=True`), `bulk_update()`, `update_or_create()` — и в каждом значении
фильтра, которое сравнивается с ним целиком (`email=`, `email__in=`, `email__not=`). Значение, не
прошедшее проверку, даёт `ValidationError` с именем поля: `email: Invalid email address`. Текстовые
операторы (`__contains`, `__startswith`, `__icontains`, ...) принимают любой текст. Валидаторы из
`validators=` тоже выполняются, до проверки формата. Объект хранит значение таким, каким его присвоили,
пока оно не прочитано из базы заново; в базе лежит записанный вид.

| Поле | Принимает | Записывает |
|---|---|---|
| `EmailField` | то, что принимает `EmailValidator`: локальную часть из слов через точку (`ann.lee+news`) или строку в кавычках (`"ann@home"`), `@`, затем имя хоста с точкой (`example.com`) или адрес в квадратных скобках (`[192.0.2.1]`); не длиннее 320 символов | домен в нижнем регистре — `Ann@Example.COM` становится `Ann@example.com`; с `lowercase=True` весь адрес, `ann@example.com` |
| `URLField` | то, что принимает `URLValidator`, со схемой из `schemes` (сравнивается в нижнем регистре, `HTTPS://` — это `https`): имя хоста с точкой, `localhost`, адрес IPv4 или IPv6, необязательный порт, данные пользователя, путь, запрос и фрагмент | как дано |
| `SlugField` | латинские буквы, цифры, дефис и подчёркивание (`my-first_post`); с `allow_unicode=True` — ещё любые буквы и цифры Юникода (`привет-мир`) | как дано |
| `PhoneField` | без пакета `phonenumbers` — только номер в виде E.164: `+`, код страны не с 0, всего не больше 15 цифр; с ним (`pip install hare-orm[phone]`) — номер в любом виде, который пакет разбирает и считает верным: `+1 (650) 253-0000`, а с `region` — и номер внутри страны, `(650) 253-0000` | E.164: `+16502530000` (`VARCHAR(16)`) |

Поэтому значение фильтра находится в каком бы виде его ни передали:
`Author.objects.filter(email="Ann@EXAMPLE.com")` находит `Ann@example.com`,
`filter(phone="(650) 253-0000")` находит `+16502530000` у поля с `region="US"`. Локальная часть адреса
сохраняет регистр — почтовый сервер может отличать `Ann` от `ann`; если ваши адреса от регистра не
зависят, задайте `lowercase=True` или сравнивайте через `email__iexact=`.

- `schemes` — имена схем в нижнем регистре.
- `SlugField` индексируется, если не передан `db_index=False`. Файл миграции пишет для него
  `db_index=True`, а поле, объявленное в файле миграции без `db_index`, индекса не имеет — то же правило,
  что у `ForeignKeyField`.
- `region` — код региона, который знает `phonenumbers` (`"US"`, `"GB"`, его `SUPPORTED_REGIONS`);
  `region` без установленного `phonenumbers` даёт `ConfigurationError`. Какие номера `phonenumbers`
  считает верными, задают его собственные данные, поэтому это может меняться с его версией.

Схема Pydantic (`pydantic_model_creator()`) и построенный по ней OpenAPI описывают их через
`format: email`, `format: uri`, `pattern` символов слага, а `PhoneField` без `phonenumbers` — через
`pattern` номера E.164 (с ним номер принимается в любом виде).

Превращение `CharField` в одно из этих полей — `AlterField`, который оставляет колонку (`SlugField`
добавляет свой индекс): уже записанные строки не проверяются и не приводятся — это случится при их
следующей записи.

### <a id="enum-fields"></a>Поля-перечисления

Это не классы, а функции, которые возвращают `IntEnumFieldInstance` (это `SmallIntField`) или
`CharEnumFieldInstance` (это `CharField`), поэтому `type(Model.status) is not IntEnumField`:

```python
def IntEnumField(
    enum_type: type[IntEnumType],
    description: str | None = None,
    **kwargs: Any,
) -> IntEnumType: ...

def CharEnumField(
    enum_type: type[CharEnumType],
    description: str | None = None,
    max_length: int = 0,
    **kwargs: Any,
) -> CharEnumType: ...
```

- `IntEnumField` построено на `SmallIntField` — значения перечисления должны помещаться в `int16`
  (1..32767 при `generated=True`, иначе весь диапазон `int16`).
- `CharEnumField` построено на `CharField` — при `max_length=0` длина берётся по самому длинному значению
  перечисления. **Если позже вы добавите более длинное значение, увеличьте `max_length` сами** — оно не
  пересчитывается. Перечисление с нестроковыми значениями (например, `ONE = 1`) тоже работает: в колонку
  пишется `str(member.value)` (`"1"`), а при чтении текст превращается обратно в элемент перечисления.
- Оба сами составляют `description` в виде `"NAME: value"` со всеми элементами, если вы не передали своё.
- Оба отклоняют объявление, которое не может хранить своё перечисление (`ConfigurationError`): класс,
  который не является перечислением, а у `CharEnumField` — `max_length` короче самого длинного значения и
  `default`, который не элемент и не значение перечисления.
- Число, которому не соответствует ни один элемент, — `7` или дробное вроде `1.5`, — даёт
  `ValidationError` и при записи, и в фильтре; так же и целое вещественное (`1.0`) для `IntEnumField`,
  которое, как целое поле, принимает только `int`.
- Там, где колонка хранит любое целое или текст (PostgreSQL, SQLite), `CHECK` ограничивает её значениями,
  которыми хранятся элементы: значение без элемента, записанное в обход поля, — готовым SQL или вычисленное
  `update(level=F("level") + 1)`, — даёт `IntegrityError`. Это `CheckConstraint` с
  `EnumMembersCondition` по имени `<поле>_enum_members`, который добавляется к ограничениям модели при её
  привязке: `makemigrations` пишет его вместе с моделью и заменяет, когда меняются элементы. Колонке
  перечислимого типа он не нужен — `Enum8`/`Enum16` ClickHouse (`Features.keeps_enum_columns`),
  `NativeEnumField` PostgreSQL.

```python
from enum import Enum

class Status(Enum):
    DRAFT = "draft"
    PUBLISHED = "published"

class Book(Model):
    status = fields.CharEnumField(Status, default=Status.DRAFT)
```

### <a id="compositeprimarykey"></a>`CompositePrimaryKey`

```python
class CompositePrimaryKey:
    def __init__(self, *field_names: str, without_overlaps: bool = False) -> None: ...
```

Это не настоящее поле — своей колонки у него нет. Оно объявляет первичным ключом таблицы два или больше
уже объявленных полей:

```python
class Shelf(Model):
    store = fields.CharField(max_length=20)
    number = fields.IntField()
    pk = CompositePrimaryKey("store", "number")
```

Каждое названное поле должно существовать и само **не** должно быть `primary_key=True`, `generated=True`
или `null=True`, а других полей с `primary_key=True` у модели нет (`ConfigurationError`). Связь модели с
самой собой тоже не может быть частью ключа: её ключ содержал бы саму связь. Не может им быть и
значение-контейнер — `ArrayField`, `MapField`, `TupleField`, `NestedField` или `JSONField`, ни в
составном ключе, ни ключом целиком: ORM держит прочитанные и записанные строки по их ключу
(`ConfigurationError`); такие поля делайте уникальными через `UniqueConstraint`. `Model.pk` у такой
модели — кортеж. `clone()` вычисляет `default=`/`db_default=` каждой части ключа отдельно, поэтому в
`pk=(...)` явно указываются только части, у которых нет ни того, ни другого.

Частью ключа может быть внешний ключ — по своему имени или по имени колонки ключа:
`CompositePrimaryKey("book_id", "number")` и `CompositePrimaryKey("book", "number")` — один и тот же
ключ. Его место занимает колонка ключа связи (связь с моделью с составным ключом добавляет каждую его
колонку — `edition_book_id`, `edition_number`); `Model.pk` хранит значение (значения) ключа связанной
модели, их же принимают `pk=` и `pk__in=`. Связь не может быть `null=True`, а `ManyToManyField` не может
быть частью ключа (`ConfigurationError`):

```python
class Edition(Model):
    book = fields.ForeignKeyField("models.Book", related_name="editions")
    number = fields.IntField()
    pk = CompositePrimaryKey("book_id", "number")

await Edition.objects.get(pk=(book.id, 2))
```

`without_overlaps=True` делает последнее поле диапазоном (или мультидиапазоном), в котором две строки с
равными остальными полями не могут пересекаться, — одна бронь читального зала в каждый момент; это
проверяет база (`PRIMARY KEY (room, booked WITHOUT OVERLAPS)`, PostgreSQL 18):

```python
class RoomBooking(Model):
    room = fields.CharField(max_length=20)
    booked = DateRangeField()  # hare.dialects.postgresql.fields
    reader = fields.ForeignKeyField("models.Reader")
    pk = CompositePrimaryKey("room", "booked", without_overlaps=True)
```

Более старый сервер PostgreSQL и SQLite дают `UnSupportedError` до любых команд DDL
(`Features.supports_without_overlaps`); скалярному полю перед диапазоном нужно расширение `btree_gist`,
его добавляют редактор схемы и автодетектор миграций. Внешний ключ не может ссылаться на такой ключ
(`ConfigurationError`), а смену `without_overlaps` у существующей таблицы автодетектор не находит — для
неё нужна миграция, написанная вручную. Цель `ON CONFLICT` по такому ключу PostgreSQL не выводит и
конфликтующую с ним строку не обновляет, поэтому `bulk_create(on_conflict=[...])` с его полями и
`update_fields` с `on_conflict_constraint="pk"` дают `QueryError`; конфликтующие строки пропускает
`ignore_conflicts=True` — без цели или с `on_conflict_constraint="pk"`. С
`UniqueConstraint(without_overlaps=True)` так же, по его `name`.

> [!NOTE]
> Внешний ключ **может** ссылаться на модель с составным первичным ключом — см.
> [Связи](relations.ru.md#targeting-a-composite-primary-key) — с настоящим ограничением `FOREIGN KEY` на
> уровне таблицы, каскадами, соединением таблиц и `prefetch_related()`.

## <a id="postgresql-only-fields"></a>Поля отдельных баз данных

`HStoreField`, `TSVectorField`, `PostGISField`, `CitextField` и поля диапазонов находятся в
`hare.dialects.postgresql` — см. [Поля PostgreSQL](../dialects/postgresql/fields.ru.md). Из `hare.fields`
`ArrayField` работает в PostgreSQL и ClickHouse, `MapField`, `TupleField` и `NestedField` — в ClickHouse,
а `IPAddressField` — везде (`inet` в PostgreSQL, `IPv6` в ClickHouse, текст в остальных). См. [Типы
ClickHouse](../dialects/clickhouse/types.ru.md). Адреса сравниваются, сортируются и агрегируются
(`Min`/`Max`) так, как каждая база упорядочивает свою колонку: `inet` PostgreSQL ставит адрес IPv4 раньше
любого IPv6, затем упорядочивает по байтам адреса; SQLite сравнивает текст; ClickHouse упорядочивает свои
значения `IPv6` по-своему. Текстовые операторы (`__startswith`) сопоставляют адрес IPv4 по его
тексту IPv4, и в форме `::ffff:` ClickHouse тоже. Адрес IPv6 с зоной (`fe80::1%eth0`) даёт
`ValidationError` в PostgreSQL и ClickHouse, колонки адресов которых зону не хранят; SQLite сохраняет её
в тексте. `VectorField` находится в `hare.vectors` и работает в PostgreSQL и SQLite — см. [Поиск по
векторам](../dialects/search-and-geodata/vector-search.ru.md).

## <a id="writing-your-own-field"></a>Свой класс поля

См. [Свои поля](../extending/custom-fields.ru.md).
