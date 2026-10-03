# Исключения (`hare.exceptions`)

```text
HareError(Exception)
├── ConfigurationError
├── FieldError
├── QueryError(HareError, ValueError)
├── NoValuesFetched
├── IncompleteInstanceError
├── ValidationError(HareError, ValueError)
├── UnSupportedError
├── ObjectLookupError                    — .model: type[Model] | None
│   ├── MultipleObjectsReturned
│   └── DoesNotExist(ObjectLookupError, LookupError)
├── TransactionManagementError
│   ├── DistributedTransactionPartiallyCommittedError — .xid, .coordinator_alias, .pending_participant_aliases
│   └── DistributedTransactionCommitAmbiguousError    — .xid, .coordinator_alias, .participant_aliases
├── DatabaseError                        — .sql, .params
│   ├── OperationalError
│   │   ├── TransactionRetryError
│   │   ├── TooManyParametersError
│   │   └── CascadeDepthLimitError        — его перехватывает сам delete(), из него до вашего кода оно не доходит
│   │       └── SqliteTriggerRecursionLimitError  — hare.dialects.sqlite.exceptions
│   ├── IntegrityError
│   │   ├── StaleObjectError              — .model, .pk, .expected_version
│   │   └── ProtectedError                — .protected_objects: list[Model]
│   └── DBConnectionError(DatabaseError, ConnectionError)
├── NonExistentTimeError(HareError, ValueError)
└── DecryptionError(HareError, ValueError)
```

Любое исключение hare — это `HareError`. Дерево разделяет два вида ошибок:

- **Неверный вызов** — `QueryError`, `FieldError`, `ConfigurationError`, `ValidationError`,
  `UnSupportedError`, `NoValuesFetched`, `IncompleteInstanceError`: их поднимает сам hare, до того
  как что-либо уйдёт в базу.
- **Отказала база** — `DatabaseError` и его подклассы: то, о чём сообщил драйвер подключения, и
  проверки целостности, которые hare выполняет вместо базы (`PROTECT`, `RESTRICT`, оптимистическая
  блокировка).

`QueryError` и `ValidationError` — ещё и `ValueError`, `DoesNotExist` — `LookupError`,
`DBConnectionError` — `ConnectionError`: обычный `except ValueError:`/`except LookupError:`,
написанный без знания о hare, их перехватывает.

Ошибка, о которой сообщила база (`IntegrityError`, `OperationalError`, `DBConnectionError` и т. п.), на
любой базе создаётся из исключения самого драйвера: оно лежит в `__cause__` (и в `args[0]`) и в
PostgreSQL содержит `sqlstate`, `constraint_name`, `table_name` и другие сведения сервера.

