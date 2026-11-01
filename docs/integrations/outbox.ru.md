# Очередь исходящих событий

Записать строку и сообщить о ней — две операции: если процесс остановится (или брокер на миг станет
недоступен) между ними, сообщение либо потеряется, либо уйдёт о записи, которая затем откатилась.
Транзакционный outbox пишет сообщение ещё одной строкой — **событием outbox** — в *той же* транзакции,
что и запись, о которой оно сообщает, поэтому обе фиксируются или откатываются вместе. Затем
`OutboxRelay` доставляет события туда, куда они идут: в Kafka, RabbitMQ, потоки Redis, на HTTP-адрес, в
taskiq или в вашу собственную функцию.

Доставка — **как минимум один раз**: событие может прийти дважды (relay остановился между доставкой и её
отметкой), поэтому получатель отбрасывает повторы по `id` события, который передаёт каждая доставка.

События пишутся двумя способами:

- `OutboxEvent.enqueue()` — своё событие там, где вы его вызываете;
- `Meta.change_capture = ChangeCapture(...)` — событие о каждой строке, которую меняют записи ORM в
  модели; его пишет сама запись.

## <a id="outboxevent"></a>Модель outbox

`OutboxEvent` — абстрактная модель; проект объявляет свою конкретную модель, соединяя её со своей базовой
моделью множественным наследованием:

```python
from hare.contrib.outbox import ListenNotifyWakeup, OutboxEvent


class MyOutboxEvent(YourBaseModel, OutboxEvent):
    class Meta(YourBaseModel.Meta, OutboxEvent.Meta):
        outbox_wakeup = ListenNotifyWakeup()
```

| Колонка | Значение |
|---|---|
| `sequence` | Первичный ключ, `BIGINT`, который выдаёт база, — порядок доставки |
| `id` | `UUID` события — по нему получатель отбрасывает повторы; UUID версии 7 (`Uuid7.new`), упорядоченный по времени, — `UUIDField(version=7)`: UUID другой версии в `extra_field_values` отклоняется |
| `topic` | До 255 символов — по нему доставки выбирают получателя |
| `payload` | Значение JSON |
| `headers` | Объект JSON — метаданные помимо payload (контекст трассировки, автор изменения), переходят в заголовки сообщения Kafka, RabbitMQ или HTTP |
| `ordering_key` | События одного ключа доставляются строго по порядку; `NULL` — без порядка |
| `idempotency_key` | Уникальный — не больше одного события на ключ |
| `created_at` | Когда событие записано |
| `attempts`, `last_error` | Неудачные доставки и ошибка последней |
| `next_attempt_at` | Когда неудачное событие пробуется снова; `NULL` — сразу |
| `lease_until`, `leased_by` | Какой relay доставляет событие и до какого времени |
| `published_at` | Когда событие доставлено |
| `dead_lettered_at` | Когда у события кончились попытки — его больше не доставляют |

`OutboxEvent.Meta` объявляет только `abstract = True` и индексы самого relay: частичные индексы событий,
которые ещё предстоит доставить (по `sequence` и по `ordering_key, sequence`), доставленных и «мёртвых»
событий, а также `(topic, sequence)`.

> [!WARNING]
> **Держите `OutboxEvent.Meta` минимальным**
>
> `ModelMeta` сливает атрибуты `Meta` всех абстрактных предков в конкретный подкласс обходом MRO в
> обратном порядке, и при **совпадении** ключа у двух предков (`YourBaseModel.Meta` и `OutboxEvent.Meta`
> задают один атрибут) побеждает тот, кто идёт позже, — без ошибки. Опции проекта кладите в
> `YourBaseModel.Meta`, никогда не задавайте `table` или `indexes` в `OutboxEvent.Meta`.

`sequence` событию выдаётся при записи. События одной строки пишутся, пока запись держит блокировку этой
строки, поэтому их порядок совпадает с порядком фиксаций при любых часах серверов — relay упорядочивает
только по `sequence`.

### <a id="encoding"></a>Payload и заголовки: `OutboxJsonEncoding`

Колонки `payload` и `headers` записываются через `OutboxJsonEncoding` — одинаково для `enqueue()` и
захваченного изменения. Кроме собственных значений JSON он пишет явную таблицу:

| Значение | JSON |
|---|---|
| `Decimal` | его точный текст, `"12.50"` |
| `UUID` | его текст |
| `datetime`, `date`, `time` | ISO 8601 |
| `timedelta` | его секунды, число |
| IP-адрес или сеть | их текст |
| `bytes` | base64 |
| член перечисления | его значение |

Любое другое значение даёт `ValidationError` (с причиной `TypeError`) при записи события — оно никогда
молча не превращается в свой `str()`. Число с плавающей точкой NaN или бесконечность в `enqueue()` тоже
даёт `ValidationError` — на любой глубине payload: в JSON для него нет числа, и событие никогда не
записывается текстом, который не прочтёт ни один читатель JSON.

## <a id="enqueue"></a>`enqueue()`

```python
class OutboxEvent(Model):
    @classmethod
    async def enqueue(
        cls,
        topic: str,
        payload: Any,
        *,
        using: str | DatabaseClient | None = None,
        idempotency_key: str | None = None,
        ordering_key: str | None = None,
        headers: Mapping[str, Any] | None = None,
        extra_field_values: Mapping[str, Any] | None = None,
        wakeup: OutboxWakeup | None = None,
    ) -> Self: ...
```

