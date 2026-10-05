# Свои операторы фильтра

`Field.register_lookup()` добавляет классу поля и его подклассам собственное окончание фильтра
проекта — `field__within_km=...` — для всех диалектов или для некоторых.

```python
@classmethod
def register_lookup(
    cls,
    lookup_name: str,
    lookup: Callable[[Field | None], FieldLookup] | None = None,
    *,
    value_shape: LookupValueShape = LookupValueShape.VALUE,
    value_type: Any = None,
    dialects: Iterable[str] | None = None,
    required_extension: str | None = None,
) -> Callable[[Callable], Callable] | None
```

- Вызывать можно в любой момент. После `Hare.init()` он сбрасывает кэши, построенные по реестрам,
  поэтому следующий запрос уже знает новый оператор.
- `lookup_name` — окончание без ведущих `__`: например, `"within_km"` регистрирует
  `field__within_km=...`.
- `lookup` — `(field) -> FieldLookup` (`hare.query.filters.FieldLookup`). Его `operator` строит
  условие как `(term, encoded_value)`; необязательный `value_encoder` преобразует значение фильтра
  как `(value, model, field, dialect)`, где `dialect` — диалект подключения, на котором выполняется
  запрос, а без него значение преобразует поле — так же, как свои значения; все атрибуты — в таблице ниже.
  Через связи оператор работает сам: `author__name__<оператор>`
  присоединяет `author` и применяет его к `name`.
- `binds_by_rebuild` у `FieldLookup` — для оператора, который выводит то, что сравнивает, из значения,
  а не сравнивает закодированное значение как есть. План запроса тогда подставляет следующее значение,
  заново строя из него условие. Текст SQL, который строит оператор, должен зависеть от значения только
  через его тип (и длину списка, ключи словаря): они входят в ключ плана, остальное подставляется. Без
  этого такой оператор плана не хранит
  ([кэш планов запросов](../querying/query-plan-cache.ru.md#correctness-notes)).
- `value_shape` — значение фильтра одно (`LookupValueShape.VALUE`), список (`LIST`) или диапазон из
  двух элементов (`RANGE`). Если у оператора `LIST`/`RANGE` нет своего `value_encoder`, каждый
  элемент преобразует поле — так же, как собственные `__in`/`__range` поля.
- `value_type` — тип значения (каждого элемента списка или диапазона); `None` означает тип самого
  поля.
- `dialects` — имена диалектов, на которых оператор работает; `None` — на всех. Запрос на другом
  диалекте даёт `UnSupportedError` ещё до построения SQL.
- `required_extension` — расширение базы, которое нужно оператору; `None` — никакое.
- `value_shape`, `value_type`, `dialects` и `required_extension` — это то, что сообщает об
  операторе [`get_lookup_info()`](../querying/describing-filters.ru.md).
- Оператор, зарегистрированный на самом `Field`, принадлежит любому значению: каждому полю и
  значению без поля — вычисляемому значению (`annotate(total=...).filter(total__<оператор>=...)`),
  пути в JSON; для них `lookup` вызывается с `None` вместо поля. Так зарегистрированы триграммные
  операторы PostgreSQL.
- Можно вызвать обычной функцией или использовать как декоратор (без `lookup`).

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
| `searched_field` | Поле, по которому ищет `__search`, — диалект, ищущий через полнотекстовый индекс, находит по нему индекс поля. |
| `required_feature` | Флаг `Features`, нужный подключению для этого оператора; на подключении без него запрос даёт `UnSupportedError` до отправки SQL. |
| `binds_by_rebuild` | См. список выше. |

`lookup.with_changes(**changes)` — копия с заменёнными атрибутами.

Два примера — первый показывает, как `PostGISField` регистрирует свой оператор `within_km`:

```python
@staticmethod
def _within_km_lookup(field: Field | None) -> FieldLookup:
    def _encode_within_km_value(value, model, field_object, dialect):
        latitude, longitude, radius_km = value
        field_object.to_db_value((latitude, longitude), model)  # запускает проверки значений поля
        return value

    def _operator(term, value):
        latitude, longitude, radius_km = value
        point = PostGISField.get_geography_term((latitude, longitude))
        return Function("ST_DWITHIN", term, point, radius_km * 1000)

    return FieldLookup(_operator, _encode_within_km_value)

PostGISField.register_lookup("within_km", PostGISField._within_km_lookup, value_type=tuple)
# .filter(location__within_km=(lat, lon, radius_km))
```

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

Встроенные окончания (`exact`, `in`, `gte`, `icontains` и другие) всегда важнее: свой оператор
никогда случайно не заменит один из них.
