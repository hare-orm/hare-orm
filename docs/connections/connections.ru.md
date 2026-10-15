# Подключения

`Connections` хранит открытые подключения контекста: подключение по имени, переподключение после замены
базы, проверка, что сервер отвечает. Как подключиться к каждой базе — драйверы, параметры адреса, TLS —
описано на странице этой базы: [PostgreSQL](../dialects/postgresql/connecting.ru.md),
[SQLite](../dialects/sqlite/connecting.ru.md), [ClickHouse](../dialects/clickhouse/connecting.ru.md).

## <a id="connections"></a>`Connections` / `ConnectionHandler`

```python
class Connections:
    @staticmethod
    def get(connection_alias: str) -> DatabaseClient: ...

    @staticmethod
    def get_client(using: str | DatabaseClient | None) -> DatabaseClient | None: ...

    @staticmethod
    def current() -> ConnectionHandler: ...

    @staticmethod
    def aliases() -> list[str]: ...

    @staticmethod
    def get_pool_statuses(connection_alias: str | None = None) -> list[PoolStatus]: ...

    @staticmethod
    def create_task_outside_transactions(
        coroutine: Coroutine[Any, Any, TaskResult],
    ) -> asyncio.Task[TaskResult]: ...

    @staticmethod
    async def reconnect() -> None: ...
```

`ConnectionHandler` (доступен как `context.connections` у `HareContext`) — настоящий владелец подключений
контекста:

```python
class ConnectionHandler:
    # создаёт клиента при первом обращении
    def get(self, connection_alias: str) -> DatabaseClient: ...

    # на нём выполняется Transactions.autonomous()
    def create_independent(
        self,
        connection_alias: str,
        credential_overrides: Mapping[str, Any] | None = None,
    ) -> DatabaseClient: ...

    def all(self) -> list[DatabaseClient]: ...

    def aliases(self) -> list[str]: ...

    async def close_all(self, discard: bool = True) -> None: ...
```

`get_client(using)` — подключение, которое называет аргумент `using=`: клиент по имени, сам клиент или
`None`. `get_pool_statuses()` перечисляет открытые пулы ([Метрики пула и
здоровье](../observability/pool-health.ru.md)). `create_task_outside_transactions(coroutine)` запускает
задачу, которая не видит транзакций вызывающего кода: каждое имя подключения даёт там общий клиент.

`close_all()` закрывает все подключения контекста — сколько он ждёт ещё идущие запросы, описано в
[Закрытии подключений](configuration.ru.md#closing-connections). При `discard=True` (по умолчанию; так
закрывает `Hare.close_connections()`) выбрасываются и сами объекты подключений: задача, которая ещё
держит такой объект, на следующем запросе получает `DBConnectionError`, а не открывает новый пул, который
никто бы не закрыл. При `discard=False` объекты остаются и на следующем запросе открывают новые
соединения — так делает [`Connections.reconnect()`](#reconnect).

`aliases()` возвращает имена всех подключений, настроенных для текущего контекста (из настроек
подключений `Hare.init()`), — не клиентов, а только имена, например чтобы перебрать все подключения при
проверке работоспособности.

`hare.core.constants.DEFAULT_CONNECTION_NAME` (`"default"`) — имя, которое `HareConfig.from_db_url(...)`
даёт своему единственному подключению, подключение приложения без `default_connection` и подключение, на
котором `Transactions.atomic()` без `using` открывает транзакцию, когда подключения приложений по
умолчанию различаются.

Чтобы узнать, на какое подключение идут запросы модели с учётом маршрутизаторов, используйте
[`Model.get_connection()`](../models/model-methods.ru.md#get-connection) или `QuerySet.get_connection()`.

### <a id="reconnect"></a>Переподключение после замены базы

```python
await Connections.reconnect()
```

Закрывает все подключения и пулы текущего контекста, сохраняя их настройки; следующий запрос открывает
новые подключения. Нужно, когда базу заменили под работающим приложением: файл SQLite подменили копией,
базу PostgreSQL восстановили из дампа или оборвали её подключения через `pg_terminate_backend()`. Без
него драйверы ведут себя с закрытыми сервером соединениями по-разному: `rust_pg` проверяет соединение из
пула перед выдачей и открывает новое, а asyncpg узнаёт об этом только при использовании — первая команда
на каждом таком соединении даёт `DBConnectionError` (чтение вне транзакции выполняется снова с
`read_retry_max_retries`), и пул заменяет соединение.

- Объекты подключений и их имена остаются прежними — код, который держит `Connections.get("default")`,
  продолжает работать.
- Вызывайте без открытой транзакции. Запрос, который уже получил соединение, доработает на нём; запрос,
  который ещё ждал соединения, когда старый пул закрылся, выполнится на новом пуле — запросы, идущие в
  это время в других задачах, не падают.
- Каждый процесс переподключает свои подключения сам; пул рабочих процессов вызывает это в каждом.
- Без активного контекста даёт `ConfigurationError`.

### <a id="health-checks"></a>Проверка связи и неотвечающий сервер

```python
ok: bool = await Connections.get("default").ping(timeout=5.0)
```

Полная проверка здоровья — все соединения пингуются одновременно, их пулы оцениваются по критериям, есть
маршрут готовности веб-приложения — это `HealthCheck` ([Метрики пула и
здоровье](../observability/pool-health.ru.md)).

`ping()` выполняет `SELECT 1` и возвращает `True`/`False` вместо исключения. Он всегда ограничен своим
временем ожидания (по умолчанию 5 секунд, аргумент `timeout=`), независимо от настроек подключения:
сервер, который перестал отвечать, не закрыв соединение (сеть разделилась, процесс завис), не заставляет
ни один драйвер выдать ошибку, поэтому вызов без ограничения ждал бы вечно. Возвращает `False` при
`DBConnectionError`, `OperationalError` и по истечении времени; всё остальное передаётся дальше. Работает
одинаково на любой базе.

У обычных запросов по умолчанию такого ограничения нет — долгую миграцию или массовую операцию прерывать
нельзя. В PostgreSQL ограничение включается параметром `command_timeout` (секунды) в `credentials` или в
адресе (`?command_timeout=30`): команда, превысившая его, отменяется и даёт `TimeoutError`, а соединение
остаётся рабочим — см. [`command_timeout`](../dialects/postgresql/connecting.ru.md#command-timeout).
