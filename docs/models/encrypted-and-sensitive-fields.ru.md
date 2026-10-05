# Зашифрованные и чувствительные поля

Поля, значения которых база хранит зашифрованными (`EncryptedTextField`, `EncryptedJSONField`),
слепой индекс, позволяющий фильтровать такое поле по равенству, и `sensitive=True` — пометка, которая
не пускает значение поля в схемы, ошибки и журналы.

## <a id="encrypted-fields"></a>Зашифрованные поля

```python
from hare import fields
from hare.fields.encrypted.field_encryption import FieldEncryption

FieldEncryption.configure(settings.SECRET_KEY)  # один раз, при запуске

class Integration(Model):
    signing_secret = fields.EncryptedTextField()
    config = fields.EncryptedJSONField(default=dict)  # {"url": ..., "token": ..., "retries": 3}
    answers = fields.EncryptedJSONField(null=True, encrypt_keys=True)  # ключи тоже секретны
```

| Поле | Как хранится | Что шифруется |
|---|---|---|
| `EncryptedTextField` | `TEXT` (токен Fernet) | Вся строка целиком (и пустая тоже). Не индексируется — `unique=True`/`db_index=True` дают `ConfigurationError` (каждая запись даёт новый токен). |
| `EncryptedJSONField` | `JSON` / `JSONB` | Каждое значение документа на любой глубине: каждая строка, число, `true`/`false` и `null` — в словаре, в списке, в списке словарей, в словаре списков или сам документ, если он одно значение, — хранится токеном Fernet от своего текста JSON и читается обратно тем же значением того же типа. Видна только форма документа: вложенность, длины списков и, если не задано `encrypt_keys=True`, ключи словарей. Сначала значение преобразуется через `encoder` поля, поэтому значение, которое тот записывает строкой JSON (`datetime`, `date`, `UUID` и т. п.), хранится и читается обратно этой строкой. Принимается любое значение JSON — как у `JSONField`, присвоенная `str` — это значение, а не текст JSON; с `field_type=` (например, моделью Pydantic или `list[Model]`, как у `JSONField`) оно проверяется при записи и читается обратно этим типом. |

`EncryptedJSONField(encrypt_keys=True)` хранит токеном Fernet ещё и каждый ключ словаря на любой
глубине, так что в базе от документа видна только его форма. `encrypt_keys` не типа `bool` даёт
`ConfigurationError`.

```python
await Integration.objects.create(config={"hosts": [{"url": "https://a.example", "port": 443}], "debug": False})
# хранится: {"hosts": [{"url": "gAAAAA...", "port": "gAAAAA..."}], "debug": "gAAAAA..."}
# encrypt_keys=True: {"gAAAAA...": [{"gAAAAA...": "gAAAAA...", "gAAAAA...": "gAAAAA..."}], "gAAAAA...": "gAAAAA..."}
```

Смена `encrypt_keys` у существующего поля — такой же `AlterField`, как любое другое изменение поля:
при применении он переписывает ключи всех сохранённых документов — шифрует их настроенным ключом
или расшифровывает — партиями по 500 строк в порядке первичного ключа, не трогая токены значений; при
откате переписывает обратно. Ключ шифрования должен быть настроен, когда выполняется `migrate`, а у
модели должен быть первичный ключ. `sqlmigrate` показывает этот шаг комментарием — перезапись
выполняется в Python, а не командой SQL.

