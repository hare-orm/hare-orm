# Свои поля

Унаследуйте `hare.fields.base.Field` (он же `hare.fields.Field`). Обычно переопределяют:

- `field_type` — тип значения поля в Python (обычно задаётся через параметр базового класса, как в
  Django: `class MoneyField(Field[int]):`).
- `SQL_TYPE` — строка или `@property`.
- `to_db_value(self, value, instance)` / `from_db_value(self, value)` — своё преобразование при
  записи в базу и чтении из неё.
- `to_python(self, value)` — приводит к нужному виду значение, только что
  переданное в `Model(**kwargs)`. Нужен, только если такое значение должно проходить ту же
  обработку, что `from_db_value` делает для прочитанного из базы (см. ниже); если пара
  `to_db_value`/`from_db_value` несимметрична (шифрование, маскирование и т. п.), не трогайте его.
- `deconstruct()` — для автоматического создания миграций, если поле принимает аргументы
  конструктора помимо базовых.
- `migration_import_path` — неизменный путь к классу через точку, если модуль поля может переехать.

```python
from hare.fields.base import Field


class MoneyField(Field[int]):
    """Хранит сумму в копейках целым числом."""

    field_type = int
    SQL_TYPE = "BIGINT"

    def to_db_value(self, value, instance):
        self.validate(value)
        return None if value is None else int(value)

    def from_db_value(self, value):
        return None if value is None else int(value)

```

## Только что присвоенные значения: `to_python` {: #to-python-value-on-assign }

При создании объекта через `Model(**kwargs)`/`Model.objects.create(**kwargs)` каждое значение проходит
через `to_python(self, value)`, а не через `from_db_value()`: значение,
прочитанное из базы, и значение, которое только что передал вызывающий код, — разные вещи, хотя
обрабатывать их часто нужно одинаково. По умолчанию метод возвращает значение без изменений — это
безопасно для любого поля, у которого пара `to_db_value`/`from_db_value` не является точным и
повторяемым без вреда кодированием и раскодированием уже готового значения Python.

Переопределите `to_python()` (обычно достаточно вызвать
`self.from_db_value(value)` — то же преобразование, что получает значение из базы), если только
что присвоенному значению действительно нужно такое же приведение, например разбор строки ISO в
настоящий `datetime`.

**Не** переопределяйте его (оставьте поведение по умолчанию), если `to_db_value`/`from_db_value`
несимметричны — кодирование и раскодирование, которые не отменяют друг друга для уже
раскодированного значения, как при шифровании или маскировании. Если применить `from_db_value`
(раскодирование) к значению, которое никогда не проходило через `to_db_value`, оно тихо испортится в
памяти, а следующий `save()` закодирует *уже испорченное* значение; у поля с шифрованием это значит,
что в базу запишется открытый текст вместо шифротекста.

```python
class EncryptedField(Field[str]):
    field_type = str
    SQL_TYPE = "TEXT"

    def to_db_value(self, value, instance):
        self.validate(value)
        return None if value is None else encrypt(value)

    def from_db_value(self, value):
        return None if value is None else decrypt(value)

    # to_python() не переопределён, и это правильно: Model(secret="hello") должен
    # хранить в памяти "hello", а не вызывать decrypt() для значения, которое никто не шифровал.
```

## Хранение на разных диалектах {: #per-dialect-storage }

У каждого диалекта есть реестр типов: для класса поля он задаёт, как поле хранится, — тип колонки,
команду создания первичного ключа, который генерирует база, и приведение типа, которым колонка
оборачивается везде, где её сравнивают или сортируют. Класс поля использует запись ближайшего
зарегистрированного базового класса, поэтому `MoneyField` выше везде получает `BIGINT`, пока диалект
не зарегистрирует для него что-то другое:

```python
from hare.dialects.base.types import TypeMapping
from hare.dialects.registry import DialectRegistry

DialectRegistry.get_dialect("sqlite").types.register(MoneyField, TypeMapping(column_type="INTEGER"))
```

Поле, которое существует только на некоторых диалектах, перечисляет их в `SUPPORTED_DIALECTS`;
создание таблицы на другом диалекте даёт `UnSupportedError`:

```python
class LtreeField(Field[str]):
    SQL_TYPE = "ltree"
    SUPPORTED_DIALECTS = frozenset({"postgresql"})
```

Результат читают `field.get_column_type(dialect)`, `field.get_generated_sql(dialect)` и
`field.get_function_cast(dialect)`; поле, которое вычисляет их из собственных настроек,
переопределяет эти методы.

## Операторы фильтра, пути внутри значения и вычисляемые колонки {: #lookups-value-paths-and-generated-columns }

Поле, значение которого — не простое скалярное значение, объясняет hare, как по нему фильтровать,
переопределяя эти методы `Field`. Так устроены собственные поля массивов, диапазонов и hstore в
hare, поэтому стороннее поле получает те же возможности без изменений в самом hare:

| Метод / атрибут | Что даёт |
|---|---|
| `get_lookups()` | Операторы фильтра поля по окончанию (`""` — обычное равенство), каждый — `FieldLookup`, как у `register_lookup()`; по умолчанию общий набор. Поле со своим набором возвращает его вместо общего, и фильтр с окончанием не из него даёт `FieldError`. Операторы из `register_lookup()` добавляются к нему. |
| `get_path_transform(segment)` | Как часть пути после имени поля читается внутри его значения — `(функция, строящая выражение из выражения поля, поле прочитанного значения)` или `None`. Этому следуют фильтры (`tags__0__startswith`), `F()`, `values()` и `order_by()`. |
| `get_lookup_value_description(lookup)` | `(LookupValueShape, тип)` значения оператора, если это не значение типа поля (`contains` у массива принимает список элементов), — для [`get_lookup_info()`](../querying/describing-filters.ru.md); иначе `None`. |
| `get_like_text_function()` | Как значение превращается в текст для `contains`/`icontains`/`startswith`/... и операторов по шаблону; `None` приводит его к `VARCHAR`. |
| `get_generated_from_field_names()` / `with_renamed_generated_from_field(old, new)` | Другие поля модели (по имени), из которых вычисляется генерируемая колонка: миграция не даст удалить такое поле, пока эта колонка его читает, а переименование поля переименовывает его и здесь. |
| `holds_container_value` | `True` для массива, диапазона или значения вроде JSON: вычисляемое значение такого поля получает его собственные операторы фильтра. |

```python
import operator
from functools import partial

from hare.fields import CharField
from hare.query.filters import FieldLookup
from hare.sql.terms import Function

class CodeField(CharField):
    def get_lookups(self):
        def starts_with(term, value):
            return term.like(f"{value}%")

        return {"": FieldLookup(operator.eq), "prefix": FieldLookup(starts_with)}

    def get_path_transform(self, segment):
        if segment == "upper":
            return partial(Function, "UPPER"), self
        return None
```

Диалект может добавить часть пути к классам полей, которые определил не он, — как `name__unaccent`
у текстовых полей в PostgreSQL, — через
`КлассПоля.register_transform(segment, get_transform, required_extension=...)` — `get_transform(field)`
возвращает ту же пару, что и `get_path_transform()`, — в своём `Dialect.install()`, который
выполняется один раз при регистрации диалекта.
