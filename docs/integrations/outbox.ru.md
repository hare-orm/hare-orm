# Очередь исходящих событий (`hare.contrib.outbox`)

Запись строки с данными и отправка события о ней — две отдельные операции: если процесс упадёт (или
брокер сообщений будет недолго недоступен) между ними, событие либо не уйдёт вовсе, либо уйдёт о
записи, которая потом откатилась. Приём «транзакционная очередь исходящих событий» (transactional
outbox) решает это так: событие записывается обычной строкой в *той же* транзакции базы, что и
данные. Это атомарно само собой — это просто ещё один `INSERT` в транзакции, которая либо
фиксируется, либо откатывается целиком. Отдельный процесс `OutboxRelay` затем читает неотправленные
строки и доставляет их туда, куда нужно (брокеру сообщений, веб-хуку и т. п.).

## `OutboxEvent` {: #outboxevent }

```python
class OutboxEvent(Model):
    id: UUIDField          # первичный ключ, по умолчанию uuid4
    topic: CharField        # max_length=255, с индексом
    payload: JSONField[dict]
    created_at: DatetimeField   # auto_now_add=True
    published_at: DatetimeField | None  # null=True; задаётся после доставки
    attempts: IntField      # по умолчанию 0
    last_error: TextField | None
    idempotency_key: CharField | None   # max_length=255, null=True, unique=True

    class Meta:
        abstract = True
        indexes = (
            Index(fields=("topic", "created_at")),
            PartialIndex(fields=("published_at",), condition=Q(published_at__isnull=True)),
        )

    @classmethod
    async def publish(
        cls,
        topic: str,
        payload: dict,
        *,
        using=None,
        idempotency_key: str | None = None,
        notify_channel: str | None = None,
        extra_field_values: Mapping[str, Any] | None = None,
    ) -> Self: ...
```

Объединяйте с базовой моделью своего проекта через множественное наследование — так же, как
[`VersionedModel`](../soft-delete-versions-tenants/versioned-models.ru.md):

```python
class MyOutboxEvent(YourBaseModel, OutboxEvent):
    class Meta(YourBaseModel.Meta, OutboxEvent.Meta):
        pass
```

!!! warning "`OutboxEvent.Meta` намеренно минимален"
    `OutboxEvent.Meta` задаёт только `abstract = True` и два индекса (`indexes`), которые нужны его
    собственному запросу доставки (выше), — больше ничего. `ModelMeta` сливает атрибуты `Meta` всех
    абстрактных предков в конкретный подкласс, обходя порядок наследования в обратную сторону, и если
    два абстрактных предка задают один и тот же атрибут (`YourBaseModel.Meta` и `OutboxEvent.Meta`),
    молча побеждает тот, кто идёт в этом обходе позже, — без ошибки. Поэтому не добавляйте в
    `OutboxEvent.Meta` имя таблицы (`table`) или дополнительные `indexes`; опции своего проекта задавайте
    в `YourBaseModel.Meta`, где они ни с чем в этом пакете не столкнутся.

### Публикация {: #publishing }

```python
async with Transactions.atomic():
    widget = await Widget.objects.create(name="Left Handle")
    await MyOutboxEvent.publish(topic="widget.created", payload={"widget_id": str(widget.id)})
```

Без `idempotency_key` `publish()` — это просто `await cls.create(topic=topic, payload=payload,
using=using)`, без особой механики. Любой вызов ORM внутри `Transactions.atomic()` сам
идёт через соединение этой транзакции (см. [Транзакции](../connections/transactions.ru.md)),
поэтому строка события и запись данных, которую она описывает, попадают в один `INSERT`/`COMMIT` без
дополнительной настройки. Если запись данных после `publish()` завершится ошибкой, откатится вся
транзакция вместе со строкой события — о записи, которой не было, ничего не будет отправлено.

`publish()` без открытой транзакции тоже работает — строка фиксируется сразу, как при любом другом
`create()`.

!!! warning "`MyOutboxEvent` должна работать через ТО ЖЕ подключение, что и запись данных"
    Атомарность есть, только если собственное подключение модели `MyOutboxEvent` (`default_connection`
    её приложения в настройках, или
    `ConnectionRouter`) указывает ровно на то подключение, на котором открыта окружающая транзакция:
    два разных подключения никогда не могут делить одну транзакцию SQL. Если `publish()` вызван без
    `using=`, когда на его собственном подключении транзакции нет, а на ДРУГОМ есть, он даёт
    `ConfigurationError`, а не фиксирует событие отдельно (что молча лишило бы весь приём смысла).
    Если это сделано намеренно, передайте `using=` с нужным подключением. Проверка действует и для
    вызовов с `idempotency_key=`/`notify_channel=`.