- Шифрование — [Fernet](https://cryptography.io/en/latest/fernet/) (AES-128-CBC + HMAC-SHA256) из
  необязательного пакета `cryptography`: `pip install hare-orm[encryption]`. Для импорта полей он
  не нужен — первое настоящее использование (запись, чтение или `FieldEncryption.configure()`)
  даёт `ConfigurationError` с подсказкой установить пакет.
- `FieldEncryption.configure(secret_key: str, previous_keys: Sequence[str] = (), blind_index_key: str | None = None) -> None`
  (`hare.fields.encrypted.field_encryption`) получает каждый ключ как `urlsafe_b64encode(sha256(secret))`; ключи общие для всего процесса и
  всех зашифрованных полей. Значения записываются ключом `secret_key`, а читаются им или любым из
  `previous_keys` (см. [Смена ключа](#changing-the-encryption-key)). Использование поля до вызова
  этого метода даёт `ConfigurationError`, как и пустой секрет или `previous_keys`, переданный одной
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
  `__has_key`/`__has_keys`/`__has_any_keys` (у `EncryptedJSONField` с открытыми ключами — по ключам
  верхнего уровня документа). С `encrypt_keys=True` ключ — тоже случайный токен, поэтому операторы
  по ключам тоже дают `FieldError`.
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
  простое `F()` другого поля того же класса, которое хранит значения так же (`EncryptedTextField` ↔
  `EncryptedTextField`, `EncryptedJSONField` ↔ `EncryptedJSONField` с тем же `encrypt_keys`) —
  шифротекст копируется как есть, и это работает, потому что у всех зашифрованных полей один ключ. Любое другое выражение (`Value`,
  `F("plain_field")`, `Upper(...)`, `Case(...)` и т. п.) даёт `FieldError`: его результат был бы
  записан без шифрования. Копирование зашифрованного поля в обычное (`title=F("secret")`)
  отклоняется так же.
- `db_default=` отклоняется с `ConfigurationError`: значение по умолчанию в схеме хранилось бы
  незашифрованным или одним и тем же токеном во всех строках. Используйте `default=` на стороне
  Python.
- Оба поля по умолчанию `sensitive=True` (передайте `sensitive=False`, чтобы отключить).

### <a id="changing-the-encryption-key"></a>Смена ключа

```python
from hare.fields.encrypted.field_encryption import FieldEncryption

FieldEncryption.configure(new_secret, previous_keys=[old_secret])  # 1. пишет новым, читает обоими
written = await FieldEncryption.reencrypt(Integration, ApiClient)  # 2. все значения — новым ключом
FieldEncryption.configure(new_secret)  # 3. старого ключа больше нет
```

`await FieldEncryption.reencrypt(*models, batch_size=500) -> int` переписывает все зашифрованные значения
моделей текущим `secret_key` — каждый лист и, при `encrypt_keys=True`, каждый ключ документа
`EncryptedJSONField` — и возвращает число записанных строк. Строки читаются партиями по
`batch_size` в порядке первичного ключа, включая мягко удалённые строки и строки всех арендаторов,
и каждая партия записывается в своей транзакции (на базе без транзакций — без неё). Когда метод
отработал, уберите старый секрет из `previous_keys`. `batch_size`, который не является положительным
целым, даёт `QueryError`, модель без первичного ключа — `ConfigurationError`, а значение,
зашифрованное ключом, которого нет в настройке, — `DecryptionError`.

### <a id="blind-index"></a>Слепой индекс — фильтр по равенству на зашифрованном поле

```python
class Customer(Model):
    email = EncryptedTextField(blind_index=True, unique=True)

await Customer.objects.get(email="ann@example.com")
await Customer.objects.filter(email__in=["ann@example.com", "bob@example.com"])
```

`EncryptedTextField(blind_index=True)` добавляет модели поле `<имя>_blind_index` рядом с полем
(`BlindIndexField`, колонка `<колонка>_blind_index`, `VARCHAR(64)`, допускает NULL, с индексом):
hex-HMAC-SHA256 открытого текста — одинаковый для одинакового значения и нечитаемый без ключа. Он
записывается всякий раз, когда записывается поле, — `create()`, `save()` с любыми
`update_fields`/`changed_only`, `bulk_create()` (и `update_fields` upsert), `bulk_update()`,
`QuerySet.update()` (из открытого текста или из `F()` другого поля со слепым индексом) — из открытого
текста поля, а не из присвоенного ему значения; после записи в экземпляре тот индекс, который вернуло бы
чтение. Передача самого индекса в `update()` даёт `FieldError`.

С ним поле принимает `=`, `__not`, `__in` и `__not_in` (и `get(email=...)`): HMAC значения фильтра
сравнивается с сохранённым. Всё остальное — как без него: `__startswith`, `__icontains`, сравнения,
`order_by()`, выражение по любую сторону фильтра дают `FieldError`; совпадение точное
(`Ann@example.com` — другое значение, нормализуйте до записи и до фильтра). `unique=True` у поля делает
уникальным слепой индекс (зашифрованная колонка уникальной быть не может), и
`bulk_create(on_conflict=["email"])` конфликтует по нему. Индекс `sensitive`, как и его поле.

Ключ: `FieldEncryption.configure(secret_key, previous_keys=(), blind_index_key=None)` — ключ HMAC
выводится из `secret_key`, если `blind_index_key` не задан. С выведенным ключом смена `secret_key`
меняет все индексы: фильтры по значению ничего не находят, пока `FieldEncryption.reencrypt()` не перепишет
строки (он пишет слепые индексы вместе с зашифрованными значениями). С собственным `blind_index_key`
индексы переживают смену ключа шифрования; смена самого `blind_index_key` так же требует
`FieldEncryption.reencrypt()`. Включение `blind_index=True` у поля с данными — это `AddField` индекса (пустого);
один раз запустите `FieldEncryption.reencrypt()`, чтобы его заполнить.

Слепой индекс раскрывает, в каких строках одинаковое значение (на этом и держится фильтр), — применяйте
его для значений, которые ищут по равенству (адрес почты, номер документа), а не для значений с немногими
вариантами, где само равенство выдаёт значение. У `EncryptedJSONField` слепого индекса нет.

## <a id="sensitive-fields"></a>Чувствительные поля

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
- Параметр запроса со значением чувствительного поля показывается как `<hidden>` — в DEBUG-логе
  каждой команды, в логе медленных запросов, в `parameters` события `QueryExecuted` и в тексте
  `DatabaseError`, если драйвер записал значение в своё сообщение. Это касается и записанного значения
  (`create()`, `save()`, `bulk_create()`, `bulk_update()`, `QuerySet.update()`), и значения, с которым
  поле сравнивает фильтр, в том числе через план запроса. Драйвер по-прежнему получает настоящее
  значение, а `DatabaseError.parameters` хранит его для отладки. Написанный вручную SQL (`execute_sql()`, `RawSQL`) не
  называет полей, поэтому его параметры показываются как есть.
