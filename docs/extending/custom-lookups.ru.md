# Свои операторы фильтра: `register_lookup()`

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
  запрос, а без него значение преобразует поле — так же, как свои значения. Остальные атрибуты —
  `array_element_field`, `array_element_fields`, `text_function`, `compares_json_path_text`,
  `is_tsvector` и `search_config`. Через связи оператор работает сам: `author__name__<оператор>`
  присоединяет `author` и применяет его к `name`.
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
