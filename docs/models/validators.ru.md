# Проверки значений

Базовый класс:

```python
class Validator(metaclass=abc.ABCMeta):
    def __init__(self, message: str | None = None) -> None: ...

    @abc.abstractmethod
    def __call__(self, value: Any) -> None:
        """При ошибке бросает ValidationError."""
```

`message` заменяет собственный текст ошибки проверки; подкласс сообщает об ошибке через
`self._raise(текст_по_умолчанию)`, и тот берёт `message`, если он передан.

Подойдёт и обычная функция — наследовать `Validator` не обязательно. Список `validators=[...]` у
поля принимает и то, и другое:

```python
def validate_even(value: int) -> None:
    if value % 2 != 0:
        raise ValidationError("должно быть чётным")

fields.IntField(validators=[validate_even])
```

## Встроенные проверки {: #built-in-validators }

Все они, кроме `IPv4Validator`/`IPv6Validator`/`IPv46Validator`, последним аргументом принимают
`message: str | None = None` — ваш текст ошибки вместо стандартного:

```python
fields.CharField(max_length=40, validators=[MinLengthValidator(3, message="Слишком коротко!")])
```

| Проверка | Конструктор | Что проверяет |
|---|---|---|
| `RegexValidator` | <code>(pattern: str, flags: int &#124; re.RegexFlag, message=None)</code> | совпадение с регулярным выражением через `re.match` |
| `MaxLengthValidator` | `(max_length: int, message=None)` | ошибка, если `len(value) > max_length`; значение — строка, список, кортеж или байты |
| `MinLengthValidator` | `(min_length: int, message=None)` | ошибка, если `len(value) < min_length` |
| `MinValueValidator` | <code>(min&#95;value: int &#124; float &#124; Decimal, message=None)</code> | значение не меньше `min_value` |
| `MaxValueValidator` | <code>(max&#95;value: int &#124; float &#124; Decimal, message=None)</code> | значение не больше `max_value` |
| `MaxDigitsValidator` | `(max_digits: int, decimal_places: int, message=None)` | Число значащих цифр `Decimal` с учётом `decimal_places`. `message` заменяет текст всех трёх его проверок (слишком много цифр всего, после запятой, до запятой) одним и тем же — отдельно для каждой проверки текст не задаётся. |
| `CommaSeparatedIntegerListValidator` | `(allow_negative: bool = False, message=None)` | список целых чисел через запятую |
| `DomainNameValidator` | `(accept_idna: bool = True, message=None)` | доменное имя по RFC 1034/1123; бросает `InvalidDomainName`. Готовый объект: `validate_domain_name`. |
| `URLValidator` | <code>(allowed&#95;schemes: list&#91;str&#93; &#124; None = None, message=None)</code> | адрес URL; схемы по умолчанию `["http","https","ftp","ftps"]`; бросает `InvalidURL`/`InvalidScheme`. Готовый объект: `validate_url`. |
| `EmailValidator` | <code>(allowed&#95;domains: list&#91;str&#93; &#124; None = None, message=None)</code> | адрес электронной почты; бросает `InvalidEmailAddress`. Готовый объект: `validate_email`. |
| `IPv4Validator` | `()` | **Без `message=`** — всегда свой постоянный текст. Готовый объект: `validate_ipv4_address`. |
| `IPv6Validator` | `()` | **Без `message=`.** Готовый объект: `validate_ipv6_address`. |
| `IPv46Validator` | `()` | **Без `message=`.** Пробует IPv4, затем IPv6. Готовый объект: `validate_ipv46_address`. |

```python
fields.CharField(max_length=254, validators=[validate_email])
fields.CharField(max_length=64, validators=[RegexValidator(r"^[a-z0-9_-]+$", 0)])
fields.IntField(validators=[MinValueValidator(0), MaxValueValidator(100)])
```

Проверки PostgreSQL для словарей и диапазонов (`KeysValidator`, `RangeMinValueValidator`,
`RangeMaxValueValidator`) находятся в `hare.dialects.postgresql.validators` — см.
[Проверки значений PostgreSQL](#postgresql-validators).

## Проверки значений PostgreSQL {: #postgresql-validators }

`hare.dialects.postgresql.validators`, для `validators=[...]` поля:

| Проверка | Что проверяет |
|---|---|
| `KeysValidator(keys, strict=False, message=None)` | в словаре `HStoreField` есть каждый ключ из `keys` — а при `strict=True` нет никаких других |
| `RangeMinValueValidator(limit_value, message=None)` | нижняя граница диапазона не меньше `limit_value` (сторона без границы не проходит) |
| `RangeMaxValueValidator(limit_value, message=None)` | верхняя граница диапазона не больше `limit_value` (сторона без границы не проходит) |

Пустой диапазон проходит обе проверки диапазона. Число элементов `ArrayField` проверяют
`MaxLengthValidator`/`MinLengthValidator`, которые принимают списки.

```python
class Product(Model):
    attributes = HStoreField(validators=[KeysValidator(["color", "size"])])
    sizes = IntRangeField(validators=[RangeMinValueValidator(0), RangeMaxValueValidator(100)])
```

## Своя проверка {: #writing-your-own }

```python
class EvenValidator(Validator):
    def __call__(self, value: Any) -> None:
        if value % 2 != 0:
            self._raise("должно быть чётным")

fields.IntField(validators=[EvenValidator()])
```

Если поле объявлено с `null=True`, а значение равно `None`, проверки не выполняются вовсе.
