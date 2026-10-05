# Миграции без файлов

Приложение, которое меняет схему таблицы во время работы (например, тип контента, настроенный в
панели администратора), собирает миграции прямо в памяти, хранит их где угодно (скажем, текстом в
своей базе) и применяет само. Всё ниже импортируется из `hare.migrations`: `Migration`, `State`,
`MigrationRunner`, `MigrationRecorder`, `MigrationWriter`, `OperationEffect`, `OperationPlan`.

```python
Migration(name, app_label, *, operations=None, dependencies=None)
Migration.from_source(source, *, name, app_label) -> Migration

State.from_models(models, *, app_label=None, default_connections=None) -> State
State.get_operations(new_state, app_label) -> OperationPlan
Operation.get_effect(app_label, state, dialect) -> OperationEffect

MigrationRunner(connection, *, recorder=None, lock_timeout=None)
await runner.ensure_journal()
await runner.apply(migration, state, *, dry_run=False) -> State
await runner.unapply(migration, state, *, dry_run=False) -> State
await runner.record(migration, *, applied, connection=None)
await runner.collect_sql(migration, state, *, backward=False) -> list[str]
runner.get_effects(migration, state) -> list[OperationEffect]
```

- `Migration(...)` собирает миграцию в коде; `operations` и `dependencies` заменяют атрибуты класса,
  которые объявляет файл миграции.
- `Migration.from_source()` читает миграцию из текста файла миграции — того, что выдаёт
  `MigrationWriter(name, app_label, operations).as_string()`. Текст выполняется как код Python,
  поэтому он должен приходить из надёжного места. Текст, который не выполняется или не объявляет
  класс `Migration`, даёт `hare.migrations.exceptions.MigrationLoadError`.
- `State.from_models()` — состояние схемы по классам моделей, как они объявлены. Приложение модели —
  её `Meta.app`, иначе `app_label`; модель без того и другого даёт `ConfigurationError`.
- `old_state.get_operations(new_state, app_label)` возвращает `OperationPlan(operations, warnings,
  data_loss_warnings)` — операции, которые `makemigrations` записал бы для приложения, с его
  предупреждениями.
- `Operation.get_effect()` возвращает `OperationEffect(operation, reversible, rewrites_table,
  loses_data, reason)`: можно ли откатить операцию, переписывает ли база всю таблицу (время растёт
  с числом строк), могут ли пропасть значения или строки, и почему. `RemoveField` и `DeleteModel`
  теряют данные. `AlterField` теряет данные, когда меняет тип колонки, и переписывает таблицу,
  когда диалект делает так для этого изменения, — см.
  [`alter_field_rewrites_table`](zero-downtime.ru.md#alter-field-rewrites-table).
- `MigrationRunner` применяет одну миграцию на `connection` так же, как `hare migrate`: атомарную —
  в одной транзакции вместе с записью в журнале, с проверкой ограничений перед фиксацией.
  `lock_timeout` — [ограничение ожидания блокировок](migrations.ru.md#lock-timeout) для её команд.
    - `ensure_journal()` создаёт таблицу журнала, если её ещё нет.
    - `apply()` возвращает состояние после миграции; `unapply()` принимает состояние до её
      применения и возвращает его.
    - `record()` отмечает миграцию в журнале применённой или откаченной. Без `recorder` ничего не
      делает — как и записи в журнал внутри `apply()`/`unapply()`.
    - `collect_sql()` возвращает SQL миграции (или её отката, при `backward=True`), не выполняя
      его; у атомарной миграции — между `BEGIN;` и `COMMIT;`.
    - `get_effects()` возвращает `OperationEffect` каждой операции по порядку.
- `MigrationRecorder(connection, *, table_name="hare_migrations")` — журнал применённых миграций.
  Дайте миграциям, собранным во время работы, собственную таблицу, чтобы `hare migrate` их не видел.

Модель, зарегистрированная во время работы (`Hare.register_live_models()`), и её первая миграция,
собранная из разницы двух состояний:

```python
from hare import Connections, Hare
from hare.migrations import Migration, MigrationRecorder, MigrationRunner, MigrationWriter, State

connection = Connections.get("default")
runner = MigrationRunner(connection, recorder=MigrationRecorder(connection, table_name="content_migrations"))
await runner.ensure_journal()

old_state = State.from_models([Tournament])
plan = old_state.get_operations(State.from_models([Tournament, News]), "content")
migration = Migration("0001_news", "content", operations=plan.operations)

print("\n".join(await runner.collect_sql(migration, old_state)))  # SQL, пока ничего не выполнено
source = MigrationWriter(migration.name, "content", migration.operations).as_string()  # сохранить
new_state = await runner.apply(migration, old_state)
Hare.register_live_models([News], app_label="content", managed=False)
```

`managed=False` не подпускает к таблице `makemigrations`, `migrate` и проверку расхождений: её
миграции есть только там, где их хранит приложение, и `hare migrate` о них не знает. Текущее
состояние такой таблицы получается из её собственных миграций, применённых по порядку, а не из
зарегистрированного класса.
