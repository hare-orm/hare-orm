# Validators

Base class:

```python
class Validator(metaclass=abc.ABCMeta):
    def __init__(self, message: str | None = None) -> None: ...

    @abc.abstractmethod
    def __call__(self, value: Any) -> None:
        """Raise ValidationError on failure."""
```

`message` replaces the validator's own failure text; a subclass raises through
`self._raise(default_message)`, which uses `message` when one was given.

A plain callable works too — you don't have to subclass `Validator`. Both forms are accepted in a
field's `validators=[...]` list:

```python
def validate_even(value: int) -> None:
    if value % 2 != 0:
        raise ValidationError("must be even")

fields.IntField(validators=[validate_even])
```

## Built-in validators {: #built-in-validators }

Every one of these except `IPv4Validator`/`IPv6Validator`/`IPv46Validator` takes a trailing
`message: str | None = None` — overrides the validator's own default failure text with yours:

```python
fields.CharField(max_length=40, validators=[MinLengthValidator(3, message="Too short!")])
```

| Validator | Constructor | Notes |
|---|---|---|
| `RegexValidator` | <code>(pattern: str, flags: int &#124; re.RegexFlag, message=None)</code> | uses `re.match` |
| `MaxLengthValidator` | `(max_length: int, message=None)` | fails when `len(value) > max_length`; a string, list, tuple or bytes |
| `MinLengthValidator` | `(min_length: int, message=None)` | |
| `MinValueValidator` | <code>(min&#95;value: int &#124; float &#124; Decimal, message=None)</code> | |
| `MaxValueValidator` | <code>(max&#95;value: int &#124; float &#124; Decimal, message=None)</code> | |
| `MaxDigitsValidator` | `(max_digits: int, decimal_places: int, message=None)` | Total significant digits of a `Decimal`, accounting for `decimal_places`. `message` overrides all three of its checks (too many total/decimal/whole digits) with the same text — no per-check override. |
| `CommaSeparatedIntegerListValidator` | `(allow_negative: bool = False, message=None)` | |
| `DomainNameValidator` | `(accept_idna: bool = True, message=None)` | RFC 1034/1123; raises `InvalidDomainName`. Pre-built instance: `validate_domain_name`. |
| `URLValidator` | <code>(allowed&#95;schemes: list&#91;str&#93; &#124; None = None, message=None)</code> | default schemes `["http","https","ftp","ftps"]`; raises `InvalidURL`/`InvalidScheme`. Pre-built instance: `validate_url`. |
| `EmailValidator` | <code>(allowed&#95;domains: list&#91;str&#93; &#124; None = None, message=None)</code> | raises `InvalidEmailAddress`. Pre-built instance: `validate_email`. |
| `IPv4Validator` | `()` | **No `message=`** — always raises its own fixed text. Pre-built instance: `validate_ipv4_address`. |
| `IPv6Validator` | `()` | **No `message=`.** Pre-built instance: `validate_ipv6_address`. |
| `IPv46Validator` | `()` | **No `message=`.** Tries v4 then v6. Pre-built instance: `validate_ipv46_address`. |

```python
fields.CharField(max_length=254, validators=[validate_email])
fields.CharField(max_length=64, validators=[RegexValidator(r"^[a-z0-9_-]+$", 0)])
fields.IntField(validators=[MinValueValidator(0), MaxValueValidator(100)])
```

PostgreSQL's own validators for mappings and ranges (`KeysValidator`, `RangeMinValueValidator`,
`RangeMaxValueValidator`) are in `hare.dialects.postgresql.validators` — see
[PostgreSQL validators](#postgresql-validators).

## PostgreSQL validators {: #postgresql-validators }

`hare.dialects.postgresql.validators`, for `validators=[...]` of a field:

| Validator | Checks |
|---|---|
| `KeysValidator(keys, strict=False, message=None)` | an `HStoreField` mapping has every key of `keys` - and no other with `strict=True` |
| `RangeMinValueValidator(limit_value, message=None)` | the range's lower bound is at least `limit_value` (an unbounded lower side fails) |
| `RangeMaxValueValidator(limit_value, message=None)` | the range's upper bound is at most `limit_value` (an unbounded upper side fails) |

An empty range passes both range validators. The element count of an `ArrayField` is checked with
`MaxLengthValidator`/`MinLengthValidator`, which take lists.

```python
class Product(Model):
    attributes = HStoreField(validators=[KeysValidator(["color", "size"])])
    sizes = IntRangeField(validators=[RangeMinValueValidator(0), RangeMaxValueValidator(100)])
```

## Writing your own {: #writing-your-own }

```python
class EvenValidator(Validator):
    def __call__(self, value: Any) -> None:
        if value % 2 != 0:
            self._raise("must be even")

fields.IntField(validators=[EvenValidator()])
```

A validator is skipped entirely when the field is `null=True` and the value is `None`.
