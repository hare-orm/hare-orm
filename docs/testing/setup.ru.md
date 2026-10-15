# Настройка тестов

Как набор тестов получает базу: плагин pytest из hare, фикстура `hare_test_context()` с повторно
используемыми базами PostgreSQL, `init_memory_sqlite()` для быстрого скрипта, предупреждение о смене
цикла событий между тестами и пропуск теста, который база не может выполнить. Объекты для тестов создают
[фабрики](factories.ru.md).

## <a id="pytest-plugin"></a>Плагин pytest

Достаточно `pip install hare-orm[pytest]` (pytest и pytest-asyncio): pytest сам загружает плагин hare, а
плагин ничего не делает, пока тест не возьмёт одну из его фикстур. Он читает настройки hare, названные в
настройках pytest, — иначе `HARE_ORM` из окружения или `hare_orm` из `[tool.hare]` в pyproject.toml, как
команда `hare`:

```toml
[tool.pytest.ini_options]
hare_config = "library.settings.HARE_ORM"
hare_db_url = "postgresql://postgres@localhost/test_{}"
asyncio_default_fixture_loop_scope = "session"
asyncio_default_test_loop_scope = "session"
```

Базы из настроек никогда не открываются: каждое соединение получает тестовую базу из `hare_db_url` — без
неё SQLite в памяти; `{}` в имени базы заменяется новым именем для каждого соединения и запуска. Базы
создаются с таблицами моделей один раз на сессию и удаляются в её конце (`TemporaryDatabases`). Фикстуры
и тесты выполняются в цикле событий сессии — отсюда две настройки `asyncio_default_*_loop_scope`.

