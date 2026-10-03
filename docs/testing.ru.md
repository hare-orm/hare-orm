# Тестирование (`hare.contrib.test`)

## `hare_test_context()` — фикстура для pytest {: #hare-test-context }

```python
async def hare_test_context(
    modules: list[str],
    db_url: str = "sqlite://:memory:",
    app_label: str = "models",
    *,
    connection_label: str | None = None,
    use_tz: bool = True,
    timezone: str = DEFAULT_TIMEZONE,
    routers: list[str | type] | None = None,
    _create_db: bool = True,
    _generate_schemas: bool = True,
    _drop_db_on_exit: bool = True,
    reuse_databases: bool | None = None,
) -> AsyncGenerator[HareContext]
```

Рекомендуемый способ подготовить базу для теста: каждый вызов — полностью отдельный `HareContext` (свои
подключения, свой реестр приложений, своя база), поэтому тесты, идущие параллельно (`pytest-xdist`),
никогда не мешают друг другу:

```python
import pytest_asyncio
from hare.contrib.test import hare_test_context


@pytest_asyncio.fixture
async def db():
    async with hare_test_context(["myapp.models"]) as ctx:
        yield ctx


async def test_create_author(db):
    author = await Author.objects.create(name="Ursula K. Le Guin")
    assert author.id is not None
```

Для SQLite в файле такая независимость требует либо своего пути на каждый вызов (фикстура
`tmp_path` из pytest и так своя у каждого рабочего процесса), либо места `{}` в пути
(`db_url="sqlite:///some/dir/test-{}.sqlite"`), которое при каждом вызове заполняется случайным UUID.
SQLite выполняет запись по очереди даже между разными соединениями к одному файлу, поэтому два рабочих
процесса xdist, одновременно работающих с одним и тем же постоянным путём, получили бы
`database is locked`; поэтому при запуске с `-n` (задан `PYTEST_XDIST_WORKER`) hare добавляет к
постоянному пути SQLite идентификатор рабочего процесса. Без `-n` путь используется ровно как задан.

Как и `CREATE DATABASE` в PostgreSQL, создание базы не принимает уже существующую: существующий
файл SQLite даёт `OperationalError` раньше, чем его что-либо коснётся, поэтому `hare_test_context()`
(или `Hare.init(_create_db=True)`), направленный на настоящий файл базы, никогда не удалит его при
выходе. `:memory:` это не касается.

База удаляется при выходе, даже если создание таблиц не удалось при входе в блок: модель, команды
создания которой база отклоняет, не оставляет после себя базу.

### Повторное использование тестовых баз PostgreSQL (`reuse_databases`) {: #reuse-databases }

Каждый `DROP DATABASE` заставляет PostgreSQL запросить контрольную точку, и на сервере с включённым
`fsync` при параллельном запуске тестов это стоит секунд — дольше, чем создать и заполнить базу. При
`reuse_databases=True` адрес PostgreSQL, в имени базы которого есть место `{}`, берёт базу из пула
процесса (`ReusableTestDatabases`), а не создаёт новую со случайным именем:

- место `{}` заполняется идентификатором ячейки пула — 32 шестнадцатеричных символа, полученных из
  номера ячейки (и рабочего процесса `pytest-xdist`), поэтому `test_{}` даёт одни и те же имена при
  каждом запуске;
- при входе база ячейки создаётся, если её ещё нет, сбрасывается, если осталась от прошлого запуска, и
  используется как есть, если этот процесс её уже сбросил;
- при выходе вместо `DROP DATABASE` база сбрасывается, а ячейка возвращается в пул — и тогда, когда блок
  бросил исключение или не удался сам сброс (тогда база сбрасывается перед следующим использованием);
- контекст, в который входят, когда все ячейки заняты (вложенные контексты, несколько подключений в
  одной конфигурации), получает новую ячейку, поэтому пул вырастает до наибольшего числа баз,
  используемых одновременно.

Сброс возвращает базу в состояние только что созданной, не удаляя её: завершает другие подключённые к
ней сеансы (`pg_terminate_backend`), откатывает её подготовленные транзакции, удаляет все схемы, кроме
системных (`DROP SCHEMA ... CASCADE` — это удаляет и установленные в них расширения: `vector`,
`postgis`, `citext`, `btree_gist` и другие), заново создаёт `public` с владельцем, правами и
комментарием, как у новой базы, и очищает настройки уровня базы (`ALTER DATABASE ... RESET ALL` и
`ALTER ROLE ... IN DATABASE ... RESET ALL` для каждой роли, у которой они есть). Ни одна из этих команд
не запрашивает контрольную точку. Объекты вне схем — большие объекты, триггеры событий, публикации —
не удаляются.

`reuse_databases=None` (по умолчанию) читает переменную окружения `HARE_TEST_REUSE_DATABASES`:
`1`/`true` включает пул, `0`/`false` или отсутствие переменной — нет; любое другое значение даёт
`ConfigurationError`. Явный аргумент `True`/`False` важнее переменной. Адреса SQLite и адреса PostgreSQL
без `{}` в пул никогда не попадают.

```bash
HARE_TEST_REUSE_DATABASES=1 pytest -n 8
```

Базы из пула не удаляются при завершении процесса — их забирает следующий запуск, потому что `test_{}`
снова даёт те же имена. Тест, проверяющий сам жизненный цикл базы, — отказ `CREATE DATABASE` для
существующего имени, `DROP DATABASE` при активном соединении — должен передавать
`reuse_databases=False`.

## `HareLoopSwitchWarning` — смена цикла событий между тестами {: #hare-loop-switch-warning }

