# Зашифрованные и чувствительные поля

## Зашифрованные поля {: #encrypted-fields }

```python
from hare import fields
from hare.fields.encryption import configure_field_encryption

configure_field_encryption(settings.SECRET_KEY)  # один раз, при запуске

class Integration(Model):
    signing_secret = fields.EncryptedTextField()
    config = fields.EncryptedJSONField(default=dict)  # {"url": ..., "token": ..., "retries": 3}
```

| Поле | Как хранится | Что шифруется |
|---|---|---|
| `EncryptedTextField` | `TEXT` (токен Fernet) | Вся строка целиком (и пустая тоже). Не индексируется — `unique=True`/`db_index=True` дают `ConfigurationError` (каждая запись даёт новый токен). |
| `EncryptedJSONField` | `JSON` / `JSONB` | Только собственные непустые **строковые** значения словаря — ключи, числа, `true`/`false`, `null` и вложенные списки и словари остаются обычным JSON, так что по сохранённому значению видно его устройство. Сначала словарь преобразуется через `encoder` поля, поэтому значение, которое тот записывает строкой JSON (`datetime`, `date`, `UUID` и т. п.), тоже шифруется и читается обратно этой строкой. Значение должно быть словарём (или его текстом JSON); с `field_type=` (например, моделью Pydantic, как у `JSONField`) оно проверяется при записи и читается обратно этим типом. |

