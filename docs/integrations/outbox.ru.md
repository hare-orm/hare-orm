# Очередь исходящих событий

Записать строку и сообщить о ней — две операции: если процесс остановится (или брокер на миг станет
недоступен) между ними, сообщение либо потеряется, либо уйдёт о записи, которая затем откатилась.
Транзакционный outbox пишет сообщение ещё одной строкой — **событием outbox** — в *той же*
транзакции, что и запись, о которой оно сообщает, поэтому обе фиксируются или откатываются вместе.
Затем `OutboxRelay` доставляет события туда, куда они идут: в Kafka, RabbitMQ, потоки Redis, на
HTTP-адрес, в taskiq или в вашу собственную функцию.

Доставка — **как минимум один раз**: событие может прийти дважды (relay остановился между доставкой
и её отметкой), поэтому получатель отбрасывает повторы по `id` события, который передаёт каждая
доставка.

События пишутся двумя способами:

- `OutboxEvent.enqueue()` — своё событие там, где вы его вызываете;
- `Meta.change_capture = ChangeCapture(...)` — событие о каждой строке, которую меняют записи ORM в
  модели; его пишет сама запись.

## <a id="outboxevent"></a>Модель outbox

`OutboxEvent` — абстрактная модель; проект объявляет свою конкретную модель, соединяя её со своей
базовой моделью множественным наследованием:

```python
from hare.contrib.outbox import ListenNotifyWakeup, OutboxEvent


class MyOutboxEvent(YourBaseModel, OutboxEvent):
    class Meta(YourBaseModel.Meta, OutboxEvent.Meta):
        outbox_wakeup = ListenNotifyWakeup()
```

| Колонка | Значение |
|---|---|
| `sequence` | Первичный ключ, `BIGINT`, который выдаёт база, — порядок доставки |
| `id` | `UUID` события — по нему получатель отбрасывает повторы |
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

`OutboxEvent.Meta` объявляет только `abstract = True` и индексы самого relay: частичные индексы
событий, которые ещё предстоит доставить (по `sequence` и по `ordering_key, sequence`),
доставленных и «мёртвых» событий, а также `(topic, sequence)`.

> [!WARNING]
> **Держите `OutboxEvent.Meta` минимальным**
>
> `ModelMeta` сливает атрибуты `Meta` всех абстрактных предков в конкретный подкласс обходом MRO в
> обратном порядке, и при **совпадении** ключа у двух предков (`YourBaseModel.Meta` и
> `OutboxEvent.Meta` задают один атрибут) побеждает тот, кто идёт позже, — без ошибки. Опции
> проекта кладите в `YourBaseModel.Meta`, никогда не задавайте `table` или `indexes` в
> `OutboxEvent.Meta`.

`sequence` событию выдаётся при записи. События одной строки пишутся, пока запись держит блокировку
этой строки, поэтому их порядок совпадает с порядком фиксаций при любых часах серверов — relay
упорядочивает только по `sequence`.

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

Любое другое значение даёт `ValidationError` (с причиной `TypeError`) при записи события — оно
никогда молча не превращается в свой `str()`.

## <a id="enqueue"></a>`enqueue()`

```python
OutboxEvent.enqueue(
    topic, payload, *, using=None, idempotency_key=None, ordering_key=None, headers=None,
    extra_field_values=None, wakeup=None,
)
```

```python
async with Transactions.atomic():
    order = await Order.objects.create(customer=customer, total=total)
    await MyOutboxEvent.enqueue(
        "shop.order.placed",
        {"order_id": order.id, "total": order.total},
        ordering_key=f"order:{order.id}",
        headers={"author": user.id},
    )
```

Внутри `Transactions.atomic()` каждый вызов ORM идёт через соединение транзакции, поэтому событие и
запись, о которой оно сообщает, фиксируются одним `COMMIT`; откат убирает обе. Вне транзакции
событие фиксируется сразу, как любой `create()`. После фиксации транзакции (вне транзакции — сразу)
будильник сигналит relay — `Meta.outbox_wakeup` модели или `wakeup=`.

> [!WARNING]
> **Модель outbox живёт на подключении записи**
>
> Два подключения никогда не делят одну транзакцию. Если `using=` не передан, у подключения модели
> нет открытой транзакции, а у другого подключения есть, `enqueue()` даёт `ConfigurationError`
> вместо того, чтобы зафиксировать событие отдельно. Передайте `using=`, если так и задумано.