Выдаётся, когда пул соединений, созданный в одном цикле событий, используется из другого: hare
незаметно открывает новое соединение для нового цикла, но об этом стоит знать. В тестовом окружении
(фикстуры с областью действия функции, `TestClient` Starlette и т. п.) это ожидаемо, и
`hare_test_context()` уже сам это предупреждение подавляет. Если вы работаете не через
`hare_test_context()`, отключите его вручную:

```python
import warnings
from hare.warnings import HareLoopSwitchWarning

warnings.filterwarnings("ignore", category=HareLoopSwitchWarning)
```

Вне тестов это предупреждение обычно указывает на настоящую ошибку — выясните, почему цикл событий
изменился между созданием соединения и его использованием.

## `requires_features()` — пропуск теста по возможностям базы {: #requires-features }

```python
def requires_features(connection_name: str | None = None, **conditions: Any) -> Callable[[FT], FT]
```

```python
@requires_features(supports_transactions=True)
async def test_rolls_back(db): ...


@requires_features(dialect="sqlite")
async def test_sqlite_sql(db): ...
```

Каждое условие называет поле `Features` подключения (`supports_transactions`, `supports_returning` и
т. п.), иначе — атрибут его диалекта (`supports_distinct_on`, `supports_partial_indexes` и т. п.), или `dialect`
— имя диалекта; см. [Диалекты и их возможности](dialects/dialects-and-features.ru.md). Тест, которому нужна возможность, называет эту
возможность, поэтому выполняется на каждом диалекте, где она есть; `dialect=` — для теста собственного
SQL или типов одного диалекта.

Каждое условие проверяется для подключения `connection_name` текущего контекста — для подключения по
умолчанию при `None` (единственного, которое создаёт `hare_test_context()`; если подключений несколько
и ни одно не называется `"default"` — для первого настроенного), — и если хоть одно не совпадает,
бросается `unittest.SkipTest`. Работает как декоратор отдельной тестовой функции или целого тестового
класса (действует на каждый метод `test_*`).

## `assert_num_queries()` / `capture_queries()` — ловить проблему N+1 {: #assert-num-queries }

```python
async def capture_queries(using: str | DatabaseClient | None = None) -> AsyncGenerator[QueryCounter]

async def assert_num_queries(expected: int, *, using: str | DatabaseClient | None = None) -> AsyncGenerator[QueryCounter]
```

```python
async with assert_num_queries(1):
    await Event.objects.all().select_related("tournament")
```

`assert_num_queries` падает с `AssertionError`, перечисляя текст SQL каждого перехваченного запроса,
если блок выполнил не ровно `expected` запросов, — прямой способ превратить пропущенный
`select_related()`/`prefetch_related()` в упавший тест, а не обнаружить его в продакшене.
`capture_queries` — более простой инструмент, на котором он построен, для случая, когда нужно только
число и список запросов без проверки:

```python
async with capture_queries() as counter:
    await Event.objects.all().select_related("tournament")
assert counter.count == 1
```

`QueryCounter` (`count: int`, `queries: list[str]`) обновляется по ходу выполнения блока, а не только
при выходе. Каждая команда считается ровно один раз — загрузка моделей, `values()`/`values_list()`,
`aggregate()`, `count()`/`exists()`, записи, готовый SQL, каждый `stream()` и каждая пачка
`bulk_create(use_copy=True)` (записывается как `COPY <таблица> (<колонки>) FROM STDIN`), — даже если
один метод клиента внутри вызывает другой.

## `init_memory_sqlite()` — быстрые скрипты {: #init-memory-sqlite }

```python
from hare import fields, models, run_async
from hare.contrib.test import init_memory_sqlite


class MyModel(models.Model):
    id = fields.IntField(primary_key=True)
    name = fields.TextField()


@init_memory_sqlite
async def run():
    obj = await MyModel.objects.create(name="")
    assert obj.id == 1


if __name__ == "__main__":
    run_async(run())
```

Поднимает базу SQLite в памяти и создаёт таблицы перед вызовом обёрнутой функции — для разовых
скриптов и примеров, а не для наборов тестов (для них используйте `hare_test_context()`). Принимает
необязательный `models=[...]` (по умолчанию `["__main__"]`), если модели лежат не в вызывающем модуле.

## `truncate_all_models()` — быстрая очистка между тестами {: #truncate-all-models }

```python
async def truncate_all_models(context: HareContext | None = None) -> None
```

Удаляет все строки из таблицы каждой зарегистрированной модели в `context` (при `None` — в текущем).
Таблицы очищает диалект каждого подключения (`Dialect.clear_tables()`); они передаются в порядке
внешних ключей — таблица раньше таблиц, на которые она ссылается, — и с `Meta.schema`, если у диалекта
есть схемы: PostgreSQL — одной командой `TRUNCATE ... CASCADE` по таблицам, в которых есть строки
(сначала он спрашивает, в каких: пустая таблица обходится серверу при очистке так же, как заполненная),
SQLite — `DELETE` для каждой таблицы, одним скриптом, при выключенных внешних ключах. Автоматически созданные промежуточные таблицы `ManyToManyField` тоже
очищаются. Модель с `Meta.managed = False` пропускается — её таблицу (или представление) очищать не
hare-orm, — как и модель, заменённая другой через свою настройку `swappable`: у неё нет таблицы. Даёт
`ConfigurationError`, если не загружено ни одно приложение.

## Сравнение значений (`hare.contrib.test.condition`) {: #value-matchers }

Из пакета `hare.contrib.test` верхнего уровня не экспортируются — импортируйте их прямо из подмодуля:

```python
from hare.contrib.test.condition import In, NotEQ, NotIn
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