```python
async with Transactions.atomic():
    loan = await Loan.objects.create(book=book, reader=reader)
    await MyOutboxEvent.enqueue(
        "library.loan.created",
        {"loan_id": loan.id, "book_id": loan.book_id},
        ordering_key=f"loan:{loan.id}",
        headers={"librarian": librarian.id},
    )
```

Внутри `Transactions.atomic()` каждый вызов ORM идёт через соединение транзакции, поэтому событие и
запись, о которой оно сообщает, фиксируются одним `COMMIT`; откат убирает обе. Вне транзакции событие
фиксируется сразу, как любой `create()`. После фиксации транзакции (вне транзакции — сразу) будильник
сигналит relay — `Meta.outbox_wakeup` модели или `wakeup=`.

> [!WARNING]
> **Модель outbox живёт на подключении записи**
>
> Два подключения никогда не делят одну транзакцию. Если `using=` не передан, у подключения модели нет
> открытой транзакции, а у другого подключения есть, `enqueue()` даёт `ConfigurationError` вместо того,
> чтобы зафиксировать событие отдельно. Передайте `using=`, если так и задумано.

Аргументы проверяются до записи (`ValidationError`): `headers` — отображение с непустыми текстовыми
именами, `extra_field_values` не задаёт ни поля самого `enqueue()`, ни поля, которые ведут ORM и relay
(`sequence`, `created_at`, `next_attempt_at`, `published_at`, `dead_lettered_at`, `attempts`,
`last_error`, `lease_until`, `leased_by`).

### <a id="idempotency-key"></a>`idempotency_key=`

На один ключ существует не больше одного события. Если оно уже есть, `enqueue()` возвращает **его** без
изменений (с его собственными темой и payload) — без ошибки, без дубликата и без сигнала будильника.
Повтор обработчика или задачи безопасен.

- Это `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING` и `SELECT` события по ключу на одном
  соединении. Конфликт не даёт ошибки и потому не обрывает внешнюю транзакцию PostgreSQL. Параллельная
  транзакция с тем же ключом ждёт первую и получает её событие — или пишет своё, если первая откатилась.
- Отбрасывается только конфликт по ключу. Конфликт по любому другому ограничению уникальности даёт
  `IntegrityError`.
- Ключ, освобождённый откатом, можно использовать снова. Доставленное, «мёртвое» или мягко удалённое
  событие ключ по-прежнему держит.
- У модели с арендаторами (`Meta.tenant_field`) ключи уникальны **между** арендаторами: ключ, занятый
  событием другого арендатора, даёт `IntegrityError` (как и ключ, зафиксированный после снимка
  `REPEATABLE READ`/`SERIALIZABLE`). Для ключей, уникальных **внутри** арендатора, объявите
  `idempotency_key` заново без `unique=True` и добавьте
  `UniqueConstraint(fields=("tenant_id", "idempotency_key"))`; без ограничения уникальности, покрывающего
  ключ, `enqueue()` с ключом даёт `ConfigurationError`.
- Событие с ключом пишется через `bulk_create()`, поэтому переопределённый у модели outbox `save()` для
  него не вызывается.

### <a id="extra-field-values"></a>Колонки подкласса: `extra_field_values=`

Подкласс со своими колонками (связь с тем, о чём событие, арендатор) заполняет их через
`extra_field_values=`. Арендатор берётся из активного `Tenancy.scope()` или из этих значений — явно
названный арендатор принимается и без активной области, как это делает `create()`.

### <a id="enqueue-many"></a>Несколько событий: `enqueue_many()`

```python
class OutboxEvent(Model):
    @classmethod
    async def enqueue_many(
        cls,
        messages: Iterable[OutboxMessage],
        *,
        using: str | DatabaseClient | None = None,
        wakeup: OutboxWakeup | None = None,
    ) -> list[Self]: ...
```

```python
from hare.contrib.outbox import OutboxMessage

await WebhookOutboxEvent.enqueue_many(
    OutboxMessage(event_topic, payload, extra_field_values={"webhook": webhook}) for webhook in subscriptions
)
```

`OutboxMessage(topic, payload, idempotency_key=None, ordering_key=None, headers=None,
extra_field_values=None)` принимает то же, что `enqueue()` для одного события, и проверяется так же.
События пишутся в одной транзакции — точке сохранения транзакции вызывающего — одним `INSERT` на каждую
серию подряд идущих одинаковых сообщений (с ключом идемпотентности или без, одного арендатора, названного
в `extra_field_values`, когда область арендатора не активна), в переданном порядке, поэтому события
одного ключа порядка доставляются в этом порядке. Вызов возвращает события в том же порядке: для ключа
идемпотентности, который уже хранится или передан дважды, — событие, хранящееся под ним (для всех таких
ключей — одним `SELECT`). Будильник срабатывает один раз — по темам записанных событий.

Ключи берутся в порядке сообщений — порядке событий, поэтому два одновременных вызова с одними
ключами в разном порядке могут взаимно заблокироваться на PostgreSQL: один из них даёт
`TransactionRetryError` — повторите его (`Transactions.atomic(retries=...)`) или передавайте общие
ключи в одном порядке.

## <a id="change-capture"></a>Захват изменений модели: `ChangeCapture`

