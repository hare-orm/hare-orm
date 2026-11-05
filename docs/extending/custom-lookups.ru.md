# Свои операторы фильтра

`Field.register_lookup()` добавляет классу поля и его подклассам собственное окончание фильтра проекта —
`field__reversed=...` — для всех диалектов или для одной базы.

```python
class Field:
    @classmethod
    def register_lookup(
        cls,
        lookup_name: str,
        lookup: LookupFunction | None = None,
        *,
        value_shape: LookupValueShape = LookupValueShape.VALUE,
        value_type: Any = None,
        required_extension: str | None = None,
    ) -> Callable[[LookupFunction], LookupFunction] | None: ...
```

## <a id="arguments"></a>Аргументы

- Вызывать можно в любой момент. После `Hare.init()` он сбрасывает кэши, построенные по реестрам, поэтому
  следующий запрос уже знает новый оператор.
- `lookup_name` — окончание без ведущих `__`: например, `"reversed"` регистрирует
  `field__reversed=...`.
- `lookup` — `(field) -> FieldLookup` (`hare.query.filters.FieldLookup`). Его `operator` строит условие
  как `(term, encoded_value)`; необязательный `value_encoder` преобразует значение фильтра как
  `(value, model, field, dialect)`, где `dialect` — диалект подключения, на котором выполняется запрос, а
  без него значение преобразует поле — так же, как свои значения; все атрибуты — в таблице ниже. Через
  связи оператор работает сам: `author__name__<lookup>` присоединяет `author` и применяет его к `name`.
- Чтобы кэш привязывал новые значения, преобразуйте вход в `value_encoder`, а оператором
  стройте условие с результатом преобразования. Оператор, самостоятельно выводящий новые
  значения, выполняется обычным путём без повторного построения условия внутри binder.
- `value_shape` — значение фильтра одно (`LookupValueShape.VALUE`), список (`LIST`) или диапазон из двух
  элементов (`RANGE`). Если у оператора `LIST`/`RANGE` нет своего `value_encoder`, каждый элемент
  преобразует поле — так же, как собственные `__in`/`__range` поля.
- `value_type` — тип значения (каждого элемента списка или диапазона); `None` означает тип самого поля.
- `required_extension` — расширение базы, которое нужно оператору; `None` — никакое.
- `value_shape`, `value_type` и `required_extension` — это то, что сообщает об операторе
  [`get_lookup_info()`](../querying/describing-filters.ru.md).
- Оператор, зарегистрированный на самом `Field`, доступен полям и выражениям без известного выходного
  поля. Типизированные аннотации передают своё выходное поле, включая типизированный путь JSON; `None`
  передаётся только при неизвестном поле. Поддержка окончаний пути определяется его объявлением
  операторов. Триграммные операторы PostgreSQL зарегистрированы на `Field`.
- Можно вызвать обычной функцией или использовать как декоратор (без `lookup`).

## <a id="one-database"></a>Оператор одной базы

У оператора фильтра, который выполняет только одна база, функция-оператор помечена
`DialectImplementedOperators.mark` (`hare.query.filters.lookups.dialect_implemented_operators`), а
реализует её диалект этой базы — `dialect.filter_operators.register_operator(operator, implementation)`,
где `implementation` — сама функция, если она уже строит SQL этой базы. Запрос на другом диалекте даёт
`UnSupportedError` ещё до построения SQL, а `get_lookup_info()` сообщает, что там оператор не
поддерживается:

```python
from hare import fields
from hare.dialects.dialect_registry import DialectRegistry
from hare.query.enums import LookupValueShape
from hare.query.filters import FieldLookup
from hare.query.filters.lookups.dialect_implemented_operators import DialectImplementedOperators
from hare.query.filters.lookups.lookups import Lookups


@DialectImplementedOperators.mark
def between_on_postgresql(term, value):
    return Lookups.between(term, value)


fields.IntField.register_lookup(
    "within_on_postgresql",
    lambda field: FieldLookup(between_on_postgresql),
    value_shape=LookupValueShape.RANGE,
)
DialectRegistry.get_dialect("postgresql").filter_operators.register_operator(
    between_on_postgresql, between_on_postgresql
)
```

Функция-оператор сравнивается по тождеству: это функция модуля, а не функция, создаваемая заново при
каждом вызове `lookup`. Операторы триграмм PostgreSQL и `__global_in` ClickHouse — операторы одной базы.

## <a id="fieldlookup"></a>`FieldLookup`

`FieldLookup` (`hare.query.filters.FieldLookup`) — dataclass:

| Атрибут | Что это |
|---|---|
| `operator` | Строит условие — `(term, encoded_value) -> Criterion`. |
| `value_encoder` | Преобразует значение фильтра — `(value, model, field, dialect)`; `None` — поле преобразует его так же, как свои значения. |
| `array_element_field` | Поле, которым передаётся каждый элемент значения-списка. |
| `array_element_fields` | Для каждой колонки ключа — поле, которым передаётся каждый элемент списка строк ключа. |
| `text_function` | Превращает значение в текст, с которым сравнивает текстовый оператор. |
| `compares_json_path_text` | Сравнивает ли оператор текст значения по пути JSON. |
| `is_tsvector`, `search_config` | Сравнивает ли `__search` хранимый вектор полнотекстового поиска как есть, и конфигурация этого вектора. |
| `requires_search_query_config` | Требует явно задать конфигурацию поискового запроса, если у вычисляемого вектора нет общей конфигурации. |
| `searched_field` | Поле, по которому ищет `__search`, — диалект, ищущий через полнотекстовый индекс, находит по нему индекс поля. |
| `required_feature` | Флаг `Features`, нужный подключению для этого оператора; на подключении без него запрос даёт `UnSupportedError` до отправки SQL. |

`lookup.with_changes(**changes)` — копия с заменёнными атрибутами.

Встроенные операторы LIKE объявляют параметр шаблона при построении SQL.
`LikePatternParameter` хранит исходный wrapper и флаги подстановки в начале и конце;
SQL-преобразование регистра может оборачивать этот источник, не меняя привязку.
Созданный вручную `Like` без такого объявления не получает встроенную привязку шаблона.
Сравнения типизированных контейнеров сохраняют источник параметра и преобразование поля.
При повторном использовании плана преобразуется только новое Python-значение;
SQL-приведение контейнера заново не строится.

## <a id="examples"></a>Примеры

```python
from hare.fields import CharField
from hare.query.filters import FieldLookup

def _reversed_lookup(field: CharField | None) -> FieldLookup:
    def _operator(term, value: str):
        return term == value[::-1]
    return FieldLookup(_operator)

CharField.register_lookup("reversed", _reversed_lookup)
# .filter(name__reversed="oom")
```

Встроенные окончания (`in`, `gte`, `icontains` и другие) всегда важнее: свой оператор никогда
случайно не заменит один из них.