### Публикация без повторов: `idempotency_key=` {: #idempotency-key }

```python
async with Transactions.atomic():
    await MyOutboxEvent.publish(
        topic="order.paid",
        payload={"order_id": order_id},
        idempotency_key=f"order-paid-{order_id}",
    )
```

На каждый ключ существует не больше одной строки. Если строка с таким ключом уже есть, `publish()`
возвращает **её** без изменений (с её собственными `topic`/`payload`, а не только что переданными) —
без ошибки и без дубликата. Повтор обработчика, повторная доставка веб-хука или повторный запуск задачи
безопасны, и собственный `bulk_create(ignore_conflicts=True)` не нужен (он обходил бы проверку
`publish()` на чужую транзакцию).

- Это атомарно: `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING`, затем `SELECT` строки по ключу
  на том же соединении. Конфликт по ключу никогда не даёт ошибки, поэтому никогда не прерывает
  окружающую транзакцию PostgreSQL. Повторная публикация того же ключа в одной транзакции или в
  разных возвращает первую строку. Параллельная транзакция, публикующая тот же ключ, ждёт фиксации
  (или отката) первой и затем получает её строку (или вставляет свою).
- Повтор определяется только по конфликту ключа. Конфликт по любому другому уникальному ограничению —
  первичному ключу, уникальной колонке подкласса, заданной через `extra_field_values`, — даёт
  `IntegrityError` так же, как без ключа (и в PostgreSQL прерывает окружающую транзакцию, как любая
  неудачная команда); он никогда не принимается за «ключ уже использован».
- Ключ, освободившийся из-за отката транзакции, можно использовать снова. Строка, которая уже
  доставлена или мягко удалена (`Meta.soft_delete_field`), свой ключ сохраняет.
- Без ключа поведение прежнее: дубликаты допускаются, и сколько угодно строк с ключом `NULL`
  сосуществуют под ограничением уникальности и в PostgreSQL, и в SQLite.
- Ключ — строка длиной от 1 до 255 символов, иначе `ValidationError`.
- У модели с арендаторами (`Meta.tenant_field`) ключи уникальны **для всех** арендаторов сразу. Если
  ключ занят строкой другого арендатора, `publish()` даёт `IntegrityError`, а не возвращает эту строку.
  То же происходит при `REPEATABLE READ`/`SERIALIZABLE`, если владелец ключа зафиксировал его после
  начала вашего снимка (PostgreSQL обычно сначала сообщает там об ошибке сериализации).
- Чтобы ключи были уникальны **в пределах** арендатора, объявите в подклассе `idempotency_key` заново
  без `unique=True` и добавьте в `Meta.constraints` безусловное
  `UniqueConstraint(fields=("tenant_id", "idempotency_key"))`: тогда `publish()` указывает в `ON CONFLICT` это ограничение. Если
  ключ не покрыт ни одним ограничением уникальности, `publish()` с ключом даёт `ConfigurationError`.