```python
from hare.contrib.outbox import ChangeCapture, ChangePayload


class Loan(Model):
    status = fields.CharField(max_length=20)
    fee = fields.DecimalField(max_digits=10, decimal_places=2)
    reader = fields.ForeignKeyField("library.Reader")

    class Meta:
        change_capture = ChangeCapture(MyOutboxEvent, payload=ChangePayload.AFTER)
```

Каждая запись ORM в модели пишет событие outbox на каждую изменённую строку **в своей же транзакции**:

| Запись | Что захватывается |
|---|---|
| `save()`, `create()`, `get_or_create()`, `update_or_create()` | вставленная или изменённая строка |
| `bulk_create()`, `bulk_update()` | каждая строка — обновлённые вставкой-или-обновлением как изменения |
| `QuerySet.update()`, `QuerySet.restore()` | каждая подходящая строка |
| `delete()`, `QuerySet.delete()` | каждая удалённая строка; мягкое удаление — как изменение поля мягкого удаления |
| что достаёт `on_delete` | строки, которые удаляет `CASCADE` и обновляют `SET_NULL`/`SET_DEFAULT`, — включая собственные `ON DELETE CASCADE`/`SET NULL` базы |
| `insert_from()`, `merge()` | каждая записанная строка |
| связи «многие-ко-многим» | вставленные и удалённые строки модели `through=Model`, объявившей `change_capture` |

Написанный вручную SQL не захватывается. У автоматически созданной промежуточной таблицы нет модели,
поэтому для захвата связей объявите промежуточную модель. Запись вне транзакции открывает транзакцию
вокруг себя и своих событий — только у захватываемой модели, — а запись внутри транзакции открывает точку
сохранения: событие, упавшее после записи строки, отменяет запись, даже если вызывающий поймал ошибку и
продолжил транзакцию; на базе без транзакций события пишутся сразу
после записи, а `bulk_create()`, упавший или отменённый после части партий, захватывает строки, которые
они оставили. Там же отменённая запись захватываемой модели — и удаление, `on_delete` которого выполняет
hare, — доводится до конца вместе со своими событиями, а отмена поднимается после них. `bulk_update()`
захватываемой модели пишет все партии в этой одной транзакции (ошибка не оставляет ни одной из них, какой
бы ни был `batch_size`; на базе без транзакций партии, записанные до неё, захватываются), кроме
устаревшего объекта: остальные строки и их события записываются, а `StaleObjectError` бросается после них.

`Meta.change_capture` принимает любой `ChangeSink` (`hare.instrumentation.capture`); `ChangeCapture` — тот,
что пишет в outbox. Свой приёмник реализует `get_written_models()` — модели, в которые он пишет, каждая
на подключении захватываемой модели, — `get_field_names(model)` — поля, которые содержит изменение, — и
`async write(connection, model, changes)`, который вызывается в транзакции записи с её `CapturedChange`.

### <a id="change-capture-declaration"></a>Объявление

```python
class ChangeCapture:
    def __init__(
        self,
        outbox: type[OutboxEvent],
        *,
        operations: Iterable[RowOperation | str] = (RowOperation.INSERT, RowOperation.UPDATE, RowOperation.DELETE),
        payload: ChangePayload | str = ChangePayload.AFTER,
        fields: Iterable[str] | None = None,
        exclude: Iterable[str] = (),
        topic: KeyDeclaration = "{app}.{model}.{operation}",
        ordering_key: KeyDeclaration | None = "{label}:{pk}",
        extend: Callable[[CapturedChange], ChangeExtension | None | Awaitable[ChangeExtension | None]] | None = None,
        extend_many: Callable[
            [list[CapturedChange]],
            Sequence[ChangeExtension | None] | Awaitable[Sequence[ChangeExtension | None]],
        ] | None = None,
    ) -> None: ...

    def replace(self, **options: Any) -> Self: ...
```