- Шифрование — [Fernet](https://cryptography.io/en/latest/fernet/) (AES-128-CBC + HMAC-SHA256) из
  необязательного пакета `cryptography`: `pip install hare-orm[encryption]`. Для импорта полей он
  не нужен — первое настоящее использование (запись, чтение или `configure_field_encryption()`)
  даёт `ConfigurationError` с подсказкой установить пакет.
- `hare.fields.encryption.configure_field_encryption(secret_key: str, previous_keys: Sequence[str] = ()) -> None`
  получает каждый ключ как `urlsafe_b64encode(sha256(secret))`; ключи общие для всего процесса и
  всех зашифрованных полей. Значения записываются ключом `secret_key`, а читаются им или любым из
  `previous_keys` (см. [Смена ключа](#changing-the-encryption-key)). Использование поля до вызова
  этой функции даёт `ConfigurationError`, как и пустой секрет или `previous_keys`, переданный одной
  строкой.
- Чтение значения, зашифрованного ключом, которого нет ни в `secret_key`, ни в `previous_keys` (или
  колонки, в которой не токен), даёт `DecryptionError` с именем `Model.field` — шифротекст никогда
  не возвращается молча.
- Присвоенные значения (`Integration(signing_secret="...")`, присваивание атрибуту) хранятся в памяти
  в открытом виде; шифрует их только `to_db_value()` при сохранении, а прочитанное из базы
  расшифровывает `from_db_value()`. `to_dict()`, `values()`/`values_list()`, вычисляемое значение
  `annotate(x=F("secret"))` и схемы Pydantic видят открытый текст. Обращение по пути JSON внутрь
  `EncryptedJSONField` (`F("config__url")`) даёт `FieldError`: по ключу хранится токен Fernet.
- Проверки значений выполняются над открытым текстом; в их сообщениях об ошибке он никогда не
  появляется (см. [Чувствительные поля](#sensitive-fields)).
- **Искать по значению нельзя**: токен Fernet содержит случайный вектор инициализации, поэтому один
  и тот же текст каждый раз шифруется по-разному, и никакое сравнение (`=`, `__in`, `__contains` и
  другие) не может совпасть. Такой фильтр даёт `FieldError` ещё при построении запроса, а не молча
  возвращает пустой результат. Допустимые операторы: `__isnull`/`__not_isnull` (у обоих полей) и
  `__has_key`/`__has_keys`/`__has_any_keys` (у `EncryptedJSONField` — ключи хранятся открыто).
  `get_or_create(secret=...)` не работает по той же причине — ищите строки по другому полю.
  Сравнение зашифрованного поля с выражением, с любой стороны (`secret=F("name")`,
  `name=F("secret")`, `secret__in=Subquery(...)`, `name__in=Other.objects.all().values("secret")` и т. п.),
  тоже даёт `FieldError`.
- Всё, что база вычисляла бы над шифротекстом, отклоняется с `FieldError` при построении запроса —
  результат был бы бессмысленным (или попытался бы расшифровать открытый текст): `order_by()`,
  `group_by()`, `distinct("field")` (`DISTINCT ON` в PostgreSQL), `.distinct()` вместе с
  `.values()`/`.values_list()`, выбирающими зашифрованное поле (если не выбран ещё и первичный
  ключ), а также зашифрованное поле внутри любой функции, `Case`/`When`, арифметики, агрегата или
  `Window` (в том числе в `partition_by`/`order_by`). Допускается: `Count("field")` (без
  `distinct=True`), простое вычисляемое значение `F("field")` и оконные функции
  `FirstValue`/`LastValue`/`Count`/`Lag`/`Lead` (`Lag`/`Lead` — без `default=`). Простой
  `.distinct()` над объектами моделей тоже допускается: первичный ключ и так делает все строки
  разными.
- `update()`/`save()`/`update_or_create(defaults=...)` принимают для зашифрованного поля только
  простое `F()` другого поля того же класса (`EncryptedTextField` ↔ `EncryptedTextField`,
  `EncryptedJSONField` ↔ `EncryptedJSONField`) — шифротекст копируется как есть, и это работает,
  потому что у всех зашифрованных полей один ключ. Любое другое выражение (`Value`,
  `F("plain_field")`, `Upper(...)`, `Case(...)` и т. п.) даёт `FieldError`: его результат был бы
  записан без шифрования. Копирование зашифрованного поля в обычное (`title=F("secret")`)
  отклоняется так же.
- `db_default=` отклоняется с `ConfigurationError`: значение по умолчанию в схеме хранилось бы
  незашифрованным или одним и тем же токеном во всех строках. Используйте `default=` на стороне
  Python.
- Оба поля по умолчанию `sensitive=True` (передайте `sensitive=False`, чтобы отключить).

### Смена ключа {: #changing-the-encryption-key }

```python
from hare.fields.encryption import configure_field_encryption, reencrypt_fields

configure_field_encryption(new_secret, previous_keys=[old_secret])  # 1. пишет новым, читает обоими
written = await reencrypt_fields(Integration, ApiClient)             # 2. все значения — новым ключом
configure_field_encryption(new_secret)                               # 3. старого ключа больше нет
```

`await reencrypt_fields(*models, batch_size=500) -> int` переписывает все зашифрованные значения
моделей текущим `secret_key` и возвращает число записанных строк. Строки читаются пачками по
`batch_size` в порядке первичного ключа, включая мягко удалённые строки и строки всех арендаторов,
и каждая пачка записывается в своей транзакции (на базе без транзакций — без неё). Когда функция
отработала, уберите старый секрет из `previous_keys`. `batch_size`, который не является
положительным целым, или модель без первичного ключа дают `ConfigurationError`; значение,
зашифрованное ключом, которого нет в настройке, даёт `DecryptionError`.

## Чувствительные поля {: #sensitive-fields }

Любое поле принимает `sensitive=True` — пометку данных, которые не должны попадать в выгрузки,
журналы и открытые схемы API (учётные данные, токены, персональные данные). Колонку и хранимое
значение она не меняет — это описание поля, единый источник правды для тех, кто им пользуется:

```python
class User(Model):
    email = fields.CharField(max_length=255, sensitive=True)
    password_hash = fields.CharField(max_length=128, sensitive=True)
    api_key = fields.EncryptedTextField()  # sensitive=True по умолчанию

User._meta.sensitive_fields  # frozenset({"email", "password_hash", "api_key"})
```

- `Model._meta.sensitive_fields: frozenset[str]` — все поля, объявленные с `sensitive=True`.
  Чувствительный `ForeignKeyField`/`OneToOneField` помечает и свою колонку ключа (`owner_id`).
  `GeneratedField` берёт `sensitive` у своего `output_field`, если не передан свой `sensitive=`.
- `field.sensitive` говорит это для одного поля.
- `pydantic_model_creator(..., exclude_sensitive=True)` убирает такие поля из созданной схемы,
  включая чувствительные поля вложенных моделей (см. [Pydantic](../integrations/pydantic.ru.md)).
- `sensitive` никогда не записывается в миграции и не создаёт своей миграции: на схему базы он не
  влияет.
- `to_dict()` это не касается — он по-прежнему возвращает все поля.
- `ValidationError` для чувствительного поля (не прошла проверка, неверный тип значения и т. п.)
  показывает `<hidden>` вместо значения и не содержит исходного исключения (`__cause__` и
  `__context__` равны `None`), поэтому значение не видно и в записанном в журнал стеке вызовов.
- `repr()` у `snapshot()` показывает `<hidden>` для чувствительного поля. `diff_against()`
  возвращает настоящие значения — скрывайте их сами, прежде чем записывать результат в журнал.
