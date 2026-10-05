# taskiq

`hare.contrib.taskiq` запускает hare в воркерах [taskiq](https://taskiq-python.github.io/): контекст
Hare воркера, команды каждой задачи с меткой задачи, арендатор задачи, транзакция на задачу с
повтором после конфликта и задачи, которые отправляются только после фиксации запросившей их
транзакции. Установите дополнение:

```bash
pip install "hare-orm[taskiq]"
```

## <a id="hare-taskiq"></a>Подключение к брокеру

```python
from taskiq_redis import ListQueueBroker

from hare.contrib.taskiq import HareTaskiq

broker = ListQueueBroker("redis://localhost:6379")
hare_taskiq = HareTaskiq(broker, HARE_ORM, atomic_tasks=True, transaction_retries=3)


@broker.task
async def charge_order(order_id: int) -> None:
    order = await Order.objects.get(id=order_id)
    ...
```

`HareTaskiq(broker, config, *, atomic_tasks=False, tenant_label="tenant", transaction_retries=0)`:

- открывает контекст Hare по `config` (как `Hare.init(config=...)`), когда воркер запускается
  (`WORKER_STARTUP`), и закрывает его подключения, когда воркер останавливается (`WORKER_SHUTDOWN`);
  контекст — общий запасной, его видит каждая задача воркера;
- добавляет брокеру `HareTaskiqMiddleware` (`hare_taskiq.middleware`) — создавайте `HareTaskiq` в
  модуле, из которого и приложение, и воркер берут брокер: отправляющей стороне middleware тоже
  нужен, чтобы записывать арендатора в отправляемые задачи.

Приложение, которое отправляет задачи, сохраняет свой контекст Hare (`Hare.init()`, плагин
веб-фреймворка); `HareTaskiq` открывает только контекст воркера.

## <a id="middleware"></a>Что получает каждая задача

`HareTaskiqMiddleware(*, atomic_tasks=False, tenant_label="tenant", transaction_retries=0)`:

- **Метки запросов** — каждая команда задачи несёт `task_name` и `task_id` в завершающем
  комментарии ([метки в комментариях SQL](../observability/query-tags.ru.md)), рядом с уже активными метками.
- **Арендатор** — при отправке задачи middleware записывает активную тогда область арендатора
  (`Tenancy.scope(...)`) в метку `tenant_label` — одно значение арендатора: строку, целое число или
  UUID (отправляется текстом); метка, заданная вручную (`task.kicker().with_labels(tenant=...)`),
  сохраняется. Воркер выполняет задачу внутри `Tenancy.scope(<значение метки>)`. `tenant_label=None`
  арендаторов не трогает.
- **Транзакция** — с `atomic_tasks=True` задача выполняется в транзакции на подключении по умолчанию
  (имена подключений — по транзакции на каждом); транзакция фиксируется, когда задача возвращает
  результат, и откатывается, когда задача падает. Неудачная фиксация роняет задачу. Обработчики
  `Transactions.on_commit()` выполняются после фиксации — `TaskiqDelivery.kiq_on_commit()` из задачи
  будит relay именно тогда.
- **Повтор после конфликта** — задача, транзакция которой получила `TransactionRetryError` (сбой
  сериализации, взаимная блокировка — в задаче или при фиксации), откатывается и отправляется заново
  под своим же id, не больше `transaction_retries` раз; метка `hare_transaction_retries` считает
  повторы. Результат неудачной попытки не сохраняется, поэтому `wait_result()` возвращает удачную
  попытку — или последний сбой. Вместе с `SimpleRetryMiddleware` из taskiq не включайте
  `TransactionRetryError` в его исключения, иначе конфликт повторится дважды.

Всё, что middleware настроил, отменяется, когда задача заканчивается, — метки, область арендатора,
выполняемая задача. Синхронная задача выполняется в потоке, где асинхронный API hare недоступен.

Параметры проверяются при создании, иначе `ConfigurationError`: `atomic_tasks` — bool или
последовательность разных имён подключений, `tenant_label` — `None` или непустая строка,
`transaction_retries` — `int` в `0..100` (`hare.contrib.taskiq.constants.MAX_TRANSACTION_RETRIES`).
Имя подключения, которого нет в конфиге, отклоняется при запуске воркера.

## <a id="kiq-on-commit"></a>Отправка задач после фиксации

Задача, отправленная изнутри транзакции, может выполниться до её фиксации — и не увидеть её записей —
или выполниться для транзакции, которая потом откатится. `TaskiqDelivery` отправляет её через
[транзакционный outbox](outbox.ru.md) — только после фиксации:

```python
from hare.contrib.outbox import OutboxEvent
from hare.contrib.taskiq import TaskiqDelivery


class TaskOutboxEvent(OutboxEvent):
    class Meta(OutboxEvent.Meta):
        table = "task_outbox"


tasks = TaskiqDelivery(broker, model=TaskOutboxEvent)

async with tasks.get_relay(poll_interval_seconds=1.0):
    async with Transactions.atomic():
        order = await Order.objects.create(total=100)
        await tasks.kiq_on_commit(charge_order, order.id)
    # charge_order отправляется здесь, сразу после фиксации.
```

`TaskiqDelivery(broker, *, model, topic="taskiq", using=None, wakeup=None)` — доставка outbox,
отправляющая задачи, и отправитель, который их записывает. `await kiq_on_commit(task, *args,
labels=None, **kwargs)` принимает задачу или её имя, её аргументы — значения JSON — и метки и
записывает задачу событием outbox с темой `topic` (`OutboxEvent.enqueue()`) — в текущей транзакции,
поэтому откат не оставляет ни события, ни задачи; вне транзакции событие фиксируется сразу. Возвращает
событие.

Задачи отправляет relay из `get_relay(**relay_options)` — только события темы доставки, поэтому в
таблице могут быть и другие темы. Будильник доставки — по умолчанию свой `InProcessWakeup` — запускает
relay сразу после фиксации, поэтому запускайте relay в процессе, который отправляет задачи; чтобы
отправлять сразу из другого процесса, передайте будильник брокера (`RedisWakeup` и т. п.). Задачу,
которую брокер не принял (был недоступен), relay повторяет, как любое событие outbox.

Доставка — как минимум один раз: задача может прийти дважды, поэтому делайте задачи безопасными для
повтора. `using` задаёт подключение, на котором пишутся задачи, — по умолчанию подключение модели.
Пустой `topic` даёт `ConfigurationError`.

## <a id="testing"></a>Тестирование

`InMemoryBroker(await_inplace=True)` из taskiq выполняет каждую задачу при отправке, в отправляющей
задаче, — с добавленным `HareTaskiqMiddleware` тесты видят те же метки, арендаторов и транзакции,
что и воркеры:

```python
broker = InMemoryBroker(await_inplace=True)
broker.add_middlewares(HareTaskiqMiddleware(atomic_tasks=True))
```