Аргументы проверяются до записи (`ValidationError`): `topic`, `idempotency_key` и `ordering_key` —
строки от 1 до 255 символов, `headers` — отображение с непустыми текстовыми именами,
`extra_field_values` не задаёт ни поля самого `enqueue()`, ни поля, которые ведут ORM и relay
(`sequence`, `created_at`, `next_attempt_at`, `published_at`, `dead_lettered_at`, `attempts`,
`last_error`, `lease_until`, `leased_by`).

### <a id="idempotency-key"></a>`idempotency_key=`

На один ключ существует не больше одного события. Если оно уже есть, `enqueue()` возвращает **его**
без изменений (с его собственными темой и payload) — без ошибки, без дубликата и без сигнала
будильника. Повтор обработчика или задачи безопасен.

- Это `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING` и `SELECT` события по ключу на одном
  соединении. Конфликт не даёт ошибки и потому не обрывает внешнюю транзакцию PostgreSQL.
  Параллельная транзакция с тем же ключом ждёт первую и получает её событие — или пишет своё, если
  первая откатилась.
- Отбрасывается только конфликт по ключу. Конфликт по любому другому ограничению уникальности даёт
  `IntegrityError`.
- Ключ, освобождённый откатом, можно использовать снова. Доставленное, «мёртвое» или мягко удалённое
  событие ключ по-прежнему держит.
- У модели с арендаторами (`Meta.tenant_field`) ключи уникальны **между** арендаторами: ключ, занятый
  событием другого арендатора, даёт `IntegrityError` (как и ключ, зафиксированный после снимка
  `REPEATABLE READ`/`SERIALIZABLE`). Для ключей, уникальных **внутри** арендатора, объявите
  `idempotency_key` заново без `unique=True` и добавьте `UniqueConstraint(fields=("tenant_id",
  "idempotency_key"))`; без ограничения уникальности, покрывающего ключ, `enqueue()` с ключом даёт
  `ConfigurationError`.
- Событие с ключом пишется через `bulk_create()`, поэтому переопределённый у модели outbox `save()`
  для него не вызывается.

### <a id="extra-field-values"></a>Колонки подкласса: `extra_field_values=`

Подкласс со своими колонками (связь с тем, о чём событие, арендатор) заполняет их через
`extra_field_values=`. Арендатор берётся из активного `Tenancy.scope()` или из этих значений — явно
названный арендатор принимается и без активной области, как это делает `create()`.

## <a id="change-capture"></a>Захват изменений модели: `ChangeCapture`

```python
from hare.contrib.outbox import ChangeCapture, ChangePayload


class Order(Model):
    status = fields.CharField(max_length=20)
    total = fields.DecimalField(max_digits=10, decimal_places=2)
    customer = fields.ForeignKeyField("shop.Customer")

    class Meta:
        change_capture = ChangeCapture(MyOutboxEvent, payload=ChangePayload.AFTER)
```

Каждая запись ORM в модели пишет событие outbox на каждую изменённую строку **в своей же
транзакции**:

| Запись | Что захватывается |
|---|---|
| `save()`, `create()`, `get_or_create()`, `update_or_create()` | вставленная или изменённая строка |
| `bulk_create()`, `bulk_update()` | каждая строка — обновлённые вставкой-или-обновлением как изменения |
| `QuerySet.update()`, `QuerySet.restore()` | каждая подходящая строка |
| `delete()`, `QuerySet.delete()` | каждая удалённая строка; мягкое удаление — как изменение поля мягкого удаления |
| что достаёт `on_delete` | строки, которые удаляет `CASCADE` и обновляют `SET_NULL`/`SET_DEFAULT`, — включая собственные `ON DELETE CASCADE`/`SET NULL` базы |
| `insert_from()`, `merge()` | каждая записанная строка |
| связи «многие-ко-многим» | вставленные и удалённые строки модели `through=Model`, объявившей `change_capture` |

Написанный вручную SQL не захватывается. У автоматически созданной промежуточной таблицы нет модели, поэтому для
захвата связей объявите промежуточную модель. Запись вне транзакции открывает транзакцию вокруг себя
и своих событий — только у захватываемой модели; на базе без транзакций события пишутся сразу после
записи.

### <a id="change-capture-declaration"></a>Объявление

```python
ChangeCapture(
    outbox, *, operations=(INSERT, UPDATE, DELETE), payload=ChangePayload.AFTER, fields=None,
    exclude=(), topic="{app}.{model}.{operation}", ordering_key="{label}:{pk}", extend=None,
)
```

