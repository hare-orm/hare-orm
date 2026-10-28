# Миграции без файлов

Приложение, которое меняет схему таблицы во время работы (например, таблицу, колонки которой настраивают
его пользователи), собирает миграции прямо в памяти, хранит их где угодно (скажем, текстом в своей базе) и
применяет само. Всё ниже импортируется из `hare.migrations`: `Migration`, `State`, `MigrationRunner`,
`MigrationRecorder`, `MigrationWriter`, `OperationEffect`, `OperationPlan`.

```python
class Migration:
    def __init__(
        self,
        name: str,
        app_label: str,
        *,
        operations: Iterable[Operation] | None = None,
        dependencies: Iterable[tuple[str, str]] | None = None,
    ) -> None: ...

    @classmethod
    def from_source(cls, source: str, *, name: str, app_label: str) -> Migration: ...

class State:
    @classmethod
    def from_models(
        cls,
        models: Iterable[type[Model]],
        *,
        app_label: str | None = None,
        default_connections: dict[str, str] | None = None,
    ) -> State: ...

    def get_operations(
        self, new_state: State, app_label: str, rename_questions: RenameQuestions | None = None
    ) -> OperationPlan: ...

class Operation(ABC):
    def get_effect(self, app_label: str, state: State, dialect: Dialect) -> OperationEffect: ...

class MigrationRunner:
    def __init__(
        self,
        connection: DatabaseClient,
        *,
        recorder: MigrationRecorder | None = None,
        lock_timeout: float | None = None,
    ) -> None: ...

    async def ensure_journal(self) -> None: ...

    async def apply(self, migration: Migration, state: State, *, dry_run: bool = False) -> State: ...

    async def unapply(self, migration: Migration, state: State, *, dry_run: bool = False) -> State: ...

    async def record(
        self,
        migration: Migration,
        *,
        applied: bool,
        connection: DatabaseClient | None = None,
    ) -> None: ...

    async def collect_sql(
        self,
        migration: Migration,
        state: State,
        *,
        backward: bool = False,
    ) -> list[str]: ...

    def get_effects(self, migration: Migration, state: State) -> list[OperationEffect]: ...

class MigrationRecorder:
    def __init__(self, connection: DatabaseClient, *, table_name: str = "hare_migrations") -> None: ...
```

- `Migration(...)` собирает миграцию в коде; `operations` и `dependencies` заменяют атрибуты класса,
  которые объявляет файл миграции.
- `Migration.from_source()` читает миграцию из текста файла миграции — того, что выдаёт
  `MigrationWriter(name, app_label, operations).as_string()`. Текст выполняется как код Python, поэтому
  он должен приходить из надёжного места. Текст, который не выполняется или не объявляет класс
  `Migration`, даёт `hare.migrations.exceptions.MigrationLoadError`.
- `State.from_models()` — состояние схемы по классам моделей, как они объявлены. Приложение модели — её
  `Meta.app`, иначе `app_label`; модель без того и другого даёт `ConfigurationError`.
- `old_state.get_operations(new_state, app_label)` возвращает
  `OperationPlan(operations, warnings, data_loss_warnings)` — операции, которые `makemigrations` записал
  бы для приложения, с его предупреждениями. Переименование делается, только когда его подтверждают
  `rename_questions` (`RenameQuestions({"content.Book.title": "name"})` — см.
  [Переименования](migrations.ru.md#renames)); без них переименованное поле или модель удаляются и
  добавляются.
- `Operation.get_effect()` возвращает
  `OperationEffect(operation, reversible, rewrites_table, loses_data, reason)`: можно ли откатить
  операцию, переписывает ли база всю таблицу (время растёт с числом строк), могут ли пропасть значения
  или строки, и почему. `RemoveField` и `DeleteModel` теряют данные. `AlterField` теряет данные, когда
  меняет тип колонки, и переписывает таблицу, когда диалект делает так для этого изменения, — см.
  [`alter_field_rewrites_table`](zero-downtime.ru.md#alter-field-rewrites-table).
- `MigrationRunner` применяет одну миграцию на `connection` так же, как `hare migrate`: атомарную — в
  одной транзакции вместе с записью в журнале, с проверкой ограничений перед фиксацией. `lock_timeout` —
  [ограничение ожидания блокировок](migrations.ru.md#lock-timeout) для её команд.
    - `ensure_journal()` создаёт таблицу журнала, если её ещё нет.
    - `apply()` возвращает состояние после миграции; `unapply()` принимает состояние до её применения и
      возвращает его. С `dry_run=True` оба выполняют операции, не трогая ни базу, ни журнал.
    - `record()` отмечает миграцию в журнале применённой или откаченной. Без `recorder` ничего не делает
      — как и записи в журнал внутри `apply()`/`unapply()`.
    - `collect_sql()` возвращает SQL миграции (или её отката, при `backward=True`), не выполняя его; у
      атомарной миграции на базе, которая откатывает DDL (не ClickHouse), — между `BEGIN;` и `COMMIT;`.
    - `get_effects()` возвращает `OperationEffect` каждой операции по порядку.
- `MigrationRecorder` — журнал применённых миграций в таблице `table_name`. Дайте миграциям, собранным во
  время работы, собственную таблицу, чтобы `hare migrate` их не видел.

Модель, зарегистрированная во время работы (`Hare.register_live_models()`), и её первая миграция,
собранная из разницы двух состояний:

```python
from hare import Connections, Hare
from hare.migrations import Migration, MigrationRecorder, MigrationRunner, MigrationWriter, State

connection = Connections.get("default")
runner = MigrationRunner(connection, recorder=MigrationRecorder(connection, table_name="content_migrations"))
await runner.ensure_journal()

old_state = State.from_models([Book])
plan = old_state.get_operations(State.from_models([Book, Notice]), "content")
migration = Migration("0001_notice", "content", operations=plan.operations)

print("\n".join(await runner.collect_sql(migration, old_state)))  # SQL, пока ничего не выполнено
source = MigrationWriter(migration.name, "content", migration.operations).as_string()  # сохранить
new_state = await runner.apply(migration, old_state)
Hare.register_live_models([Notice], app_label="content", managed=False)
```

`managed=False` не подпускает к таблице `makemigrations`, `migrate` и проверку расхождений: её миграции
есть только там, где их хранит приложение, и `hare migrate` о них не знает. Текущее состояние такой
таблицы получается из её собственных миграций, применённых по порядку, а не из зарегистрированного
класса. Журнал этих миграций — таблица, за которой нет модели: проверке расхождений о нём сообщают, иначе
она показывает его как неотслеживаемую таблицу —
`await detect_drift_for_alias(Hare.apps, apps_config, "default", journal_tables=["content_migrations"])`,
`hare drift --journal-table content_migrations`.
