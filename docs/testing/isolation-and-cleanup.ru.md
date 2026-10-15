# Изоляция и очистка

Два способа начинать каждый тест с одних и тех же данных: откатывать транзакцию вокруг каждого теста или
очищать таблицы после него.

## <a id="rollback-isolation"></a>Откат транзакции после каждого теста: `RollbackIsolation`

```python
class RollbackIsolation:
    def __init__(self, context: HareContext | None = None) -> None: ...

    async def __aenter__(self) -> HareContext: ...

    def capture_on_commit(
        self, *, execute: bool = False
    ) -> AbstractAsyncContextManager[list[Callable[[], Any]]]: ...
```

Самая быстрая изоляция тестов друг от друга: база создаётся один раз на модуль, а каждый тест идёт внутри
транзакции, которая откатывается, когда он заканчивается.

```python
import pytest_asyncio
from hare.contrib.test import RollbackIsolation, hare_test_context

@pytest_asyncio.fixture(scope="module")
async def database():
    async with hare_test_context(["library.models"]) as context:
        yield context

@pytest_asyncio.fixture
async def isolated_context(database):
    async with RollbackIsolation(database) as context:
        yield context
```

Внутри блока каждое подключение контекста (текущего, если `context` — None) работает в собственной
транзакции, а контекст становится текущим; когда блок заканчивается, каждая транзакция откатывается. У
подключения к базе без транзакций вместо этого очищаются таблицы его моделей
(`truncate_all_models(connections=...)`). Блок `Transactions.atomic()`, который открывает тест, — точка
сохранения изолирующей транзакции, поэтому тест видит то, что записал, а блок с ошибкой откатывается как
обычно. В базе без точек сохранения (ClickHouse с `transactions=true`) блок вместо этого присоединяется к
изолирующей транзакции: записи блока с ошибкой и его колбэки `on_commit()` остаются до конца изоляции —
см. [Транзакции ClickHouse](../dialects/clickhouse/transactions-and-locks.ru.md).

Изолирующая транзакция никогда не фиксируется: попытка зафиксировать её вручную
(`await connection.commit()`) даёт `TransactionManagementError`, а колбэки `on_commit()`, записанные в
тесте, сами не выполняются. `capture_on_commit()` отдаёт их:

```python
isolation = RollbackIsolation(database)
async with isolation:
    async with isolation.capture_on_commit(execute=True) as callbacks:
        await lend_book()            # вызывает Transactions.on_commit(send_due_date_reminder)
    assert len(callbacks) == 1       # send_due_date_reminder выполнился при выходе из блока
```

Список получает колбэки, записанные в блоке, по порядку, когда блок заканчивается; с `execute=True` они
тут же и выполняются (колбэк `async def` ожидается), как и колбэки, которые они в свою очередь
записывают. Колбэка, записанного в откаченной точке сохранения, среди них нет, как и колбэка, который
hare записывает для собственного учёта записи (с `execute=True` он всё равно выполняется).

## <a id="truncate-all-models"></a>Очистка таблиц: `truncate_all_models()`

```python
async def truncate_all_models(
    context: HareContext | None = None,
    *,
    connections: Collection[DatabaseClient] | None = None,
) -> None: ...
```

Удаляет все строки из таблицы каждой зарегистрированной модели в `context` (при `None` — в текущем), а с
`connections` — только моделей этих подключений, каждой на своём подключении. Таблицы очищает диалект
каждого подключения (`clear_tables()` его редактора схемы); они передаются в порядке внешних ключей —
таблица раньше таблиц, на которые она ссылается, — и с `Meta.schema`, если у диалекта есть схемы:

| База данных | Поведение |
|---|---|
| PostgreSQL | Одна команда `TRUNCATE ... CASCADE` по таблицам, в которых есть строки, — сначала он спрашивает, в каких: пустая таблица обходится серверу при очистке так же, как заполненная |
| SQLite | `DELETE` для каждой таблицы, одним скриптом, при выключенных внешних ключах |
| ClickHouse | `TRUNCATE TABLE` для каждой таблицы |

Автоматически созданные промежуточные таблицы `ManyToManyField` тоже очищаются. Модель с
`Meta.managed = False` пропускается — её таблицу (или представление) очищать не hare, — как и модель,
заменённая другой через свою настройку `swappable`: у неё нет таблицы. Даёт `ConfigurationError`, если не
загружено ни одно приложение.

`topological_sort_models(models)` даёт этот порядок для любого списка моделей — модель раньше моделей, на
которые она ссылается: в таком порядке можно удалять их строки.