| Аргумент | Значение |
|---|---|
| `outbox` | Конкретная модель `OutboxEvent` — на подключении захватываемой модели, это проверяет `Hare.init()` |
| `operations` | Захватываемые `RowOperation` |
| `payload` | Что событие держит от своей строки — ниже |
| `fields` | Поля, которые держит событие; связь — своим ключом (`customer_id`). По умолчанию все поля, записанные в таблице, кроме `sensitive` и шифрованных — их только если они названы здесь |
| `exclude` | Поля, убранные из полей по умолчанию, — не вместе с `fields` |
| `topic` | Шаблон из `{app}`, `{model}`, `{table}` и `{operation}` (`inserted`, `updated`, `deleted`) или функция изменения. По умолчанию `"{app}.{model}.{operation}"` в нижнем регистре — `shop.order.updated` |
| `ordering_key` | Шаблон из `{label}` (`shop.Order`), `{pk}` (части составного ключа через `:`), `{app}`, `{model}` и `{table}` или функция изменения; по умолчанию ключ самой строки, поэтому события одной строки доставляются по порядку. `None` — без порядка |
| `extend` | Функция изменения — обычная или асинхронная — дающая `ChangeExtension` или `None`; выполняется при записи, в её транзакции |

Объявление проверяется, когда модель готова (`ConfigurationError`): имя поля, которое не записано в
таблице, шаблон с неизвестным именем, `BEFORE_AND_AFTER` без `Meta.track_dirty_fields`, абстрактная
модель outbox, модель outbox, которая сама захватывает изменения. Объявленное у абстрактной базовой
модели, оно захватывает каждую построенную на ней модель — каждую под своим именем, — так что одно
объявление покрывает весь сервис.

Тема — это метка модели, а не её таблица: она уникальна между приложениями и подключениями, та же в
миграциях и обобщённых связях и ничего не говорит о хранении (схема арендатора, разделы).
`topic="{table}.{operation}"` называет таблицу. Точки совпадают с шаблонами
[`TopicRouter`](#topicrouter) (`shop.order.*`) и тематических exchange RabbitMQ.

### <a id="change-payload"></a>Режимы payload и конверт

Payload события — конверт:

```json
{"model": "shop.Order", "operation": "updated", "pk": 7, "changed": ["status"],
 "before": {"status": "new", "total": "10.00", "customer_id": 3},
 "after": {"status": "paid", "total": "10.00", "customer_id": 3},
 "occurred_at": "2026-10-06T12:00:00+00:00"}
```

Составной ключ — объект его полей: `"pk": {"id": 7, "version": 2}`. `changed` называет поля, которые
задало изменение, — из `update_fields`, изменённых полей отслеживаемого экземпляра, присвоений
`update()`; `null`, когда это неизвестно, и для вставки или удаления.

| `payload` | `before` / `after` |
|---|---|
| `KEYS` | их нет — только ключ |
| `AFTER` | `after` — строка, какой её оставила запись; у удаления — `before`, строка, какой она была |
| `BEFORE_AND_AFTER` | оба; `before` равен `null` у вставки, `after` — у удаления |

Что каждый режим добавляет к записи:

| Запись | `KEYS` | `AFTER` | `BEFORE_AND_AFTER` |
|---|---|---|---|
| `save()` | — | поле, которое экземпляр не загрузил, читается после записи | «было» — из снимка изменённых полей экземпляра |
| `create()` | — | значения экземпляра; поле, ждущее умолчания базы, читается после записи | — |
| `bulk_create()` | `RETURNING` ключей | строки читаются после записи | строки вставки-или-обновления читаются до неё, с блокировкой |
| `QuerySet.update()` | `RETURNING` ключей | `RETURNING` полей | `RETURNING OLD`, где сервер его умеет (PostgreSQL 18), иначе строки читаются заранее через `SELECT ... FOR UPDATE` |
| `delete()`, `QuerySet.delete()`, каскад | ключи — строка экземпляра читается заранее, с блокировкой; у запроса — `RETURNING` | поля удалённых строк, так же | то же |
| `bulk_update()` | строки читаются после | строки читаются после | снимки, иначе строки читаются заранее, с блокировкой |

`QuerySet.delete()` захватываемой модели — или модели, чей `on_delete` до неё достаёт, — выполняет
каскад ORM, поэтому строки, которые удалил бы собственный каскад базы, сначала читаются и
захватываются. Модель без `change_capture` платит одно чтение атрибута на запись; её команды не
меняются.

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

У изменения — `CapturedChange` (`hare.contrib.outbox`) — есть `model`, `operation`, `pk`, `changed`, `before`, `after`,
`occurred_at` и `tenant` (`Meta.tenant_field` строки). Если у модели outbox тоже есть поле
арендатора, в него пишется арендатор строки, если его не задал `extra_field_values`.

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
    wakeup=None,                       # по умолчанию Meta.outbox_wakeup модели
    name=None,                         # по умолчанию host:pid:случайная часть
)
async with relay:
    ...
