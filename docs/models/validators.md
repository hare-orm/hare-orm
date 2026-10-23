# Validators

A field's `validators=[...]` check every value written to it before it reaches the database: the
validators of `hare.fields.validators`, PostgreSQL's own, a `Validator` subclass of yours or a plain
callable.

```python
from hare.fields.validators import MaxValueValidator, MinValueValidator, Validator, validate_email
```

The base class:

```python
class Validator(metaclass=abc.ABCMeta):
    def __init__(self, message: str | None = None) -> None: ...

    @abc.abstractmethod
    def __call__(self, value: Any) -> None:
        """Raise ValidationError on failure."""
```

`message` replaces the validator's own failure text; a subclass raises through
`self._raise(default_message)`, which uses `message` when one was given.

A plain callable works too — you don't have to subclass `Validator`. Both forms are accepted in a field's
`validators=[...]` list:

```python
def validate_even(value: int) -> None:
    if value % 2 != 0:
        raise ValidationError("must be even")

fields.IntField(validators=[validate_even])
```

## <a id="built-in-validators"></a>Built-in validators

Every one of these takes a trailing `message: str | None = None` — your failure text instead of the
validator's own:

```python
fields.CharField(max_length=40, validators=[MinLengthValidator(3, message="Too short!")])
```

| Validator | Constructor | Notes |
|---|---|---|
| `RegexValidator` | <code>(pattern: str, flags: int &#124; re.RegexFlag, message=None)</code> | uses `re.match` |
| `MaxLengthValidator` | `(max_length: int, message=None)` | fails when `len(value) > max_length`; a string, list, tuple or bytes |
| `MinLengthValidator` | `(min_length: int, message=None)` | fails when `len(value) < min_length` |
| `MinValueValidator` | <code>(min&#95;value: int &#124; float &#124; Decimal, message=None)</code> | the value is at least `min_value` |
| `MaxValueValidator` | <code>(max&#95;value: int &#124; float &#124; Decimal, message=None)</code> | the value is at most `max_value` |
| `MaxDigitsValidator` | `(max_digits: int, decimal_places: int, message=None)` | Total significant digits of a `Decimal`, accounting for `decimal_places`. `message` overrides all three of its checks (too many total/decimal/whole digits) with the same text — no per-check override. |
| `CommaSeparatedIntegerListValidator` | `(allow_negative: bool = False, message=None)` | a list of integers separated by commas |
| `DomainNameValidator` | `(accept_idna: bool = True, message=None)` | RFC 1034/1123; raises `InvalidDomainName`. Pre-built instance: `validate_domain_name`. |
| `URLValidator` | <code>(allowed&#95;schemes: list&#91;str&#93; &#124; None = None, message=None)</code> | default schemes `["http","https","ftp","ftps"]`; raises `InvalidURL`/`InvalidScheme`. Pre-built instance: `validate_url`. |
| `EmailValidator` | <code>(allowed&#95;domains: list&#91;str&#93; &#124; None = None, message=None)</code> | raises `InvalidEmailAddress`. Pre-built instance: `validate_email`. |
| `IPv4Validator` | `(message=None)` | Pre-built instance: `validate_ipv4_address`. |
| `IPv6Validator` | `(message=None)` | Pre-built instance: `validate_ipv6_address`. |
| `IPv46Validator` | `(message=None)` | Tries v4 then v6. Pre-built instance: `validate_ipv46_address`. |
| `SlugValidator` | `(allow_unicode: bool = False, message=None)` | ASCII letters, digits, `-` and `_`; any Unicode letter or digit too with `allow_unicode`. Raises `InvalidSlug`. Pre-built instance: `validate_slug`. |
| `E164PhoneValidator` | `(message=None)` | a phone number in E.164 form: `+`, a country code not starting with 0, 15 digits at most (`+16502530000`). Raises `InvalidPhoneNumber`. Pre-built instance: `validate_e164_phone`. |

```python
fields.CharField(max_length=254, validators=[validate_email])
fields.CharField(max_length=64, validators=[RegexValidator(r"^[a-z0-9_-]+$", 0)])
fields.IntField(validators=[MinValueValidator(0), MaxValueValidator(100)])
```

PostgreSQL's own validators for mappings and ranges (`KeysValidator`, `RangeMinValueValidator`,
`RangeMaxValueValidator`) are in `hare.dialects.postgresql.validators` — see [PostgreSQL
validators](../dialects/postgresql/fields.md#postgresql-validators).

## <a id="writing-your-own"></a>Writing your own

```python
class EvenValidator(Validator):
    def __call__(self, value: Any) -> None:
        if value % 2 != 0:
            self._raise("must be even")

fields.IntField(validators=[EvenValidator()])
```

A validator is skipped entirely when the field is `null=True` and the value is `None`.

`LengthValidator` and `NumericValidator` are the bases of the length and the value validators: their
`_validate_type(value)` raises `ValidationError` for a value that isn't a string, list, tuple or bytes —
or a number (`int`, `float`, `Decimal`) — before the check itself.
