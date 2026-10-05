# Метрики пула и здоровье

Что держит и что сделал пул соединений и насколько пригодно каждое подключение. На странице:

- снимок каждого пула;
- события исчерпанного ожидания и неудачного подключения;
- метрики пула в OpenTelemetry;
- проверка здоровья с критериями на ваш выбор — для проверки готовности, страницы мониторинга или
  оповещения.

## <a id="pool-status"></a>Снимок пула

```python
from hare import Connections

status = Connections.get("default").get_pool_status()
status.size, status.idle, status.in_use, status.waiting   # 20, 0, 20, 3

for status in Connections.get_pool_statuses():            # все открытые пулы текущего контекста
    print(status.connection_alias, status.role, status.schema, status.in_use, status.waiting)
```

`get_pool_status()` возвращает `PoolStatus` (`hare.instrumentation.declarations`) или None, пока пул
клиента ещё не открыт:

| Поле | Что это |
|---|---|
| `connection_alias` | Подключение из конфигурации. |
| `role` | Какому клиенту подключения принадлежит пул (ниже). |
| `schema` | Схема арендатора у пула схемы арендатора, иначе None. |
| `size`, `idle`, `in_use` | Открытые соединения, свободные и занятые. |
| `waiting` | Задачи, которые сейчас ждут соединения. |
| `min_size`, `max_size` | Границы пула. |
| `acquire_count` | Сколько раз соединение было взято. |
| `acquire_timeouts` | Сколько ожиданий соединения исчерпали `pool_acquire_timeout`. |
| `acquire_wait_seconds_total` | Сколько длились ожидания в сумме — меряется, только пока включён `PoolMetrics`. |
| `connect_count`, `connect_failures` | Сколько соединений пул открыл и сколько раз открыть не удалось. |

Счётчики принадлежат клиенту, а не его пулу: они переживают `Connections.reconnect()`, который
заменяет пулы. У SQLite «пул» — единственное соединение клиента: `size` равен 1, а `waiting` считает
задачи и транзакции, которые его ждут.

У подключения может быть несколько пулов, у каждого своя `PoolRole` (`hare.instrumentation.enums`):

| Роль | Пул |
|---|---|
| `OWN` | Собственного клиента подключения. |
| `TENANT_SCHEMA` | Клиента схемы одного арендатора ([схема на арендатора](../soft-delete-versions-tenants/schema-per-tenant.ru.md)). |
| `DIRECT` | Клиента самого сервера в обход пулера транзакций (`direct_host`, [Пулеры соединений](../connections/connection-poolers.ru.md)). |
| `INDEPENDENT` | Отдельного клиента — `Transactions.autonomous()`, сессии миграции с тайм-аутом блокировки. |

`Connections.get_pool_statuses(connection_alias=None)` перечисляет все открытые пулы текущего
контекста — по подключению, роли и схеме. Клиента, чей драйвер не сообщает о пуле
(`Features.supports_pool_status` выключен — сторонний диалект), в списке нет, а его
`get_pool_status()` бросает `UnSupportedError`.

## <a id="pool-metrics"></a>Замер ожиданий: `PoolMetrics`

Сколько длится каждое ожидание соединения, меряется, только пока включены метрики пула. Без них
взятие соединения не добавляет ни одного вызова:

```python
from hare.instrumentation.pools.pool_metrics import PoolMetrics

PoolMetrics.enable()
...
PoolMetrics.disable()
```

Каждому `enable()` соответствует `disable()`: замер останавливается, когда его выключили все, кто
включал. Инструментатор OpenTelemetry включает его на время работы. Время открытия соединения
меряется всегда: это происходит раз на соединение и никогда — на пути запроса.

## <a id="events"></a>События

Два события [наблюдателей](observers.ru.md), только при сбое:

- `PoolAcquireTimedOut` — ожидание соединения исчерпало `pool_acquire_timeout`, вызов бросает
  `PoolTimeoutError` (это `DBConnectionError`). В событии: подключение, роль, схема, сколько длилось
  ожидание, а также `size`, `max_size` и `waiting` пула в этот момент.
- `ConnectionFailed` — открыть соединение не удалось: при открытии пула (каждая попытка
  `connect_max_retries`) или при его росте. В событии: подключение, роль, адрес сервера, имя класса
  исключения (не его текст — в нём бывают пользователь и хост), номер попытки и будет ли следующая.

```python
from hare.instrumentation import Observers
from hare.instrumentation.declarations import PoolAcquireTimedOut

Observers.observe(PoolAcquireTimedOut, lambda event: alert(f"пул {event.connection_alias} исчерпан"))
```

