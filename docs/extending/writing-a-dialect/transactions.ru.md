# Создание диалекта: транзакции

Шаг 7 [создания диалекта](index.ru.md): транзакции, точки сохранения и конкурентный доступ.

## <a id="transactions"></a>7. Транзакции и одновременная работа

`Features.supports_transactions` решает, есть ли транзакции вообще (см.
[Features](driver-and-client.ru.md#features)).

Сама транзакция — один автомат состояний, написанный один раз в `TransactionClient`
(`hare.dialects.base.client`): `begin()`, `commit()`, `rollback()`, `savepoint()`, `release_savepoint()`
и `savepoint_rollback()`, вложенность, обработчики `on_commit()`/`on_rollback()`, защита `COMMIT` от
отмены задачи, отказ выполнять команду после окончания транзакции и сообщения `TransactionEvent`. Клиент
транзакции драйвера даёт только примитивы, которые автомат вызывает:

| Примитив | Что делает |
|---|---|
| `_driver_begin()`, `_driver_commit()`, `_driver_rollback()` | Начинает транзакцию на соединении; отправляет её `COMMIT`; отправляет её `ROLLBACK`. |
| `_driver_savepoint(name)`, `_driver_release_savepoint(name)`, `_driver_rollback_to_savepoint(name)` | Открывает точку сохранения, освобождает её и откатывается к ней. |
| `_get_new_savepoint_name()` | Имя точки сохранения, ещё не использованное на этом соединении. |
| `_is_connection_lost(error)` | Означает ли ошибка драйвера при `COMMIT`/`ROLLBACK`, что соединение пропало, — такую транзакцию сервер откатывает. По умолчанию `False`. |
| `_is_commit_outcome_unknown(error)` | Остаётся ли при потере соединения неизвестным, прошёл ли выполнявшийся `COMMIT`. |
| `_is_commit_rejection(error)` | Ответила ли база на `COMMIT` ошибкой — транзакция закончена. |
| `_is_transaction_finished_error(error)` | Сообщает ли драйвер, что более ранний, прерванный `COMMIT`/`ROLLBACK` уже закончил транзакцию. |
| `_check_commit_allowed()`, `_check_savepoint_allowed()`, `_before_top_level_end(event)`, `_after_rejected_commit(error)` | Необязательные точки расширения вокруг шагов автомата: SQLite отказывается фиксировать прерванную транзакцию и возвращает транзакцию «только чтение» в обычный режим перед её окончанием. |
| `_take_transaction_resources()`, `_give_back_transaction_resources()` | Забирают то, что транзакция верхнего уровня держит всю свою жизнь, до `BEGIN` — соединение из пула, блокировку соединения — и возвращают сразу, как только `COMMIT`/`ROLLBACK` выполнен, до обратных вызовов. По умолчанию ничего не делают. |
| `_undo_failed_begin()` | Завершает транзакцию, чей `BEGIN` упал, но мог успеть выполниться. По умолчанию ничего не делает. |
| `_end_unfinished_transaction()` | Завершает то, что транзакция верхнего уровня оставила открытым у драйвера, когда её блок закончился. По умолчанию ничего не делает. |

Ставьте `begins_without_suspending = True` только если `_driver_begin()` подготавливает отложенное
состояние, ни разу не приостанавливаясь. Такая подготовка выполняется напрямую; сам отложенный `BEGIN`
по-прежнему должен идти через защищённый путь `PendingStatements`. По умолчанию `False`: тогда
защищён от отмены сам `_driver_begin()`.

Шесть методов `_driver_*` и `_get_new_savepoint_name()` абстрактные; у остальных есть реализация по
умолчанию. Вложенная транзакция — клиент того же класса на том же соединении; его создаёт базовый класс.

Клиент, который сам применяет `lock_timeout` транзакции, — таймаут занятости SQLite — ставит
`enforces_lock_timeout_itself = True`, и база без команды для него тогда принимает эту опцию.

`TransactionStatements` диалекта (`build_transactions()`) настраивают транзакцию:

| Член класса | Что задаёт |
|---|---|
| `get_isolation_level(requested)` | Уровень, на котором выполняется транзакция, попросившая уровень, — самый слабый из `Features.isolation_levels`, который не слабее запрошенного. |
| `get_isolation_level_sql(level)` | Команду, задающую его; `None`, если каждая транзакция и так выполняется на этом уровне. |
| `get_read_only_sql()` | Команду, делающую транзакцию «только чтение» (по умолчанию `SET TRANSACTION READ ONLY` по стандарту). |
| `get_begin_sql()`, `get_commit_sql()`, `get_rollback_sql()` | Команды, которые открывают, фиксируют и откатывают транзакцию (по умолчанию `START TRANSACTION`, `COMMIT`, `ROLLBACK` по стандарту), — ими обрамляется SQL, который `sqlmigrate` печатает для атомарной миграции, и они отправляются там, где hare завершает транзакцию в обход своих обработчиков. |
| `get_statement_timeout_sql(milliseconds)`, `get_lock_timeout_sql(milliseconds)` | Ограничения времени команды и ожидания блокировки на время транзакции; `None`, если у базы их нет. |

Часть `TableLocks` редактора схемы (`table_locks_class`): `get_lock_table_sql()` блокирует таблицу на
время транзакции, а `get_migration_lock_sql()` выстраивает одновременные запуски `migrate` по очереди;
`build_two_phase_commit()` даёт `Transactions.distributed()` его команды.