| Аргумент | Что задаёт |
|---|---|
| `outbox` | Конкретная модель `OutboxEvent` — класс или метка `"app.Model"`, которая ищется при связывании моделей; на подключении захватываемой модели, это проверяет `Hare.init()` |
| `operations` | Захватываемые `RowOperation` |
| `payload` | Что событие держит от своей строки — ниже |
| `fields` | Поля, которые держит событие; связь — своим ключом (`reader_id`). По умолчанию все поля, записанные в таблице, кроме `sensitive` и шифрованных — их только если они названы здесь |
| `exclude` | Поля, убранные из полей по умолчанию, — не вместе с `fields` |
| `topic` | Шаблон из `{app}`, `{model}`, `{table}` и `{operation}` (`inserted`, `updated`, `deleted`) или функция изменения. По умолчанию `"{app}.{model}.{operation}"` в нижнем регистре — `library.loan.updated` |
| `ordering_key` | Шаблон из `{label}` (`library.Loan`), `{pk}` (части составного ключа через `:`), `{app}`, `{model}` и `{table}` или функция изменения — см. [`ordering_key`](#ordering-key) |
| `extend` | Функция изменения — обычная или асинхронная — дающая `ChangeExtension` или `None`; выполняется при записи, в её транзакции |
| `extend_many` | Функция изменений одного оператора записи, дающая дополнение каждого, — см. [`extend_many`](#extend-many); не вместе с `extend` |

<a id="ordering-key"></a>**`ordering_key`.** Шаблон из `{label}` (`library.Loan`), `{pk}` (части
составного ключа через `:`), `{app}`, `{model}` и `{table}` или функция изменения; по умолчанию ключ
самой строки, поэтому события одной строки доставляются по порядку. `None` — без порядка. Ключ длиннее
255 символов, собранный по шаблону (длинный текстовый первичный ключ), сохраняет начало и заканчивается
на `#` и SHA-256 всего ключа; ключ функции длиннее этого обрывает запись ошибкой `ValidationError` поля
события

Объявление проверяется, когда модель готова (`ConfigurationError`): имя поля, которое не записано в
таблице, шаблон с неизвестным именем, `BEFORE_AND_AFTER` без `Meta.track_dirty_fields`, абстрактная
модель outbox, модель outbox, которая сама захватывает изменения. Объявленное у абстрактной базовой
модели, оно захватывает каждую построенную на ней модель — каждую под своим именем, — так что одно
объявление покрывает весь сервис.

<a id="change-capture-replace"></a>**`replace(**options)`** даёт копию с другими опциями и тем же outbox —
для модели, уточняющей захват своей базовой модели:

```python
class Account(CapturedModel):
    class Meta(CapturedModel.Meta):
        change_capture = CapturedModel.Meta.change_capture.replace(exclude=("token",))
```

Принимает опции конструктора, кроме `outbox` (другое имя — `TypeError`), и проверяет их значения так же,
как конструктор. `fields` заменяет `exclude` и наоборот, `extend` заменяет `extend_many` и наоборот. Копия —
того же класса, что захват, атрибуты подкласса `ChangeCapture` сохраняются; сам копируемый захват не
меняется.

Тема — это метка модели, а не её таблица: она уникальна между приложениями и подключениями, та же в
миграциях и обобщённых связях и ничего не говорит о хранении (схема арендатора, разделы).
`topic="{table}.{operation}"` называет таблицу. Точки совпадают с шаблонами [`TopicRouter`](#topicrouter)
(`library.loan.*`) и тематических exchange RabbitMQ.

### <a id="change-payload"></a>Режимы payload и конверт

Payload события — конверт:

```json
{"model": "library.Loan", "operation": "updated", "pk": 7, "changed": ["status"],
 "before": {"status": "lent", "fee": "0.00", "reader_id": 3},
 "after": {"status": "returned", "fee": "0.00", "reader_id": 3},
 "occurred_at": "2026-10-06T12:00:00+00:00"}
```

Составной ключ — объект его полей: `"pk": {"id": 7, "version": 2}`. `changed` называет поля, которые
задало изменение, — из `update_fields`, изменённых полей отслеживаемого экземпляра, присвоений
`update()`, `fields` у `bulk_update()`, `update_fields` у вставки-или-обновления, присвоений веток
обновления `merge()` (когда все они задают одни и те же поля), у всех записей одинаково:
поля, названные вызывающим, связь — её ключевым полем, без отметок `auto_now`, слепых индексов и версии,
которые запись добавляет сама; `null`, когда это неизвестно, и для вставки или удаления.

| `payload` | `before` / `after` |
|---|---|
| `KEYS` | их нет — только ключ |
| `AFTER` | `after` — строка, какой её оставила запись; у удаления — `before`, строка, какой она была |
| `BEFORE_AND_AFTER` | оба; `before` равен `null` у вставки, `after` — у удаления |

Каждое значение `before` и `after` — JSON-форма его поля, `Field.get_json_value()`:

`GeneratedField` использует JSON-форму своего `output_field`, в том числе для диапазонов и именованных
контейнеров.

| Поле | В событии |
|---|---|
| `FloatField`, `Float32Field`, число с плавающей точкой в любом контейнере или `VectorField` | число; NaN или бесконечность — текстом, `"NaN"`, `"Infinity"`, `"-Infinity"` |
| `DecimalField`, `UUIDField`, даты и время, `TimeDeltaField`, поля IP, `BinaryField`, поля перечислений | как в таблице выше |
| `ArrayField`, `TupleField` | список, каждый элемент — в форме своего поля элемента; именованный tuple использует значения в порядке объявления полей |
| `NestedField` | список объектов, каждое значение — в форме своего поля элемента |
| `MapField` | список пар `[ключ, значение]` — ключ не обязан быть текстом (`UUID`, дата, кортеж) |
| `JSONField` с `field_type` | значение так, как pydantic пишет его в JSON (`model_dump(mode="json")`) |
| `RangeField` и его виды | `{"lower", "upper", "lower_inc", "upper_inc", "is_empty"}` — отображение, которое поле читает обратно, границы — как выше |
| `MultiRangeField` и его виды | список таких отображений |
| `GeometryField` и его виды (`PointField`, …) | его объект геометрии GeoJSON, который поле читает обратно — координаты в `srid` поля; GeoJSON (RFC 7946) не называет систему координат |
| `LowCardinalityField` | форма его базового поля |

Своё поле, у значений которого нет формы в JSON, задаёт свой `get_json_value()` — см. [Своё
поле](../models/field-types.ru.md#writing-your-own-field).

Что каждый режим добавляет к записи:

| Запись | `KEYS` | `AFTER` | `BEFORE_AND_AFTER` |
|---|---|---|---|
| `save()` | — | поле, которое экземпляр не загрузил, читается после записи | «было» — из снимка изменённых полей экземпляра |
| `create()` | — | значения экземпляра; поле, ждущее умолчания базы, читается после записи | — |
| `bulk_create()` | `RETURNING` ключей | строки читаются после записи | строки вставки-или-обновления читаются до неё, с блокировкой |
| `QuerySet.update()` | `RETURNING` ключей | `RETURNING` полей | `RETURNING OLD`, где сервер его умеет (PostgreSQL 18), иначе строки читаются заранее через `SELECT ... FOR UPDATE` |
| `delete()`, `QuerySet.delete()`, каскад | ключи — строка экземпляра читается заранее, с блокировкой; у запроса — `RETURNING` | поля удалённых строк, так же | то же |
| `bulk_update()` | строки читаются после | строки читаются после | снимки, иначе строки читаются заранее, с блокировкой |

`QuerySet.delete()` захватываемой модели — или модели, чей `on_delete` до неё достаёт, — выполняет каскад
ORM, поэтому строки, которые удалил бы собственный каскад базы, сначала читаются и захватываются. Модель
без `change_capture` платит одно чтение атрибута на запись; её команды не меняются.

`QuerySet.update()`, задающий колонку составного первичного ключа, переносит строки на другие ключи:
заранее прочитанные строки захватываются под ключами, которые им дало обновление. Там, где строки читаются
заранее (нет `RETURNING OLD`), такая колонка из выражения (`update(number=F("number") + 1)`) вызывает
`UnSupportedError` ещё до записи: какая строка ушла на какой ключ, не узнать — задайте значение.

`restore()`, `hard_delete()` и upsert (`bulk_create(update_fields=..., on_conflict=...)`,
`ignore_conflicts=True`) захватываются как любая запись: конфликтующая строка — как изменение, вставленная —
как вставка, пропущенная — никак. События `RowsChanged` одной записи — её модели и outbox — приходят в
произвольном порядке.

Outbox объявляет уникальные поля и пишет свои события через `INSERT ... ON CONFLICT`: на базе, которая
ничего не хранит уникальным (ClickHouse), модель outbox, а с ней и каждая модель с `Meta.change_capture`,
даёт `ConfigurationError` при привязке. `OutboxRelay` арендует события через `UPDATE ... RETURNING`.

### <a id="change-extension"></a>`ChangeExtension`

`extend` добавляет то, что сервис знает об изменении, а ORM — нет: адресатов, область, автора:

```python
from hare.contrib.outbox import ChangeCapture, ChangeExtension


async def add_author(change):
    return ChangeExtension(
        payload={"recipients": await get_recipients(change)},
        headers={"author": current_user_id.get()},
        extra_field_values={"tenant_id": change.tenant},
    )


class CapturedModel(Model):
    class Meta:
        abstract = True
        change_capture = ChangeCapture(MyOutboxEvent, extend=add_author)
```

| Атрибут | Значение |
|---|---|
| `payload` | Ключи, добавляемые в конверт, — не его собственные (`ValidationError`) |
| `headers` | Заголовки события |
| `extra_field_values` | Колонки модели outbox помимо собственных колонок `OutboxEvent` |

<a id="extend-many"></a>**`extend_many`** — для дополнения, прочитанного из базы: функция изменений
одного оператора записи — обычная или асинхронная, — дающая `ChangeExtension` (или `None`) каждого из
них, в их порядке. Она вызывается один раз на оператор каждой модели — `bulk_create`, `update()`, пакет
`bulk_update`, `MERGE`, удаление одной модели в каскаде, — где `extend` вызывается на каждую строку, так
что один запрос обслуживает все строки оператора. Число дополнений, не равное числу изменений, — запись
падает с `ValidationError`; каждое дополнение проверяется, как дополнение `extend`. Не вместе с `extend`
(`ConfigurationError`).

```python
async def add_recipients(changes):
    recipients = await get_recipients_of(change.pk for change in changes)
    return [ChangeExtension(payload={"recipients": recipients[change.pk]}) for change in changes]


change_capture = ChangeCapture(MyOutboxEvent, extend_many=add_recipients)
```

У изменения — `CapturedChange` (`hare.contrib.outbox`) — есть `model`, `operation`, `pk`, `changed`,
`before`, `after`, `occurred_at` и `tenant` (`Meta.tenant_field` строки). Если у модели outbox тоже есть
поле арендатора, в него пишется арендатор строки, если его не задал `extra_field_values`. В строках
модели с `Meta.tenant_schema` арендатора нет — `tenant` равен `None`, и её события не называют
арендатора (ключ одной строки у двух арендаторов — один ключ порядка): объявите её outbox тоже с
`Meta.tenant_schema = True`, чтобы события каждого арендатора оставались в его схеме.

## <a id="outboxrelay"></a>`OutboxRelay`

```python
relay = OutboxRelay(
    MyOutboxEvent,
    KafkaDelivery("localhost:9092"),   # или async def deliver(event)
    poll_interval_seconds=5.0,
    batch_size=100,
    max_delivery_attempts=5,
    retry_base_seconds=1.0,
    retry_max_seconds=300.0,
    lease_seconds=60.0,
    delivery_timeout_seconds=30.0,
    concurrency=10,
    topics=None,
    exclude_topics=(),
    wakeup=None,                       # по умолчанию Meta.outbox_wakeup модели
    name=None,                         # по умолчанию host:pid:случайная часть
)
async with relay:
    ...
```

`batch_size` — `LIMIT` захвата — целое от 1 до 10000 (иначе `ConfigurationError`).
`delivery_timeout_seconds` держите меньше `lease_seconds` — аренда, истёкшая посреди доставки, позволяет
другому relay доставить событие ещё раз. Модель можно задать меткой `"app.Model"`
(`OutboxRelay("shop.MyOutboxEvent", ...)`) — она ищется в текущем контексте, когда понадобится relay.

`start()`/`stop()` можно вызывать повторно; `stop()` дожидается relay и закрывает соединения доставки.
`Hare.close_connections()` relay не останавливает — остановите его сами или используйте `async with` на
время жизни приложения.

### <a id="relay-poll"></a>Один опрос

1. **Захват.** Одна короткая команда отдаёт relay в аренду следующую партию — события, которые ещё
   предстоит доставить, подошедшие по времени (`next_attempt_at` прошёл или `NULL`), не арендованные
   живым relay, в порядке `sequence`:
   `UPDATE ... SET lease_until, leased_by WHERE sequence IN (...) RETURNING`. Где база блокирует строки
   (PostgreSQL), кандидаты читаются `FOR UPDATE SKIP LOCKED`, поэтому несколько relay делят очередь; в
   SQLite это делает единственный писатель.
2. **Порядок.** Событие ключа порядка не берётся, пока не доставлено более раннее событие того же ключа —
   включая неудачное и «мёртвое». События одного ключа доставляются строго по порядку, а неудача
   задерживает только свой ключ; события без ключа ничего не ждут.
3. **Доставка** — вне какой-либо транзакции, поэтому медленный брокер не держит ни соединение, ни
   блокировку. В партии не больше одного события на ключ, поэтому она доставляется в любом порядке:
   доставка, отправляющая партии, получает её целиком, другая — до `concurrency` событий одновременно,
   каждое за `delivery_timeout_seconds`. Пока партия доставляется, relay продлевает её аренду трижды за
   `lease_seconds`, чтобы партию, которая идёт дольше одной аренды, не забрал и не доставил повторно
   другой relay.
4. **Отметка** — один `UPDATE` отмечает доставленные события опубликованными; неудачное событие получает
   `attempts + 1`, `last_error` и
   `next_attempt_at = now + min(retry_base_seconds * 2 ** (attempts - 1), retry_max_seconds)` со
   случайным разбросом до 10% в обе стороны. Когда попытки кончились, событие становится «мёртвым»
   (`dead_lettered_at`). Отметка пишется, даже если relay тем временем останавливают. Событие, которое
   тем временем опубликовал другой relay (аренда истекла), остаётся опубликованным, а неудача
   записывается, только пока событие ещё в аренде у этого relay.

Останавливаемый relay сначала заканчивает свою партию; relay, чей процесс упал между доставкой и
отметкой, оставляет аренду истечь, и партия доставляется снова — «как минимум один раз». Пока опрос
забирает события, relay сразу опрашивает снова, иначе ждёт сигнал будильника или `poll_interval_seconds`.
Время аренды и повторов — по часам relay.

`topics` ограничивает relay событиями этих тем — их он забирает, доставляет, считает и чистит, — поэтому
relay разных тем делят одну таблицу; `exclude_topics` оставляет эти темы другим relay. Оба принимают имена
тем и шаблоны-префиксы, оканчивающиеся на `*`: `"library.*"` — каждая тема, начинающаяся с `library.`
(`_` и `%` в нём — обычные символы). `*` или `?` в любом другом месте шаблона — `ConfigurationError`.
Будильник, назвавший темы, будит relay, если одна из них — его. Изменения модели и задачи
[`TaskiqDelivery`](taskiq.ru.md) в одном outbox, каждые своим relay:

```python
change_relay = OutboxRelay(MyOutboxEvent, KafkaDelivery("localhost:9092"), exclude_topics=["taskiq"])
task_relay = TaskiqDelivery(broker, model=MyOutboxEvent).get_relay()   # topics=["taskiq"]
```

### <a id="dead-letters"></a>«Мёртвые» события, очередь и очистка

«Мёртвое» событие больше не доставляется и задерживает следующие события своего ключа порядка, пока его
не повторят или не удалят:

```python
await relay.retry_dead_lettered()                       # все
await relay.retry_dead_lettered(topics=["library.loan.updated"], ids=[event_id])
await relay.retry_dead_lettered(topics=["library.*"])      # те же шаблоны, что у topics=
await relay.cleanup_dead_lettered(older_than=timedelta(days=30))
await relay.cleanup_published(older_than=timedelta(days=7))
```

`retry_dead_lettered()` возвращает событиям их попытки. Очистки удаляют партиями (`batch_size`, по
умолчанию 1000), не держа долгую блокировку, и отказывают модели с `Meta.soft_delete_field`
(`ConfigurationError`) — её удаление не освободило бы строки. Сам relay не запускает ни то, ни другое.

`get_backlog()` даёт `OutboxBacklog` для проверки здоровья: `pending` — события в очереди,
`oldest_pending_age_seconds` — возраст самого старого, `dead_lettered` — «мёртвые».

### <a id="relay-observers"></a>Наблюдение за relay

Relay сообщает о себе через [`Observers`](../observability/observers.ru.md):

| Событие | Атрибуты |
|---|---|
| `OutboxDelivered` | `model`, `event_id`, `topic`, `attempts`, `delay_seconds` — с момента записи события |
| `OutboxDeliveryFailed` | `model`, `event_id`, `topic`, `attempts`, `error`, `next_attempt_at` |
| `OutboxDeadLettered` | `model`, `event_id`, `topic`, `attempts`, `error` |

```python
Observers.observe(OutboxDeadLettered, alert_on_call)
```

Неудача также пишется в журнал — WARNING, а для «мёртвого» события ERROR — с исключением доставки,
привязанным к `DeliveryError`.

## <a id="wakeups"></a>Будильники

Будильник запускает опрос сразу после записи событий, чтобы relay не ждал `poll_interval_seconds`. Один
объект передаётся и модели outbox (`Meta.outbox_wakeup` или `enqueue(wakeup=)`), и relay — ничего не надо
согласовывать вручную. Сигналы одной транзакции склеиваются, по одному на тему, и уходят после её
фиксации — никогда для отката. Потерянный сигнал безопасен: relay всё равно опрашивают таблицу. Таким может
оказаться первый сигнал после перезапуска брокера: клиент Redis отправляет его по соединению, закрытому
сервером, и не повторяет. Подписка,
которая оборвалась, восстанавливается с паузами, а после пяти неудач подряд бросается с ERROR — relay
продолжает опрос.

| Будильник | Extra | Как устроен |
|---|---|---|
| `InProcessWakeup()` | — | relay того же процесса, сразу после фиксации — каждый в своём цикле событий, и для записи из другого потока |
| `ListenNotifyWakeup(channel="hare_outbox")` | — | `NOTIFY` на тему в транзакции событий — PostgreSQL доставляет его при фиксации; relay слушают `LISTEN` через [`NotificationListener`](../dialects/postgresql/listen-notify.ru.md) |
| `RedisWakeup(url, channel=...)` или `RedisWakeup(client=...)` | `redis` | pub/sub |
| `KafkaWakeup(bootstrap_servers, topic=..., create_topic=True, replication_factor=1)` | `kafka` | тема сигналов, которую каждый relay читает с конца, вне групп потребителей; создаётся с хранением сигналов в течение минуты |
| `RabbitMQWakeup(url, exchange=...)` | `rabbitmq` | fanout exchange, у каждого relay своя эксклюзивная автоудаляемая очередь |

Если relay делят будильник между работающими циклами событий, удаление последней подписки останавливает
слушатель и закрывает его транспорт на цикле, где они открыты, даже если этот relay использует другой цикл.

На соединении без `LISTEN`/`NOTIFY` (`Features.supports_listen_notify` выключен) запись, подающая сигнал
`ListenNotifyWakeup`, даёт `ConfigurationError` до записи событий, а relay, который слушает, —
`UnSupportedError`; там оставьте outbox без него, его relay опрашивают таблицу.

Свой будильник — подкласс `OutboxWakeup`: `signal(topics)` отправляет сигнал, `listen(connection_alias)`
слушает до отмены и передаёт каждый сигнал в `dispatch(topics)`, `close()` освобождает то, что они
открыли; `raise_if_unsupported(connection)` отклоняет соединение, на котором он не может подать сигнал,
до записи событий.

## <a id="deliveries"></a>Доставки

Доставка только отправляет: повторы, подсчёт попыток и «мёртвые» события — у relay. Каждая доставка
отправляет JSON payload телом, а `headers` события, его `id` (`hare-outbox-event-id`) и тему
(`hare-outbox-topic`) — заголовками. Функция `async def deliver(event)` тоже работает как доставка (её
оборачивает `CallableDelivery`). Своя доставка — подкласс `OutboxDelivery`: `deliver(event)` отправляет
одно событие; `deliver_batch(events, *, concurrency, timeout_seconds)` отправляет партию (по умолчанию
каждое событие через `deliver()`) и возвращает для каждого события `None` или то, из-за чего оно не ушло;
`close()` освобождает её соединения. `get_body()`, `get_headers()` и `get_key()` дают тело, заголовки и
ключ сообщения события так, как их отправляют встроенные доставки.

| Доставка | Extra | Куда отправляет |
|---|---|---|
| `KafkaDelivery(bootstrap_servers, topic=None)` или `KafkaDelivery(producer=...)` | `kafka` | в `topic` или тему события, с ключом порядка как ключом сообщения — события одного ключа остаются в одном разделе; партия уходит целиком, `acks="all"` |
| `RabbitMQDelivery(url, exchange="", routing_key=None)` | `rabbitmq` | постоянные сообщения через существующий exchange (при пустом — стандартный), ключ маршрутизации по умолчанию — тема события; каждое подтверждается брокером (publisher confirms) |
| `RedisStreamsDelivery(url, stream=None, max_length=None)` или `(client=...)` | `redis` | `XADD` в `stream` или тему события — id, тема, ключ порядка, заголовки и payload события в JSON; партия — одним конвейером, потоки подрезаются до `max_length` |
| [`WebhookDelivery(url, secret=None, timeout_seconds=10, headers=None)`](#webhookdelivery) или `(target=...)` | `http` | `POST`; с ключом — `hare-outbox-signature: sha256=<HMAC-SHA256 of the body>`; значения заголовков — байты UTF-8; любой ответ, кроме 2xx, — неудача, перенаправление не выполняется |
| [`TaskiqDelivery`](taskiq.ru.md#kiq-on-commit) | `taskiq` | задача taskiq |

### <a id="webhookdelivery"></a>`WebhookDelivery` подписчикам

```python
class WebhookDelivery(OutboxDelivery):
    def __init__(
        self,
        url: str | None = None,
        *,
        target: Callable[[OutboxEvent], WebhookTarget | Awaitable[WebhookTarget]] | None = None,
        secret: str | bytes | None = None,
        timeout_seconds: float = 10.0,
        headers: Mapping[str, str] | None = None,
        allows_url: Callable[[str], bool | Awaitable[bool]] | None = None,
        signature_header: str = "hare-outbox-signature",
        result_field: str | None = None,
    ) -> None: ...
```

| Аргумент | Значение |
|---|---|
| `url` | Адрес каждого события, подпись ключом `secret`, — или `target` |
| `target` | Функция события — обычная или асинхронная — дающая его `WebhookTarget(url, secret=None, headers={})`: адрес, ключ подписи и заголовки подписчика события. Ключ подписи — у цели, не вместе с `secret` (`ConfigurationError`) |
| `headers` | Заголовки каждого запроса; заголовки цели и самого события их перекрывают |
| `allows_url` | Функция адреса — обычная или асинхронная, — отказывающая ему ответом `False`; вызывается перед каждым запросом: событие отклонённого адреса не отправляется, не доставлено и повторяется, как любая неудача, — для проверки от запросов во внутреннюю сеть сервиса. Перенаправление не выполняется никогда, поэтому проверенный адрес — тот, по которому идёт запрос |
| `signature_header` | Заголовок, в котором идёт подпись `sha256=<HMAC-SHA256 тела>`, — по умолчанию `hare-outbox-signature`, другое имя для получателей, которые ждут своё (`X-Webhook-Signature`) |
| `result_field` | Поле модели outbox — `JSONField` вашего подкласса, — в которое пишется результат каждой попытки: `{"status_code", "body", "error"}`; тело и ошибка обрезаются до 4000 символов (`WEBHOOK_RESULT_BODY_MAX_LENGTH`), символ NUL пишется как U+FFFD (JSON PostgreSQL его не хранит); статус и тело — `None` без ответа |

Ни `url`, ни `target` или оба сразу — `ConfigurationError`.

```python
from hare.contrib.outbox import WebhookDelivery, WebhookTarget


class SubscriberOutboxEvent(OutboxEvent):
    subscription = fields.ForeignKeyField("app.Subscription", on_delete=OnDelete.CASCADE)
    webhook_result = fields.JSONField(null=True)

    class Meta(OutboxEvent.Meta):
        pass


async def get_target(event):
    subscription = await event.subscription
    return WebhookTarget(subscription.url, subscription.secret)


delivery = WebhookDelivery(target=get_target, allows_url=is_public_url, result_field="webhook_result")
```

### <a id="topicrouter"></a>`TopicRouter`

```python
delivery = TopicRouter(
    {
        "library.loan.*": KafkaDelivery("localhost:9092"),
        "notify.*": WebhookDelivery("https://hooks.example.com/notify", secret=secret),
    },
    default=log_event,
)
```

Тема, названная точно, получает свою доставку, иначе — первый подходящий шаблон (`fnmatch`) в заданном
порядке, иначе — `default`. Событие темы, которую не берёт ни один маршрут, **не доставляется** — оно
повторяется и становится «мёртвым», но никогда не отмечается доставленным.

## <a id="example"></a>Пример: одно объявление на сервис

```python
from hare.contrib.outbox import (
    ChangeCapture, ChangeExtension, ChangePayload, KafkaDelivery, OutboxEvent, OutboxRelay, RedisWakeup,
    TopicRouter, WebhookDelivery,
)

wakeup = RedisWakeup("redis://localhost:6379/0")


class ServiceOutboxEvent(OutboxEvent):
    class Meta(OutboxEvent.Meta):
        outbox_wakeup = wakeup


def get_extension(change):
    return ChangeExtension(headers={"author": current_user_id.get()})


class ServiceModel(Model):
    class Meta:
        abstract = True
        track_dirty_fields = True
        change_capture = ChangeCapture(
            ServiceOutboxEvent, payload=ChangePayload.BEFORE_AND_AFTER, extend=get_extension
        )


relay = OutboxRelay(
    ServiceOutboxEvent,
    TopicRouter(
        {"billing.*": WebhookDelivery("https://billing.example.com/events", secret=secret)},
        default=KafkaDelivery("localhost:9092"),
    ),
)
```

Каждая модель, построенная на `ServiceModel`, пишет свои изменения событиями `{app}.{model}.{operation}`,
которые доставляются по порядку для каждой строки.

## <a id="exceptions"></a>Исключения

`hare.contrib.outbox.DeliveryError` — событие не доставлено: собственный отказ доставки (webhook ответил
ошибкой, ни один маршрут не берёт тему) или неудача, которую пишет в журнал relay, с исключением доставки
в `__cause__`. Вызывающему relay оно не передаётся; неудачное событие не останавливает остальную партию.