## <a id="opentelemetry-metrics"></a>Метрики OpenTelemetry

`OpenTelemetryInstrumentor` ([OpenTelemetry](opentelemetry.ru.md)) сообщает о каждом открытом пуле
процесса. Имена — по семантическим соглашениям OpenTelemetry для клиентов баз данных (их статус пока
«development»):

| Метрика | Тип | Значение |
|---|---|---|
| `db.client.connection.count` | up-down counter, `db.client.connection.state` = `idle`/`used` | `idle`, `in_use` |
| `db.client.connection.max` | up-down counter | `max_size` |
| `db.client.connection.idle.min` | up-down counter | `min_size` |
| `db.client.connection.pending_requests` | up-down counter | `waiting` |
| `db.client.connection.timeouts` | counter | `acquire_timeouts` |
| `db.client.connection.wait_time` | гистограмма, секунды | каждое ожидание соединения |
| `db.client.connection.create_time` | гистограмма, секунды | каждое подключение |
| `hare.pool.connect_failures` | counter | `connect_failures` |

У каждой метрики есть атрибуты:

- `db.client.connection.pool.name` (подключение);
- `hare.pool.role`;
- `db.system.name`;
- у пула схемы арендатора — `hare.pool.schema`.

Пулы с одинаковыми атрибутами (одно подключение в нескольких `HareContext` процесса) суммируются.

Значения читаются при сборе метрик, в потоке экспортёра. Гистограмму нельзя наблюдать, поэтому
ожидания и подключения, замеренные после прошлого сбора, записываются во время сбора. Каждый пул хранит последние 4096 длительностей каждого вида для своих
читателей; отставший читатель теряет самые старые.

## <a id="health-check"></a>Проверка здоровья

```python
from hare.health import ConnectionCriteria, HealthCheck
from hare.health.criteria import PingFailed, PoolSaturation, WaitingRequests

health_check = HealthCheck(
    timeout_seconds=2.0,
    degraded_when=[WaitingRequests(at_least=1), PoolSaturation(ratio=0.9)],   # по умолчанию
    unhealthy_when=[PingFailed()],                                            # по умолчанию
    criteria_by_connection={"analytics": ConnectionCriteria(degraded_when=[PoolSaturation(ratio=1.0)])},
)
report = await health_check.run()
report.status                                    # HealthStatus.DEGRADED
report.connections["default"].reasons            # ("3 tasks wait for a connection of the own pool (at least 1)",)
```

`run()` одновременно пингует все подключения текущего контекста, каждое не дольше `timeout_seconds`,
и судит его по критериям:

- сработал критерий неработоспособности — `UNHEALTHY`;
- иначе сработал критерий деградации — `DEGRADED`;
- иначе — `HEALTHY`.

Статус отчёта (`status`) — статус худшего подключения.

| Аргумент | По умолчанию | Что это |
|---|---|---|
| `timeout_seconds` | 2.0 | Сколько ждёт пинг — больше 0, не больше 60. |
| `degraded_when` | `[WaitingRequests(at_least=1), PoolSaturation(ratio=0.9)]` | Критерии деградации — `[]`, чтобы её не было никогда. |
| `unhealthy_when` | `[PingFailed()]` | Критерии неработоспособности. |
| `criteria_by_connection` | нет | Другие критерии для некоторых подключений (`ConnectionCriteria`); список, оставленный None, берётся у проверки. |
| `connections` | все подключения | Какие подключения проверять. |
| `check_direct` | False | Пинговать ещё и сервер за пулером транзакций подключения (`direct_host`). |

- `run()` не бросает исключений о базе. Неудачный пинг — отказ в соединении, неверный пароль,
  сервер не ответил вовремя — это статус `UNHEALTHY` с именем класса исключения. `ConfigurationError`
  бывает только без текущего контекста или для имени подключения, которого нет в конфигурации.
- `run(ping=False)` оценивает только состояние пулов и не открывает ни одного пула — `PingFailed` и
  `PingLatency` тогда не срабатывают.
- Держите один объект `HealthCheck`: критерии «с прошлой проверки» сравнивают с его предыдущим запуском.
- Каждый аргумент проверяется по типу и диапазону при создании объекта.

### <a id="criteria"></a>Критерии

