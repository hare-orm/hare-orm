# Encrypted and sensitive fields

## Encrypted fields {: #encrypted-fields }

```python
from hare import fields
from hare.fields.encryption import configure_field_encryption

configure_field_encryption(settings.SECRET_KEY)  # once, at startup

class Integration(Model):
    signing_secret = fields.EncryptedTextField()
    config = fields.EncryptedJSONField(default=dict)  # {"url": ..., "token": ..., "retries": 3}
```

| Field | Stored as | What is encrypted |
|---|---|---|
| `EncryptedTextField` | `TEXT` (a Fernet token) | The whole string value (an empty string too). Not indexable — `unique=True`/`db_index=True` raise `ConfigurationError` (every write produces a different token). |
| `EncryptedJSONField` | `JSON` / `JSONB` | Only a dict's own non-empty **string** values — keys, numbers, booleans, `null` and nested lists/dicts stay plain JSON, so the stored shape stays inspectable. The dict is serialized through the field's `encoder` first, so a value it writes as a JSON string (a `datetime`, `date`, `UUID`, ...) is encrypted too and reads back as that string. The value must be a dict (or its JSON text); with `field_type=` (e.g. a Pydantic model, as for `JSONField`) it's validated on write and read back as that type. |

- Encryption is [Fernet](https://cryptography.io/en/latest/fernet/) (AES-128-CBC + HMAC-SHA256)
  from the optional `cryptography` package: `pip install hare-orm[encryption]`. Importing the fields
  never needs it — the first actual use (a write, a read, or `configure_field_encryption()`) raises
  `ConfigurationError` telling you to install the extra.
- `hare.fields.encryption.configure_field_encryption(secret_key: str, previous_keys: Sequence[str] = ()) -> None`
  derives each key as `urlsafe_b64encode(sha256(secret))`; the keys are process-wide and shared by
  every encrypted field. Values are written with `secret_key` and read with it or any of
  `previous_keys` (see [Changing the key](#changing-the-encryption-key)). Using a field before it's
  called raises `ConfigurationError`, and so do an empty secret and `previous_keys` given as one
  string.
- Reading a value encrypted with a key that is neither `secret_key` nor one of `previous_keys` (or a
  column that doesn't hold a token) raises `DecryptionError` naming the `Model.field` — never
  silently returns ciphertext.
- Values you assign (`Integration(signing_secret="...")`, attribute assignment) stay plaintext in memory;
  they're encrypted only by `to_db_value()` on save, and a value read from the database is decrypted
  by `from_db_value()`. `to_dict()`, `values()`/`values_list()`, an `annotate(x=F("secret"))`
  annotation and Pydantic schemas all see the plaintext. A JSON path into an `EncryptedJSONField`
  (`F("config__url")`) raises `FieldError` - the key's stored value is a Fernet token.
- Validators run against the plaintext; their error messages never include it (see
  [Sensitive fields](#sensitive-fields)).
- **Filtering by value is impossible**: a Fernet token embeds a random IV, so the same plaintext
  encrypts differently every time and no `=`/`__in`/`__contains`/... comparison could ever match.
  Such a lookup raises `FieldError` at query-build time instead of silently returning nothing.
  Supported lookups: `__isnull`/`__not_isnull` (both fields) and `__has_key`/`__has_keys`/
  `__has_any_keys` (`EncryptedJSONField` — keys stay plaintext).
  `get_or_create(secret=...)` fails the same way — look rows up by another field. Comparing an
  encrypted field with an expression, on either side (`secret=F("name")`, `name=F("secret")`,
  `secret__in=Subquery(...)`, `name__in=Other.objects.all().values("secret")`, ...), raises `FieldError` too.
- Anything the database would compute over the ciphertext is rejected with `FieldError` at
  query-build time, since it would be meaningless (or would try to decrypt a plaintext fallback):
  `order_by()`, `group_by()`, `distinct("field")` (Postgres `DISTINCT ON`), `.distinct()` with
  `.values()`/`.values_list()` selecting an encrypted field (unless the primary key is selected
  too), and an encrypted field inside any function, `Case`/`When`, arithmetic, aggregate or
  `Window` (`partition_by`/`order_by` included). Still allowed: `Count("field")` (without
  `distinct=True`), a bare `F("field")` annotation, and the `FirstValue`/`LastValue`/`Count`/
  `Lag`/`Lead` window functions (`Lag`/`Lead` without a `default=`). A plain `.distinct()` on
  model instances stays allowed — the primary key already makes every row distinct.
- `update()`/`save()`/`update_or_create(defaults=...)` accept an expression for an encrypted
  field only as a bare `F()` of another field of the same class (`EncryptedTextField` ↔
  `EncryptedTextField`, `EncryptedJSONField` ↔ `EncryptedJSONField`) — the ciphertext is copied
  as-is, which works because every encrypted field shares one key. Any other expression
  (`Value`, `F("plain_field")`, `Upper(...)`, `Case(...)`, ...) raises `FieldError`: its result
  would be written unencrypted. Copying an encrypted field into a plain one (`title=F("secret")`)
  is rejected the same way.
- `db_default=` is rejected with `ConfigurationError` — a schema-level default would be stored
  unencrypted or as one fixed token shared by every row. Use a Python-side `default=`.
- Both fields default to `sensitive=True` (pass `sensitive=False` to opt out).

### Changing the key {: #changing-the-encryption-key }

```python
from hare.fields.encryption import configure_field_encryption, reencrypt_fields

configure_field_encryption(new_secret, previous_keys=[old_secret])  # 1. new writes, both read
written = await reencrypt_fields(Integration, ApiClient)             # 2. every value, new key
configure_field_encryption(new_secret)                               # 3. the old key is gone
```

`await reencrypt_fields(*models, batch_size=500) -> int` writes every encrypted value of the models
with the current `secret_key` and returns how many rows it wrote. It reads the rows in batches of
`batch_size`, ordered by the primary key, soft-deleted rows and every tenant's rows included, and
writes each batch in a transaction of its own (on a database without transactions, without one).
Once it has run, drop the old secret from `previous_keys`. A `batch_size` that isn't a positive int,
or a model without a primary key, raises `ConfigurationError`; a value encrypted with a key that
isn't configured raises `DecryptionError`.

## Sensitive fields {: #sensitive-fields }

Any field accepts `sensitive=True` to mark data that must not leak into exports, logs or public API
schemas (credentials, tokens, personal data). It doesn't change the column or the stored value — it
is metadata, one source of truth for consumers:

```python
class User(Model):
    email = fields.CharField(max_length=255, sensitive=True)
    password_hash = fields.CharField(max_length=128, sensitive=True)
    api_key = fields.EncryptedTextField()  # sensitive=True by default

User._meta.sensitive_fields  # frozenset({"email", "password_hash", "api_key"})
```

- `Model._meta.sensitive_fields: frozenset[str]` — every field declared with `sensitive=True`. A
  sensitive `ForeignKeyField`/`OneToOneField` also marks its own shadow column (`owner_id`). A
  `GeneratedField` inherits `sensitive` from its `output_field` unless it passes its own
  `sensitive=`.
- `field.sensitive` tells it for one field.
- `pydantic_model_creator(..., exclude_sensitive=True)` drops them from the generated schema,
  nested models' sensitive fields included (see [Pydantic](../integrations/pydantic.md)).
- `sensitive` is never written into migrations and never produces a migration of its own — it has
  no effect on the database schema.
- `to_dict()` is unaffected — it still returns every field.
- A `ValidationError` raised for a sensitive field (a failed validator, a value of the wrong type,
  ...) shows `<hidden>` instead of the value, and carries no chained exception (`__cause__` and
  `__context__` are `None`), so a logged or reported traceback doesn't show the value either.
- `repr()` of a `snapshot()` shows `<hidden>` for a sensitive field. `diff_against()` returns the
  real values - mask them before logging its result.
