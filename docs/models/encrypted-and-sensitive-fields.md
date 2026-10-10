# Encrypted and sensitive fields

Fields whose values the database stores encrypted (`EncryptedTextField`, `EncryptedJSONField`), the
blind index that lets one be filtered by equality, and `sensitive=True` — the mark keeping a field's
value out of schemas, errors and logs.

## <a id="encrypted-fields"></a>Encrypted fields

```python
from hare import fields
from hare.fields.encrypted.field_encryption import FieldEncryption

FieldEncryption.configure(settings.SECRET_KEY)  # once, at startup

class Integration(Model):
    signing_secret = fields.EncryptedTextField()
    config = fields.EncryptedJSONField(default=dict)  # {"url": ..., "token": ..., "retries": 3}
    answers = fields.EncryptedJSONField(null=True, encrypt_keys=True)  # keys are secret too
```

| Field | Stored as | What is encrypted |
|---|---|---|
| `EncryptedTextField` | `TEXT` (a Fernet token) | The whole string value (an empty string too). Not indexable — `unique=True`/`db_index=True` raise `ConfigurationError` (every write produces a different token). |
| `EncryptedJSONField` | `JSON` / `JSONB` | Every value of the document, at any depth: each string, number, boolean and `null` — in a dict, a list, a list of dicts, a dict of lists, or the document itself when it's a single value — is stored as a Fernet token of its JSON text and read back as the same value of the same type. The document's shape stays visible: its nesting, the lengths of its lists and, unless `encrypt_keys=True`, its dict keys. The value is serialized through the field's `encoder` first, so a value it writes as a JSON string (a `datetime`, `date`, `UUID`, ...) is stored and read back as that string. Any JSON value is accepted — like `JSONField`, an assigned `str` is a value, not JSON text; with `field_type=` (e.g. a Pydantic model or `list[Model]`, as for `JSONField`) it's validated on write and read back as that type. |

`EncryptedJSONField(encrypt_keys=True)` stores every dict key — at any depth — as a Fernet token
too, so nothing of the document is readable in the database but its shape. A non-bool
`encrypt_keys` raises `ConfigurationError`.

```python
await Integration.objects.create(config={"hosts": [{"url": "https://a.example", "port": 443}], "debug": False})
# stored: {"hosts": [{"url": "gAAAAA...", "port": "gAAAAA..."}], "debug": "gAAAAA..."}
# encrypt_keys=True: {"gAAAAA...": [{"gAAAAA...": "gAAAAA...", "gAAAAA...": "gAAAAA..."}], "gAAAAA...": "gAAAAA..."}
```

Changing `encrypt_keys` of an existing field is an `AlterField` like any other change of a field:
applying it rewrites every stored document's keys — encrypted with the configured key, or
decrypted — in batches of 500 rows ordered by the primary key, the value tokens left as they are;
unapplying it rewrites them back. The encryption key has to be configured when `migrate` runs, and
the model needs a primary key. `sqlmigrate` lists the step as a comment — the rewrite runs in
Python, not as SQL.