| Фикстура | Что получает тест |
|---|---|
| `hare_db` | Контекст, записи которого откатываются в конце теста ([`RollbackIsolation`](isolation-and-cleanup.ru.md#rollback-isolation)). Транзакция, открытая тестом, — точка сохранения, а функции `on_commit()` не выполняются. |
| `hare_transactional_db` | Контекст, записи которого действительно фиксируются — функции `on_commit()` выполняются, — а все таблицы очищаются в конце теста ([`truncate_all_models()`](isolation-and-cleanup.ru.md#truncate-all-models)). Для кода, которому нужна настоящая фиксация. |
| `hare_database` | Контекст сессии, общий для всех тестов, — ничего не откатывается. |
| `hare_assert_query_count` | [`assert_query_count()`](assertions.ru.md#assert-num-queries): `async with hare_assert_query_count(1): ...`. |
| `hare_capture_on_commit` | `RollbackIsolation.capture_on_commit()` от `hare_db` теста. |
| `hare_rollback_isolation` | `RollbackIsolation` от `hare_db` теста. |

```python
@pytest.mark.asyncio
async def test_new_reader(hare_db, hare_assert_query_count):
    async with hare_assert_query_count(1):
        await Reader.objects.create(name="ann")


@pytest.mark.hare_requires(supports_transactions=True)
@pytest.mark.asyncio
async def test_rollback(hare_db): ...
```

Маркер `hare_requires(connection_alias=None, **conditions)` пропускает тест, соединение которого не
выполняет условия, — те же, что у [`requires_features()`](#requires-features); такой тест берёт одну из
фикстур `hare_db`, `hare_transactional_db` или `hare_database`.

| Параметр | Что задаёт |
|---|---|
| `--hare-db-url URL` | URL, из которого создаются тестовые базы, вместо `hare_db_url` из настроек. |
| `--hare-reuse-db` | Брать тестовые базы PostgreSQL из [`ReusableTestDatabases`](../dialects/postgresql/testing.ru.md#reuse-databases) вместо создания и удаления. |

Без pytest-asyncio тест, взявший фикстуру, падает с сообщением, что установить.

## <a id="hare-test-context"></a>Контекст на тест: `hare_test_context()`

```python
def hare_test_context(
    modules: list[str],
    db_url: str = "sqlite+aiosqlite://:memory:",
    app_label: str = "models",
    *,
    connection_label: str | None = None,
    use_timezone: bool = True,
    timezone: str = "UTC",
    routers: list[str | type] | None = None,
    reuse_databases: bool | None = None,
) -> AbstractAsyncContextManager[HareContext]: ...
```

Рекомендуемый способ подготовить базу для теста: каждый вызов — полностью отдельный `HareContext` (свои
подключения, свой реестр приложений, своя база), поэтому тесты, идущие параллельно (`pytest-xdist`),
никогда не мешают друг другу:

```python
import pytest_asyncio
from hare.contrib.test import hare_test_context


@pytest_asyncio.fixture
async def database():
    async with hare_test_context(["library.models"]) as context:
        yield context


async def test_create_author(database):
    author = await Author.objects.create(name="Ursula K. Le Guin")
    assert author.id is not None
```

Для SQLite в файле такая независимость требует либо своего пути на каждый вызов (фикстура `tmp_path` из
pytest и так своя у каждого рабочего процесса), либо места `{}` в пути
(`db_url="sqlite+aiosqlite:///some/dir/test-{}.sqlite"`), которое при каждом вызове заполняется случайным
UUID. SQLite выполняет запись по очереди даже между разными соединениями к одному файлу, поэтому два
рабочих процесса xdist, одновременно работающих с одним и тем же постоянным путём, получили бы
`database is locked`; поэтому при запуске с `-n` (задан `PYTEST_XDIST_WORKER`) hare добавляет к
постоянному пути SQLite идентификатор рабочего процесса. Без `-n` путь используется ровно как задан.

Как и `CREATE DATABASE` в PostgreSQL, создание базы не принимает уже существующую: существующий файл
SQLite даёт `OperationalError` раньше, чем его что-либо коснётся, поэтому `hare_test_context()` (или
`Hare.init(_create_db=True)`), направленный на настоящий файл базы, никогда не удалит его при выходе.
`:memory:` это не касается.

База удаляется при выходе, даже если создание таблиц не удалось при входе в блок: модель, команды
создания которой база отклоняет, не оставляет после себя базу.

С `reuse_databases=True` тестовая база PostgreSQL берётся из пула, а не создаётся и удаляется — см.
[PostgreSQL: тестирование](../dialects/postgresql/testing.ru.md#reuse-databases).

## <a id="init-memory-sqlite"></a>Быстрые скрипты: `init_memory_sqlite()`

```python
from hare import Hare, fields
from hare.contrib.test import init_memory_sqlite
from hare.models import Model


class Book(Model):
    id = fields.IntField(primary_key=True)
    title = fields.TextField()


@init_memory_sqlite
async def run():
    obj = await Book.objects.create(title="Moby Dick")
    assert obj.id == 1


if __name__ == "__main__":
    Hare.run_async(run())
```

Поднимает базу SQLite в памяти и создаёт таблицы перед вызовом обёрнутой функции — для разовых скриптов и
примеров, а не для наборов тестов (для них используйте `hare_test_context()`). Принимает необязательный
`models=[...]` (по умолчанию `["__main__"]`), если модели лежат не в вызывающем модуле. Его адрес
подключения — `hare.contrib.test.MEMORY_SQLITE` (`sqlite+aiosqlite://:memory:`).

## <a id="hare-loop-switch-warning"></a>Смена цикла событий между тестами: `HareLoopSwitchWarning`

Выдаётся, когда пул соединений, созданный в одном цикле событий, используется из другого: hare незаметно
открывает новое соединение для нового цикла, но об этом стоит знать. В тестовом окружении (фикстуры с
областью действия функции, `TestClient` Starlette и т. п.) это ожидаемо, и `hare_test_context()` уже сам
это предупреждение подавляет. Если вы работаете не через `hare_test_context()`, отключите его вручную:

```python
import warnings
from hare.warnings import HareLoopSwitchWarning

warnings.filterwarnings("ignore", category=HareLoopSwitchWarning)
```

Заменённое подключение закрывается в цикле событий, который его открыл, пока этот цикл ещё работает (цикл
другого потока); если тот цикл уже закрыт, его соединения разрываются — сервер закрывает их сессии. В
обоих случаях на сервере ничего не остаётся открытым.

Вне тестов это предупреждение обычно указывает на настоящую ошибку — выясните, почему цикл событий
изменился между созданием соединения и его использованием.

## <a id="requires-features"></a>Пропуск теста по возможностям базы: `requires_features()`

```python
def requires_features(
    connection_alias: str | None = None,
    **conditions: Any,
) -> Callable[[TestItem], TestItem]: ...
```

```python
@requires_features(supports_transactions=True)
async def test_rolls_back(database): ...


@requires_features(dialect="sqlite")
async def test_sqlite_sql(database): ...
```

Каждое условие называет поле `Features` подключения (`supports_transactions`, `supports_returning` и т.
п.), иначе — атрибут `SqlLiterals` его диалекта (`identifier_quote_char` и т. п.), иначе — атрибут самого
диалекта (`supports_distinct_on`, `supports_partial_indexes` и т. п.), или `dialect` — имя диалекта. См.
[Диалекты и их возможности](../dialects/dialects-and-features.ru.md). Тест, которому нужна возможность,
называет эту возможность, поэтому выполняется на каждом диалекте, где она есть; `dialect=` — для теста
собственного SQL или типов одного диалекта.

Каждое условие проверяется для подключения `connection_alias` текущего контекста — для подключения по
умолчанию при `None` (единственного, которое создаёт `hare_test_context()`; если подключений несколько и
ни одно не называется `"default"` — для первого настроенного), — и если хоть одно не совпадает, бросается
`unittest.SkipTest`. Работает как декоратор отдельной тестовой функции или целого тестового класса
(действует на каждый метод `test_*`).
