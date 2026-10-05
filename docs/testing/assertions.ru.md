# Проверки запросов и значений

Проверки, которые тест делает помимо прочитанных значений: сколько запросов выполняет блок кода, и
сравнение значения по правилу, а не на равенство.

## <a id="assert-num-queries"></a>`assert_query_count()` / `capture_queries()` — ловить проблему N+1

```python
async def capture_queries(using: str | DatabaseClient | None = None) -> AsyncGenerator[QueryCounter]

async def assert_query_count(expected: int, *, using: str | DatabaseClient | None = None) -> AsyncGenerator[QueryCounter]
```

```python
async with assert_query_count(1):
    await Event.objects.all().select_related("tournament")
```

`assert_query_count` падает с `AssertionError`, перечисляя текст SQL каждого перехваченного запроса,
если блок выполнил не ровно `expected` запросов, — прямой способ превратить пропущенный
`select_related()`/`prefetch_related()` в упавший тест, а не обнаружить его в продакшене.
`capture_queries` — более простой инструмент, на котором он построен, для случая, когда нужно только
число и список запросов без проверки:

```python
async with capture_queries() as counter:
    await Event.objects.all().select_related("tournament")
assert counter.count == 1
```

`QueryCounter` (`connection_alias: str`, `count: int`, `queries: list[str]`) обновляется по ходу выполнения блока, а не только
при выходе. Каждая команда считается ровно один раз — загрузка моделей, `values()`/`values_list()`,
`aggregate()`, `count()`/`exists()`, записи, готовый SQL, каждый `stream()` и каждая партия
`bulk_create(use_copy=True)` (записывается как `COPY <таблица> (<колонки>) FROM STDIN`), — даже если
один метод клиента внутри вызывает другой.

## <a id="value-matchers"></a>Сравнение значений (`hare.contrib.test.conditions`)

Из пакета `hare.contrib.test` верхнего уровня не экспортируются — импортируйте их прямо из подмодуля:

```python
from hare.contrib.test.conditions import In, NotEQ, NotIn
```

Используются справа от сравнения `==`, например внутри словаря, который в проверке сравнивается с
настоящими данными:

| Класс | С чем совпадает |
|---|---|
| `NotEQ(value)` | С чем угодно, что не равно `value`. |
| `In(*values)` | С чем угодно из `values`. |
| `NotIn(*values)` | С чем угодно, чего нет в `values`. |

```python
assert response.json() == {"id": In(*known_ids), "status": NotEQ("deleted")}
```
