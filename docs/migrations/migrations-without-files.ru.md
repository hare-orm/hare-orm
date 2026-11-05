# Миграции без файлов

Приложение, которое меняет схему таблицы во время работы (например, таблицу, колонки которой настраивают
его пользователи), собирает миграции прямо в памяти, хранит их где угодно (скажем, текстом в своей базе) и
применяет само. Всё ниже импортируется из `hare.migrations`: `Migration`, `State`, `MigrationRunner`,
`MigrationRecorder`, `MigrationWriter`, `OperationEffect`, `OperationPlan`.

При создании `State` фиксирует обработчик соединений приложения, настройки `swappable` и объявления
unmanaged-моделей. Отложенный рендеринг и `clone()` сохраняют это `MigrationEnvironment`, даже если
текущим стал другой `HareContext`. Копии состояния используют общие соединения, не владеют ими и не
закрывают их; объявления и отрисованные классы моделей остаются независимыми. Для явного окружения
импортируйте `MigrationEnvironment` из `hare.migrations.state.migration_environment` и передайте
`environment=` в `State` или `State.from_models()`. `MigrationEnvironment()` создаёт пустое окружение
для работы без базы, а `MigrationEnvironment.capture()` фиксирует текущее приложение.

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

    @staticmethod
    def from_source(source: str, *, name: str, app_label: str) -> Migration: ...

class State:
    @classmethod
    def from_models(
        cls,
        models: Iterable[type[Model]],
        *,
        app_label: str | None = None,
        default_connections: dict[str, str] | None = None,
        environment: MigrationEnvironment | None = None,
    ) -> State: ...

    def get_operations(
        self, new_state: State, app_label: str, rename_questions: RenameQuestions | None = None
    ) -> OperationPlan: ...

class Operation(ABC):
    def get_effect(self, app_label: str, state: State, schema_editor: BaseSchemaEditor) -> OperationEffect: ...

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

    async def unapply(self, migration: Migration, state_before: State, *, dry_run: bool = False) -> State: ...

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
  Обращение к `state.apps` публикует граф исторических моделей только после успешного связывания.
  Если добавленная или изменённая модель не связывается, прежние классы и отметки изменений сохраняются;
  после исправления объявления следующее обращение повторит те же изменения.
- `old_state.get_operations(new_state, app_label)` возвращает
  `OperationPlan(operations, warnings, data_loss_warnings)` — операции, которые `makemigrations` записал
  бы для приложения, с его предупреждениями. Однозначные совпадения полей по колонке или сигнатуре
  связи распознаются автоматически. Остальные кандидаты требуют `rename_questions`
  (`RenameQuestions({"content.Book.title": "name"})` — см. [Переименования](migrations.ru.md#renames));
  неподтверждённый кандидат удаляется и добавляется.
- `Operation.get_effect()` возвращает
  `OperationEffect(operation, reversible, rewrites_table, loses_data, reason)`: можно ли откатить
  операцию, переписывает ли база всю таблицу (время растёт с числом строк), могут ли пропасть значения
  или строки, и почему. `RemoveField` и `DeleteModel` теряют данные. Для `AlterField` флаг `loses_data`
  консервативно отмечает любое изменение объявленного типа колонки, в том числе расширение; он не
  доказывает потерю существующих значений. Передайте настроенный `BaseSchemaEditor`: метод готовит
  тот же план, что и исполнение, без обращений к базе и вычисления runtime-defaults. Эффект перезаписи
  включает зависимые таблицы и преобразования хранимых значений — см.
  [`alter_field_rewrites_table`](zero-downtime.ru.md#alter-field-rewrites-table).
- `MigrationRunner` применяет одну миграцию на `connection` так же, как `hare migrate`: атомарную — в
  одной транзакции вместе с записью в журнале, с проверкой ограничений перед фиксацией. `lock_timeout` —
  [ограничение ожидания блокировок](migrations.ru.md#lock-timeout) для её команд.
  Исполнитель сохраняет обработчик подключений переданного клиента; для самостоятельного клиента
  без обработчика фиксирует текущий. Активная транзакция используется только для того же исходного
  клиента. Независимый клиент или клиент схемы не заменяется другим клиентом с таким же alias.
    - `ensure_journal()` создаёт таблицу журнала, если её ещё нет.
    - `apply()` возвращает состояние после миграции; `unapply()` принимает состояние до её применения и
      возвращает его новую копию. Оба сохраняют переданное состояние неизменным, в том числе при ошибке.
      С `dry_run=True` возвращают результат пробного применения без исполнения операций миграции
      и изменения журнала. Подключение или транзакция при этом могут открываться.
      Тот же вход можно затем передать настоящему запуску. Для отката передавайте сохранённое
      состояние до применения явно: `unapply(migration, state_before=before)`.
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