```

Опции проверяются при создании relay — тип и диапазон (`ConfigurationError`): длительности — конечные
числа до 86400 (`poll_interval_seconds`, `lease_seconds` и `delivery_timeout_seconds` больше 0, паузы
повтора — от 0, `retry_max_seconds` не меньше `retry_base_seconds`, `delivery_timeout_seconds` меньше
`lease_seconds`), `batch_size` и `max_delivery_attempts` — целые от 1 до 10000, `concurrency` — от 1
до 1000, `topics` — `None` или непустой список тем, `name` — от 1 до 255 символов.

`start()`/`stop()` можно вызывать повторно; `stop()` дожидается relay и закрывает соединения
доставки. `Hare.close_connections()` relay не останавливает — остановите его сами или используйте
`async with` на время жизни приложения.

### <a id="relay-poll"></a>Один опрос

1. **Захват.** Одна короткая команда отдаёт relay в аренду следующую партию — события, которые ещё
   предстоит доставить, подошедшие по времени (`next_attempt_at` прошёл или `NULL`), не арендованные
   живым relay, в порядке `sequence`: `UPDATE ... SET lease_until, leased_by WHERE sequence IN (...)
   RETURNING`. Где база блокирует строки (PostgreSQL), кандидаты читаются `FOR UPDATE SKIP LOCKED`,
   поэтому несколько relay делят очередь; в SQLite это делает единственный писатель.
2. **Порядок.** Событие ключа порядка не берётся, пока не доставлено более раннее событие того же
   ключа — включая неудачное и «мёртвое». События одного ключа доставляются строго по порядку, а
   неудача задерживает только свой ключ; события без ключа ничего не ждут.
3. **Доставка** — вне какой-либо транзакции, поэтому медленный брокер не держит ни соединение, ни
   блокировку. В партии не больше одного события на ключ, поэтому она доставляется в любом порядке:
   доставка, отправляющая партии, получает её целиком, другая — до `concurrency` событий одновременно,
   каждое за `delivery_timeout_seconds`.
4. **Отметка** — один `UPDATE` отмечает доставленные события опубликованными; неудачное событие
   получает `attempts + 1`, `last_error` и `next_attempt_at = now + min(retry_base_seconds * 2 **
   (attempts - 1), retry_max_seconds)` со случайным разбросом до 10% в обе стороны. Когда попытки
   кончились, событие становится «мёртвым» (`dead_lettered_at`). Отметка пишется, даже если relay тем
   временем останавливают.

Останавливаемый relay сначала заканчивает свою партию; relay, чей процесс упал между доставкой и
отметкой, оставляет аренду истечь, и партия доставляется снова — «как минимум один раз». Пока опрос забирает события, relay сразу опрашивает снова, иначе ждёт сигнал
будильника или `poll_interval_seconds`. Время аренды и повторов — по часам relay.

`topics` ограничивает relay событиями этих тем — их он забирает, доставляет, считает и чистит, —
поэтому relay разных тем делят одну таблицу.

### <a id="dead-letters"></a>«Мёртвые» события, очередь и очистка

«Мёртвое» событие больше не доставляется и задерживает следующие события своего ключа порядка, пока
его не повторят или не удалят:

```python
await relay.retry_dead_lettered()                       # все
await relay.retry_dead_lettered(topics=["shop.order.updated"], ids=[event_id])
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

Будильник запускает опрос сразу после записи событий, чтобы relay не ждал `poll_interval_seconds`.
Один объект передаётся и модели outbox (`Meta.outbox_wakeup` или `enqueue(wakeup=)`), и relay — ничего не
надо согласовывать вручную. Сигналы одной транзакции склеиваются, по одному на тему, и уходят после
её фиксации — никогда для отката. Потерянный сигнал безопасен: relay всё равно опрашивают таблицу.
Подписка, которая оборвалась, восстанавливается с паузами, а после пяти неудач подряд бросается с
ERROR — relay продолжает опрос.