- Арендатор берётся из текущего `Tenancy.scope()` или из `extra_field_values`: явно заданный арендатор
  принимается и без текущей области, с ключом или без, так же как в `create()`. При области из
  [нескольких значений](../soft-delete-versions-tenants/multi-tenancy.ru.md#tenancy-scope) его обязан назвать `extra_field_values`,
  а `publish()` с ключом возвращает строку, записанную под этим ключом для этого арендатора.
- С ключом строка записывается через `bulk_create()`, поэтому переопределённый у подкласса `save()`
  для неё не вызывается.

### Колонки подкласса: `extra_field_values=` {: #extra-field-values }

Подкласс со своими колонками (связь с тем, о чём событие, метка источника) заполняет их через
`extra_field_values=`, с ключом или без:

```python
await DeliveryEvent.publish(
    topic=channel.key,
    payload={"message": message},
    idempotency_key=f"automation-{automation.id}-{event_id}",
    extra_field_values={"automation": automation, "event_key": event_key},
)
```

Через него нельзя задать `topic`, `payload` или `idempotency_key` — передавайте их собственными
аргументами `publish()`, — а также поля, которые ведут ORM и доставщик: `created_at`, `published_at`,
`attempts`, `last_error` (для всех них `ValidationError` ещё до записи). Первичный ключ задать можно
(например, заранее вычисленный идентификатор); повторная публикация того же ключа с тем же явным
идентификатором возвращает существующую строку и второй `NOTIFY` не отправляет.

### Как разбудить доставщика: `notify_channel=` {: #notify-channel }

```python
async with Transactions.atomic():
    await MyOutboxEvent.publish(topic="widget.created", payload={...}, notify_channel="my_outbox_channel")
```

Когда записывается новая строка, `publish()` ещё и отправляет `NOTIFY` в канал `notify_channel`
(содержимое — идентификатор строки) через то же соединение. Внутри транзакции PostgreSQL доставляет
его только при фиксации — никогда при откате, — поэтому `OutboxRelay` с
`listen_channel="my_outbox_channel"` просыпается ровно тогда, когда строка становится видна. Когда для
`idempotency_key` возвращается уже существующая строка, `NOTIFY` не отправляется — сообщается только о
строке, которую этот вызов действительно вставил. В SQLite (там нет LISTEN/NOTIFY, доставщики только
опрашивают таблицу) `notify_channel` молча не учитывается.

## `OutboxRelay` {: #outboxrelay }

```python
relay = OutboxRelay(
    MyOutboxEvent,
    deliver=send_to_message_bus,   # async def deliver(event: MyOutboxEvent) -> None
    poll_interval_seconds=5.0,
    batch_size=100,
    listen_channel=None,           # необязательно: имя канала LISTEN/NOTIFY в PostgreSQL
    max_delivery_attempts=5,
    backoff_base_seconds=0.5,
)
await relay.start()
...
await relay.stop()
```

или как асинхронный контекстный менеджер:

```python
async with OutboxRelay(MyOutboxEvent, deliver=send_to_message_bus) as relay:
    ...
```

Параметры проверяются при создании — тип и диапазон, иначе `ConfigurationError`:
`poll_interval_seconds` — конечное число в `(0, 86400]`, `batch_size` — `int` в `1..10000`,
`max_delivery_attempts` — `int` в `1..10000`, `backoff_base_seconds` — конечное число в `[0, 3600]`,
`listen_channel` — `None` или непустая строка (границы:
`hare.contrib.outbox.constants.MAX_POLL_INTERVAL_SECONDS`/`MAX_BATCH_SIZE`/
`MAX_DELIVERY_ATTEMPTS_LIMIT`/`MAX_BACKOFF_BASE_SECONDS`).

Повторный вызов `start()`/`stop()` ничего не меняет. `stop()` отменяет фоновые задачи доставщика и
дожидается их, поэтому всегда возвращается, когда всё аккуратно остановлено, без вылетевшего
`asyncio.CancelledError`. Доставщик всегда опрашивает таблицу через общий клиент подключения, даже если
`start()` вызван внутри транзакции.

!!! warning "Не связан с `Hare.close_connections()`"
    В отличие от пулов соединений, `Hare.close_connections()` ничего не знает об `OutboxRelay`.
    Приложение должно само вызвать `.stop()` перед остановкой (или использовать доставщик как блок
    `async with`, живущий столько же, сколько приложение), — так же, как с
    `OpenTelemetryInstrumentor.uninstrument()`.

### Доставка, повторы и отложенные события {: #delivery-retry-dead-lettering }

Каждый цикл опроса забирает до `batch_size` строк, где `published_at IS NULL` и
`attempts < max_delivery_attempts`, начиная с самых старых, и вызывает для каждой `deliver(event)`:

- **Успех** (`deliver` вернулся без ошибки): `published_at` получает текущее время и сохраняется.
- **Ошибка** (`deliver` бросил исключение): `attempts` увеличивается, `last_error` получает
  `str(exc)` и сохраняется, а исключение оборачивается в `DeliveryError` для записи в журнал. Когда
  `attempts` достигает `max_delivery_attempts`, строку больше не забирает ни один опрос — она
  **откладывается** (dead-lettering), но не удаляется: `published_at` навсегда остаётся `NULL`, поэтому
  её легко найти потом:

```python
dead_lettered = await MyOutboxEvent.objects.filter(
    published_at=None, attempts__gte=relay.max_delivery_attempts
)
```

Временная ошибка, после которой доставка удалась (до достижения предела), доставляется обычным
образом при следующем опросе — за повтор, который в итоге сработал, никакого наказания нет.

В PostgreSQL каждая строка (до `batch_size` за цикл) забирается через `SELECT ... FOR UPDATE SKIP LOCKED`
и доставляется в **своей** транзакции, которая фиксируется отдельно, как только доставка этой строки
удалась и сохранена. Следствия:

- Несколько доставщиков (например, в нескольких копиях приложения) безопасно делят одну очередь, а не
  доставляют дважды: строку, уже заблокированную транзакцией одного доставщика, параллельный опрос
  другого просто пропускает, а не ждёт.
- Если после успешного `deliver()` строку не удалось сохранить (временная ошибка базы или отмена задачи
  опроса посреди сохранения), откатывается только забор этой одной строки — это никогда не отменяет
  уже зафиксированную доставку более ранней строки того же цикла. Именно поэтому строки никогда не
  забираются одной транзакцией на всю пачку «всё или ничего».
- Сам `deliver()` выполняется в точке сохранения этой транзакции. Ошибка базы, которую он бросил
  (например, `IntegrityError` от его собственной записи), откатывает только его записи, а
  `attempts`/`last_error` строки всё равно сохраняются — строка повторяется и в итоге откладывается, как
  при любой другой ошибке, а не остаётся навсегда первой в очереди.

**В SQLite нет ничего похожего на `FOR UPDATE`/`SKIP LOCKED`** — там предполагается один экземпляр
`OutboxRelay` (несколько на одну базу SQLite рискуют доставить дважды), строки забираются одним
запросом вообще без окружающей транзакции, и каждый `event.save()` сразу фиксируется сам.

### Опрос — надёжная основа, LISTEN/NOTIFY — только скорость {: #polling-and-listen-notify }

Опрос (`poll_interval_seconds`) работает всегда и является настоящим источником правды — только он
гарантирует, что каждая строка в итоге будет доставлена. `listen_channel`, если задан, дополнительно
открывает подписку `LISTEN` в PostgreSQL и запускает лишний цикл опроса сразу, как только приходит
подходящий `NOTIFY`, — чтобы не ждать интервала опроса. Серия `NOTIFY` сводится вместе: одновременно
идёт не больше одного такого опроса, и он делает ещё один проход, если за это время пришёл новый
`NOTIFY` или он забрал полную пачку `batch_size`. Это **не** замена опросу: `NOTIFY` в PostgreSQL не
сохраняется — уведомление, отправленное, когда никто не слушает (доставщик ещё не запущен, слушатель
переподключается и т. п.), просто теряется навсегда, без очереди и повторной отправки. `NOTIFY`
отправляется, только если вы об этом попросили, — передайте тот же канал в
`publish(..., notify_channel=...)` (см. [выше](#notify-channel)) или отправьте его сами (или из
триггера базы):

```python
async with Transactions.atomic() as connection:
    await MyOutboxEvent.publish(topic="widget.created", payload={...})
    await connection.notify("my_outbox_channel")
```

Подписка — это [`NotificationListener`](../dialects/postgresql/listen-notify.ru.md), открытый на том же
(выбранном маршрутизатором) подключении, через которое пишет `publish()`. Если её не удаётся
установить или она обрывается и не может переподключиться, доставщик повторяет попытки с растущей
паузой (`backoff_base_seconds * 2**номер_попытки`, без ограничения сверху) до 5 неудачных попыток
подряд, затем пишет в журнал на уровне ERROR и продолжает работать только на опросе — потеря
соединения LISTEN никогда не обрушивает доставщик. Оба драйвера PostgreSQL (`asyncpg` и `rust_pg`)
поддерживают `.listen()`; `listen_channel` не учитывается (об этом один раз пишется в журнал на
уровне ERROR) только на базе вообще без LISTEN/NOTIFY — то есть в SQLite.

### Очистка {: #cleanup }

`OutboxRelay` сам не удаляет старые доставленные строки. Вызывайте `cleanup_published()` сами — с той
периодичностью, которая подходит вашим периодическим задачам:

```python
from datetime import timedelta

deleted = await relay.cleanup_published(older_than=timedelta(days=7))
```

## Исключения {: #exceptions }

`hare.contrib.outbox.DeliveryError` оборачивает то, что бросил `deliver`, — именно оно записывается в
журнал (через `exc_info=`) вместе со строкой WARNING/ERROR о неудачной или отложенной доставке. Оно
никому не передаётся: ошибка одной строки никогда не останавливает обработку остальной пачки.

!!! note "Чего здесь нет"
    Доставки «ровно один раз» (здесь доставка «хотя бы один раз» — `idempotency_key` убирает повторы
    при *публикации*, но ваш `deliver` и всё, чему он передаёт событие, всё равно должны спокойно
    переносить повторы) и версионирования схемы сообщений нет — добавьте их сверху, если нужно.