| Исключение | Когда возникает |
|---|---|
| `FieldError` | Ошибка, связанная с полем модели: неизвестное поле или поиск в запросе, неверная ссылка на поле. |
| `QueryError` | Вызов нельзя выполнить в таком виде: неверный аргумент (`clone()` без обязательного `pk=`), сочетание методов запроса, которое SQL не выражает, или операция с объектом в неподходящем состоянии (несохранённым, чужого арендатора). В базу ничего не отправляется. Это ещё и `ValueError`. |
| `ConfigurationError` | Неверная настройка: параметры, подключения, приложения, `Meta` модели, объявление поля или менеджер — обнаруженная при настройке hare или при первом использовании. Сюда же относится выполнение запроса до `Hare.init()`. |
| `TransactionManagementError` | Неверное использование API транзакций. |
| `DistributedTransactionPartiallyCommittedError` | Бросает `Transactions.distributed()`: решение распределённой транзакции уже надёжно зафиксировано (собственный `COMMIT` координатора удался), но один или несколько участников не успели выполнить `COMMIT PREPARED` до возврата из вызова — транзакция ЗАФИКСИРОВАНА, это не откат. Содержит `.xid`, `.coordinator_alias`, `.pending_participant_aliases`. Завершите участников через `hare distributed-recover` или повторите позже вручную. |
| `DistributedTransactionCommitAmbiguousError` | Бросает `Transactions.distributed()`: не удалась сама фиксация решения координатором, или её исход неизвестен (например, соединение оборвалось после того, как сервер выполнил `COMMIT`, но до получения подтверждения), — в отличие от `DistributedTransactionPartiallyCommittedError`, неизвестно, зафиксирована ли транзакция вообще. Содержит `.xid`, `.coordinator_alias`, `.participant_aliases`. Каждый участник всё ещё держит подготовленную транзакцию; выполните `hare distributed-recover`, чтобы правильно их завершить, а не угадывать. |
| `DatabaseError` | Основа всего, в чём отказала база; содержит `.sql`/`.params`, если они известны (см. [ниже](#sqlerrormixin)). |
| `OperationalError` | База не смогла выполнить запрос. |
| `TransactionRetryError` | База прервала команду из-за параллельной транзакции, и повтор всей транзакции может удаться: ошибка сериализации при `REPEATABLE READ`/`SERIALIZABLE`, взаимная блокировка, которую база разрешила, прервав эту сторону, или база, занятая другим пишущим (`database is locked` в SQLite). Какие это ошибки, решает драйвер подключения (`Driver.is_retryable`). Внутри `atomic()` транзакция откатывается, когда ошибка выходит из блока, — повторяйте блок целиком, а не одну его команду. |
| `IntegrityError` | Запись нарушила бы целостность данных: ограничение, о котором сообщила база, либо связь `RESTRICT`/`PROTECT`, которую hare проверил вместо базы. |
| `StaleObjectError` | `Meta.optimistic_lock_field` обнаружил одновременное изменение — `UPDATE` не затронул ни одной строки, потому что версия не совпала. Содержит `.model`, `.pk`, `.expected_version` (версия, с которой объект был прочитан). |
| `ProtectedError` | Удаление строки, которое запрещает связь с `on_delete=PROTECT`. Содержит `.protected_objects: list[Model]`. |
| `NoValuesFetched` | Чтение связи (или поля, пропущенного `.only()`/`.defer()`), которая не загружена — через `select_related()`, `prefetch_related()`, `prefetch_related_objects()` или `await`. |
| `DoesNotExist` | `get()`/`Model[pk]` не нашёл подходящей строки. Содержит `.model`. Это ещё и `LookupError`. |
| `MultipleObjectsReturned` | `get()` нашёл больше одной строки. Содержит `.model`. |
| `IncompleteInstanceError` | Сохранение частично загруженного (`.only()`/`.defer()`) объекта, у которого нет первичного ключа или сохраняемых полей, а также передача такого объекта в `bulk_create()`/`bulk_update()` без всех полей, которые записывает вызов. |
| `TooManyParametersError` | Команда передаёт больше параметров, чем за одну команду принимает драйвер или база (`SQLITE_LIMIT_VARIABLE_NUMBER` в SQLite, 32767 у `asyncpg`, 65535 по протоколу PostgreSQL у `rust_pg`), — соединение остаётся рабочим, поэтому это `OperationalError`, а не `DBConnectionError`. |
| `CascadeDepthLimitError` | Собственный `ON DELETE CASCADE` базы при настоящем удалении остановился на пределе глубины рекурсии и не дошёл до конца (клиент с `cascade_depth_limit` в `Features`; в SQLite это `SqliteTriggerRecursionLimitError`, глубже `SQLITE_LIMIT_TRIGGER_DEPTH`). `delete()` выполняет такое удаление в транзакции, перехватывает эту ошибку, откатывает попытку и проходит каскад в Python; до вашего кода она доходит только из «сырого» DELETE через `execute()`. |
| `DBConnectionError` | Ошибка на уровне соединения; это ещё и `ConnectionError`, поэтому её ловит и простой `except ConnectionError:`. Команда, которую драйвер отказывается отправлять (слишком много параметров, сообщение, которое он не может закодировать), — это `OperationalError`: соединение в порядке. |
| `ValidationError` | Не прошла проверка значения поля, или значение нельзя привести к типу поля. Это ещё и `ValueError`. |
| `NonExistentTimeError` | Дата-время без пояса попадает в пропуск при переходе на летнее время — время на часах, которое пояс пропускает и которому не соответствует ни один момент (`Timezone.make_aware()`). Это ещё и `ValueError`. |
| `DecryptionError` | Значение, прочитанное из `EncryptedTextField`/`EncryptedJSONField`, не удаётся расшифровать: оно записано другим ключом, чем передан в `configure_field_encryption()`, или в колонке не зашифрованный токен. Это ещё и `ValueError`. |
| `UnSupportedError` | База, диалект, драйвер или версия сервера подключения не умеют того, что попросили: оператор фильтра, `select_for_update()`, `stream()`, `distinct(*fields)`, `bulk_create(returning=/use_copy=)`, транзакцию или уровень изоляции, двухфазную фиксацию, тип поля, ограничение, параметр индекса или вид триггера, `.explain(output_format=...)` в SQLite (простой `.explain()` там тоже работает, через `EXPLAIN QUERY PLAN`), или сервер старше того, с которым работает диалект. Бросается до отправки чего-либо. Ошибка в самом объявлении, на любой базе, — это `ConfigurationError`. |

## Ошибки миграций {: #migration-errors }

Миграции бросают собственные подклассы `HareError` из `hare.migrations.exceptions`:

```text
HareError
└── HareMigrationError
    ├── UnknownMigrationError(HareMigrationError, LookupError)
    ├── MigrationLoadError
    ├── CircularDependencyError
    ├── IncompatibleStateError
    ├── IrreversibleMigrationError
    ├── PartiallyAppliedMigrationError
    ├── FieldNarrowingDataLossError
    ├── ForeignKeyTargetChangeError
    └── InconsistentMigrationStateError(HareMigrationError, RuntimeError)
```

| Исключение | Когда |
|---|---|
| `UnknownMigrationError` | Метка приложения, имя миграции или цель `migrate` не называет ничего существующего — или имени миграции соответствует несколько миграций. Это ещё и `LookupError`. |
| `MigrationLoadError` | Файл миграции не загружается: не импортируется, в нём нет класса `Migration`, миграция определена дважды или зависимость называет несуществующую миграцию или приложение. |
| `CircularDependencyError` | Зависимости миграций образуют цикл. |
| `IncompatibleStateError` | Операцию нельзя выполнить на состоянии, которое оставляют предыдущие миграции. |
| `IrreversibleMigrationError` | Миграцию откатывают, а одну из её операций обратить нельзя. |
| `PartiallyAppliedMigrationError` | Неатомарная миграция упала посередине — часть её операций уже выполнилась. |
| `FieldNarrowingDataLossError` | `AlterField`, сужающий колонку, обрезал бы хранящиеся значения. |
| `ForeignKeyTargetChangeError` | `AlterField` переводит связь на другую целевую колонку (`to_field`), а строки хранят значения ключа по прежней. |
| `InconsistentMigrationStateError` | После прогона файлов миграций связь указывает на модель, которой больше нет. |

Операция миграции или значение, которое нельзя объявить или записать в файл миграции (`lambda` в
`default`, `RemoveIndex()` без имени и полей), дают `ConfigurationError`; противоречащие друг другу
цели `migrate` дают `QueryError`.

## SQL ошибки базы данных {: #sqlerrormixin }

Каждое `DatabaseError` — `OperationalError`, `IntegrityError`, `DBConnectionError` и их подклассы —
содержит запрос, который не выполнился:

```python
class DatabaseError(HareError):
    def __init__(self, *args, sql: str | None = None, params: list | None = None) -> None
    sql: str | None
    params: list | None
```

Если задан `sql`, `str(exc)` его включает — удобно для журналов и Sentry, не нужно доставать его из
исключения вручную. Параметры запроса в `str(exc)` никогда не попадают (как и в событие исключения,
которое записывает трассировка OpenTelemetry): в них могут быть пароли, токены или персональные данные.
Они остаются в `exc.params` для отладки — пишите их в журнал только туда, куда этим данным можно:

```python
try:
    await Model.objects.raw(bad_sql)
except OperationalError as exc:
    logger.error("query failed", extra={"sql": exc.sql})
    logger.debug("query params", extra={"params": exc.params})
```

Сообщение, которое дают драйвер или сам сервер, всё равно может назвать значение, например у
`asyncpg`: `invalid input for query argument $1: 'value'`. hare преобразует значение через его поле
везде, где поле известно, — обычное значение фильтра или `update()`, `Value(...)`, сравниваемое с
колонкой или записываемое в неё, текст в арифметике с числовой колонкой (`F("pin") + "5"`), — поэтому
неверное значение сначала даёт `ValidationError` поля (и скрывает значение поля с `sensitive=True`).
Константа, которую база встречает без поля (`annotate(x=Value(...))`, сравниваемое позже, параметр
готового SQL, аргумент функции), доходит до драйвера как есть, и текст его ошибки может её показать.

## Как обрабатывать {: #handling-patterns }

```python
try:
    await link.delete()
except ProtectedError as exc:
    raise ValidationError(f"На эту строку ещё ссылается объектов: {len(exc.protected_objects)}")
```

```python
try:
    await report.save()
except StaleObjectError as exc:
    # кто-то изменил строку раньше — перечитайте и повторите или покажите пользователю конфликт
    logger.warning("stale write on %s pk=%s, expected version %s", exc.model.__name__, exc.pk, exc.expected_version)
    ...
```

```python
widget = await Widget.objects.get_or_none(pk=widget_id)
if widget is None:
    raise NotFound()
```