| Будильник | Extra | Как устроен |
|---|---|---|
| `InProcessWakeup()` | — | relay того же процесса, сразу после фиксации |
| `ListenNotifyWakeup(channel="hare_outbox")` | — | `NOTIFY` на тему в транзакции событий — PostgreSQL доставляет его при фиксации; relay слушают `LISTEN` через [`NotificationListener`](../dialects/postgresql/listen-notify.ru.md). На соединении без `LISTEN`/`NOTIFY` сигнала нет |
| `RedisWakeup(url, channel=...)` или `RedisWakeup(client=...)` | `redis` | pub/sub |
| `KafkaWakeup(bootstrap_servers, topic=..., create_topic=True, replication_factor=1)` | `kafka` | тема сигналов, которую каждый relay читает с конца, вне групп потребителей; создаётся с хранением сигналов в течение минуты |
| `RabbitMQWakeup(url, exchange=...)` | `rabbitmq` | fanout exchange, у каждого relay своя эксклюзивная автоудаляемая очередь |

Свой будильник — подкласс `OutboxWakeup`: `signal(topics)` отправляет сигнал, `listen(connection_alias)`
слушает до отмены и передаёт каждый сигнал в `dispatch(topics)`, `close()` освобождает то, что они открыли.

## <a id="deliveries"></a>Доставки

Доставка только отправляет: повторы, подсчёт попыток и «мёртвые» события — у relay. Каждая доставка
отправляет JSON payload телом, а `headers` события, его `id` (`hare-outbox-event-id`) и тему
(`hare-outbox-topic`) — заголовками. Функция `async def deliver(event)` тоже работает как доставка (её
оборачивает `CallableDelivery`). Своя доставка — подкласс `OutboxDelivery`: `deliver(event)` отправляет одно
событие; `deliver_batch(events, *, concurrency, timeout_seconds)` отправляет партию (по умолчанию каждое
событие через `deliver()`) и возвращает для каждого события `None` или то, из-за чего оно не ушло;
`close()` освобождает её соединения. `get_body()`, `get_headers()` и `get_key()` дают тело, заголовки и
ключ сообщения события так, как их отправляют встроенные доставки.

| Доставка | Extra | Куда отправляет |
|---|---|---|
| `KafkaDelivery(bootstrap_servers, topic=None)` или `KafkaDelivery(producer=...)` | `kafka` | в `topic` или тему события, с ключом порядка как ключом сообщения — события одного ключа остаются в одном разделе; партия уходит целиком, `acks="all"` |
| `RabbitMQDelivery(url, exchange="", routing_key=None)` | `rabbitmq` | постоянные сообщения через существующий exchange (при пустом — стандартный), ключ маршрутизации по умолчанию — тема события; каждое подтверждается брокером (publisher confirms) |
| `RedisStreamsDelivery(url, stream=None, max_length=None)` или `(client=...)` | `redis` | `XADD` в `stream` или тему события — id, тема, ключ порядка, заголовки и payload события в JSON; партия — одним конвейером, потоки подрезаются до `max_length` |
| `WebhookDelivery(url, secret=None, timeout_seconds=10, headers=None)` | `http` | `POST`; с `secret` — `hare-outbox-signature: sha256=<HMAC-SHA256 тела>`; любой ответ, кроме 2xx, — неудача |
| [`TaskiqDelivery`](taskiq.ru.md#kiq-on-commit) | `taskiq` | задача taskiq |

### <a id="topicrouter"></a>`TopicRouter`

```python
delivery = TopicRouter(
    {
        "shop.order.*": KafkaDelivery("localhost:9092"),
        "notify.*": WebhookDelivery("https://hooks.example.com/notify", secret=secret),
    },
    default=log_event,
)
```

Тема, названная точно, получает свою доставку, иначе — первый подходящий шаблон (`fnmatch`) в
заданном порядке, иначе — `default`. Событие темы, которую не берёт ни один маршрут, **не
доставляется** — оно повторяется и становится «мёртвым», но никогда не отмечается доставленным.

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

Каждая модель, построенная на `ServiceModel`, пишет свои изменения событиями
`{app}.{model}.{operation}`, которые доставляются по порядку для каждой строки.

## <a id="exceptions"></a>Исключения

`hare.contrib.outbox.DeliveryError` — событие не доставлено: собственный отказ доставки (webhook
ответил ошибкой, ни один маршрут не берёт тему) или неудача, которую пишет в журнал relay, с
исключением доставки в `__cause__`. Вызывающему relay оно не передаётся; неудачное событие не
останавливает остальную партию.
