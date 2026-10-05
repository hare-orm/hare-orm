# Изоляция и очистка

Два способа начинать каждый тест с одних и тех же данных: откатывать транзакцию вокруг каждого
теста или очищать таблицы после него.

## <a id="rollback-isolation"></a>`RollbackIsolation` — откат транзакции после каждого теста

```python
class RollbackIsolation:
    def __init__(self, context: HareContext | None = None) -> None
    async def __aenter__(self) -> HareContext
    def capture_on_commit(self, *, execute: bool = False) -> AsyncContextManager[list[Callable[[], Any]]]
```

Самая быстрая изоляция тестов друг от друга: база создаётся один раз на модуль, а каждый тест идёт
внутри транзакции, которая откатывается, когда он заканчивается.

```python
import pytest_asyncio
from hare.contrib.test import RollbackIsolation, hare_test_context

@pytest_asyncio.fixture(scope="module")
async def database():
    async with hare_test_context(["myapp.models"]) as context:
        yield context

@pytest_asyncio.fixture
async def db(database):
    async with RollbackIsolation(database) as context:
        yield context
```

Внутри блока каждое подключение контекста (текущего, если `context` — None) работает в собственной
транзакции, а контекст становится текущим; когда блок заканчивается, каждая транзакция откатывается.
У подключения к базе без транзакций вместо этого очищаются таблицы его моделей
(`truncate_all_models(connections=...)`). Блок `Transactions.atomic()`, который открывает тест, —
точка сохранения изолирующей транзакции, поэтому тест видит то, что записал, а блок с ошибкой
откатывается как обычно.

Изолирующая транзакция никогда не фиксируется: попытка зафиксировать её вручную
(`await connection.commit()`) даёт `TransactionManagementError`, а колбэки `on_commit()`, записанные в
тесте, сами не выполняются. `capture_on_commit()` отдаёт их:

```python
isolation = RollbackIsolation(database)
async with isolation:
    async with isolation.capture_on_commit(execute=True) as callbacks:
        await place_order()          # вызывает Transactions.on_commit(send_receipt)
    assert len(callbacks) == 1       # send_receipt выполнился при выходе из блока
```

Список получает колбэки, записанные в блоке, по порядку, когда блок заканчивается; с `execute=True`
они тут же и выполняются (колбэк `async def` ожидается), как и колбэки, которые они в свою очередь
записывают. Колбэка, записанного в откаченной точке сохранения, среди них нет.

## <a id="truncate-all-models"></a>`truncate_all_models()` — быстрая очистка между тестами

```python
async def truncate_all_models(context: HareContext | None = None, *, connections: Collection[DatabaseClient] | None = None) -> None
```

Удаляет все строки из таблицы каждой зарегистрированной модели в `context` (при `None` — в текущем),
а с `connections` — только моделей этих подключений, каждой на своём подключении.
Таблицы очищает диалект каждого подключения (`clear_tables()` его редактора схемы); они передаются в порядке
внешних ключей — таблица раньше таблиц, на которые она ссылается, — и с `Meta.schema`, если у диалекта
есть схемы: PostgreSQL — одной командой `TRUNCATE ... CASCADE` по таблицам, в которых есть строки
(сначала он спрашивает, в каких: пустая таблица обходится серверу при очистке так же, как заполненная),
SQLite — `DELETE` для каждой таблицы, одним скриптом, при выключенных внешних ключах, ClickHouse —
`TRUNCATE TABLE` для каждой таблицы. Автоматически созданные промежуточные таблицы `ManyToManyField` тоже
очищаются. Модель с `Meta.managed = False` пропускается — её таблицу (или представление) очищать не
hare-orm, — как и модель, заменённая другой через свою настройку `swappable`: у неё нет таблицы. Даёт
`ConfigurationError`, если не загружено ни одно приложение.

`topological_sort_models(models)` даёт этот порядок для любого списка моделей — модель раньше моделей,
на которые она ссылается: в таком порядке можно удалять их строки.