| Критерий (`hare.health.criteria`) | Срабатывает, когда |
|---|---|
| `PingFailed()` | пинг не удался или не ответил вовремя — или, с `check_direct`, пинг сервера за пулером |
| `PingLatency(max_ms)` | пинг дольше `max_ms` (больше 0, не больше 60000) |
| `PoolSaturation(ratio)` | у пула занято не меньше доли `ratio` от `max_size` (больше 0, не больше 1) |
| `WaitingRequests(at_least)` | соединения пула ждут не меньше `at_least` задач |
| `AcquireWaitTime(max_seconds)` | ожидание соединения с прошлой проверки длилось дольше `max_seconds` — нужен включённый при создании проверки `PoolMetrics`, иначе `ConfigurationError` |
| `AcquireTimeouts(at_least)` | с прошлой проверки исчерпано не меньше `at_least` ожиданий |
| `ConnectFailures(at_least)` | с прошлой проверки не удалось открыть соединение не меньше `at_least` раз |
| `AllOf(*criteria)` | сработали все его критерии |

Свой критерий — подкласс `HealthCriterion`, который возвращает причину из `check()`:

```python
from hare.health.criteria import HealthCriterion

class TenantPoolsBusy(HealthCriterion):
    def check(self, connection_check):
        busy = [pool for pool in connection_check.pools if pool.status.schema and pool.status.waiting]
        return f"{len(busy)} tenant pools have waiting tasks" if len(busy) > 2 else None
```

`connection_check` (`ConnectionCheck`) содержит пинг, пинг сервера за пулером и каждый пул
(`PoolChange`). Для пула — его снимок и что он сделал с прошлой проверки: взятые соединения,
исчерпанные ожидания, неудачные подключения, замеренные ожидания.

### <a id="report"></a>Отчёт

`HealthReport.to_dict()` отдаёт отчёт значениями JSON. По умолчанию — только статусы, такой ответ
можно показывать кому угодно. `to_dict(include_details=True)` добавляет для каждого подключения
задержку пинга, причины, имя класса исключения и его пулы:

```json
{
  "status": "degraded",
  "checked_at": "2026-10-06T14:03:11.204871+00:00",
  "connections": {
    "default": {
      "status": "degraded",
      "latency_ms": 1.8,
      "reasons": ["3 tasks wait for a connection of the own pool (at least 1)"],
      "error_type": null,
      "pools": [{"connection_alias": "default", "role": "own", "schema": null, "size": 20, "idle": 0,
                 "in_use": 20, "waiting": 3, "min_size": 5, "max_size": 20, "acquire_count": 18211,
                 "acquire_timeouts": 0, "acquire_wait_seconds_total": 3.42, "connect_count": 20,
                 "connect_failures": 0}]
    }
  }
}
```

Текст исключения в отчёт не попадает никогда — в нём бывают адрес сервера и пользователь.

### <a id="liveness-and-readiness"></a>Проверки работоспособности и готовности

Проверка готовности (readiness) спрашивает «может ли экземпляр принимать трафик» — это
`HealthCheck.run()`. Проверка работоспособности (liveness) спрашивает «жив ли процесс» и не должна
трогать базу. Если бы она падала при недоступной базе, оркестратор перезапускал бы все экземпляры
приложения во время аварии. Интеграции фреймворков добавляют оба маршрута
([FastAPI](../integrations/fastapi.ru.md#health-routes), [Litestar](../integrations/litestar.ru.md#health-routes),
[Robyn](../integrations/robyn.ru.md#health-routes)):

```python
from hare.contrib.frameworks.health_routes import HealthRoutes

app = HareFastAPI(
    hare_config=HARE_CONFIG,
    health_routes=HealthRoutes(
        health_check,
        readiness_path="/health/ready",   # по умолчанию
        liveness_path="/health/live",     # по умолчанию
        degraded_status_code=200,         # по умолчанию: при деградации трафик ещё принимается
        include_details=False,            # по умолчанию: только статусы
    ),
)
```

| Статус | `/health/ready` | `/health/live` |
|---|---|---|
| healthy | 200 | 200 |
| degraded | `degraded_status_code` | 200 |
| unhealthy | 503 | 200 |

Ни один маршрут не работает в транзакции запроса (`atomic_requests`) и не попадает в схему OpenAPI.

```yaml
readinessProbe: {httpGet: {path: /health/ready, port: 8000}, periodSeconds: 5, timeoutSeconds: 3}
livenessProbe:  {httpGet: {path: /health/live,  port: 8000}, periodSeconds: 10}
```

### <a id="behind-pgbouncer"></a>За PgBouncer

Через пулер транзакций пинг доходит до сервера через пулер. `check_direct=True` пингует ещё и сам
сервер — по `direct_host`/`direct_port` подключения ([Пулеры соединений](../connections/connection-poolers.ru.md)).
Так видно, когда пулер работает, а сервер нет, и наоборот. У подключения через пулер без
`direct_host` этот пинг не удаётся с `ConfigurationError`.