- Encryption is [Fernet](https://cryptography.io/en/latest/fernet/) (AES-128-CBC + HMAC-SHA256)
  from the optional `cryptography` package: `pip install hare-orm[encryption]`. Importing the fields
  never needs it — the first actual use (a write, a read, or `FieldEncryption.configure()`) raises
  `ConfigurationError` telling you to install the extra.
- `FieldEncryption.configure(secret_key: str, previous_keys: Sequence[str] = (), blind_index_key: str | None = None) -> None`
  (`hare.fields.encrypted.field_encryption`) derives each key as `urlsafe_b64encode(sha256(secret))`; the keys are process-wide and shared by
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
  (`F("config__url")`) raises `FieldError` — the key's stored value is a Fernet token.
- Validators run against the plaintext; their error messages never include it (see
  [Sensitive fields](#sensitive-fields)).
- **Filtering by value is impossible**: a Fernet token embeds a random IV, so the same plaintext
  encrypts differently every time and no `=`/`__in`/`__contains`/... comparison could ever match.
  Such a lookup raises `FieldError` at query-build time instead of silently returning nothing.
  Supported lookups: `__isnull`/`__not_isnull` (both fields) and `__has_key`/`__has_keys`/
  `__has_any_keys` (`EncryptedJSONField` with plaintext keys, on the document's top-level keys).
  With `encrypt_keys=True` a key is a random token as well, so the key lookups raise `FieldError` too.
  `get_or_create(secret=...)` fails the same way — look rows up by another field. Comparing an
  encrypted field with an expression, on either side (`secret=F("name")`, `name=F("secret")`,
  `secret__in=Subquery(...)`, `name__in=Other.objects.all().values("secret")`, ...), raises `FieldError` too.
- Anything the database would compute over the ciphertext is rejected with `FieldError` at
  query-build time, since it would be meaningless (or would try to decrypt a plaintext fallback):
  `order_by()`, `group_by()`, `distinct("field")` (PostgreSQL `DISTINCT ON`), `.distinct()` with
  `.values()`/`.values_list()` selecting an encrypted field (unless the primary key is selected
  too), and an encrypted field inside any function, `Case`/`When`, arithmetic, aggregate or
  `Window` (`partition_by`/`order_by` included). Still allowed: `Count("field")` (without
  `distinct=True`), a bare `F("field")` annotation, and the `FirstValue`/`LastValue`/`Count`/
  `Lag`/`Lead` window functions (`Lag`/`Lead` without a `default=`). A plain `.distinct()` on
  model instances stays allowed — the primary key already makes every row distinct.
- `update()`/`save()`/`update_or_create(defaults=...)` accept an expression for an encrypted
  field only as a bare `F()` of another field of the same class storing its values the same way
  (`EncryptedTextField` ↔ `EncryptedTextField`, `EncryptedJSONField` ↔ `EncryptedJSONField` of the
  same `encrypt_keys`) — the ciphertext is copied as-is, which works because every encrypted field
  shares one key. Any other expression
  (`Value`, `F("plain_field")`, `Upper(...)`, `Case(...)`, ...) raises `FieldError`: its result
  would be written unencrypted. Copying an encrypted field into a plain one (`title=F("secret")`)
  is rejected the same way.
- `db_default=` is rejected with `ConfigurationError` — a schema-level default would be stored
  unencrypted or as one fixed token shared by every row. Use a Python-side `default=`.
- Both fields default to `sensitive=True` (pass `sensitive=False` to opt out).

### <a id="changing-the-encryption-key"></a>Changing the key

```python
from hare.fields.encrypted.field_encryption import FieldEncryption

FieldEncryption.configure(new_secret, previous_keys=[old_secret])  # 1. new writes, both read
written = await FieldEncryption.reencrypt(Integration, ApiClient)  # 2. every value, new key
FieldEncryption.configure(new_secret)  # 3. the old key is gone
```

`await FieldEncryption.reencrypt(*models, batch_size=500) -> int` writes every encrypted value of the models
with the current `secret_key` — every leaf and, with `encrypt_keys=True`, every key of an
`EncryptedJSONField` document — and returns how many rows it wrote. It reads the rows in batches of
`batch_size`, ordered by the primary key, soft-deleted rows and every tenant's rows included, and
writes each batch in a transaction of its own (on a database without transactions, without one).
Once it has run, drop the old secret from `previous_keys`. A `batch_size` that isn't a positive int
raises `QueryError`, a model without a primary key `ConfigurationError`, and a value encrypted with a
key that isn't configured `DecryptionError`.

### <a id="blind-index"></a>Blind index — equality filters on an encrypted field

```python
class Customer(Model):
    email = EncryptedTextField(blind_index=True, unique=True)

await Customer.objects.get(email="ann@example.com")
await Customer.objects.filter(email__in=["ann@example.com", "bob@example.com"])
```

`EncryptedTextField(blind_index=True)` gives the model a `<name>_blind_index` field next to the field
(a `BlindIndexField`, column `<column>_blind_index`, `VARCHAR(64)`, nullable, indexed): the hex
HMAC-SHA256 of the plaintext — the same for the same value, unreadable without the key. It is written
whenever its field is — `create()`, `save()` with any `update_fields`/`changed_only`, `bulk_create()`
(an upsert's `update_fields` too), `bulk_update()`, `QuerySet.update()` (from the plaintext, or from
`F()` of another field with a blind index) — from the field's plaintext, never from a value assigned
to it; after a write the instance holds the index a read would give. Giving the index itself to
`update()` raises `FieldError`.

With it, the field takes `=`, `__not`, `__in` and `__not_in` (and `get(email=...)`): the filter value's
HMAC is compared with the stored one. Everything else stays as without it — `__startswith`,
`__icontains`, comparisons, `order_by()`, an expression on either side of a filter raise `FieldError`;
the match is exact (`Ann@example.com` is another value — normalize before writing and filtering).
`unique=True` on the field makes the blind index unique (the encrypted column can't be), and
`bulk_create(on_conflict=["email"])` conflicts on it. The index is `sensitive` like its field.

The key: `FieldEncryption.configure(secret_key, previous_keys=(), blind_index_key=None)` — the HMAC
key is derived from `secret_key` unless `blind_index_key` is given. With the derived key, changing
`secret_key` changes every index: filters by value match nothing until `FieldEncryption.reencrypt()` has
rewritten the rows (it writes the blind indexes with the encrypted values). With a `blind_index_key`
of its own, the indexes survive an encryption key change; changing `blind_index_key` itself needs
`FieldEncryption.reencrypt()` the same way. Turning `blind_index=True` on for a field with rows is an
`AddField` of the index (empty); run `FieldEncryption.reencrypt()` once to fill it.

A blind index reveals which rows hold the same value (that is what makes it filterable) — use it for
values looked up by equality (an email address, a document number), not for low-cardinality ones where
equality alone gives the value away. `EncryptedJSONField` has no blind index.

## <a id="sensitive-fields"></a>Sensitive fields

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
  real values — mask them before logging its result.
- A query's parameter holding a sensitive field's value is shown as `<hidden>` — in the DEBUG log
  of every statement, the slow query log, the `parameters` of `QueryExecuted` and the text of a
  `DatabaseError` whose driver wrote the value into its message. It covers a value written
  (`create()`, `save()`, `bulk_create()`, `bulk_update()`, `QuerySet.update()`) and one a filter
  compares the field with, through a query plan too. The driver still receives the real value, and
  `DatabaseError.parameters` keeps it for debugging. Raw SQL (`execute_sql()`, `RawSQL`) names no field,
  so its parameters are shown as they are.
