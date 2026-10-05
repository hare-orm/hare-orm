# Исключения

Все исключения hare: где каждое стоит в дереве, когда возникает, какой SQL несёт ошибка базы данных и как
обрабатывать частые случаи.

```text
HareError(Exception)
├── ConfigurationError
├── FieldError
├── QueryError(HareError, ValueError)
├── NoValuesFetched(HareError, AttributeError)
├── IncompleteInstanceError
├── ValidationError(HareError, ValueError)
├── UnSupportedError
├── ObjectLookupError                    — .model: type[Model] | None
│   ├── MultipleObjectsReturned
│   └── DoesNotExist(ObjectLookupError, LookupError)
├── TransactionManagementError
│   ├── DistributedTransactionPartiallyCommittedError — .xid, .coordinator_alias, .pending_participant_aliases
│   └── DistributedTransactionCommitAmbiguousError    — .xid, .coordinator_alias, .participant_aliases
├── DatabaseError                        — .sql, .parameters
│   ├── OperationalError
│   │   ├── TransactionRetryError
│   │   ├── TooManyParametersError
│   │   └── CascadeDepthLimitError        — его перехватывает сам delete(), из него до вашего кода оно не доходит
│   │       └── SqliteTriggerRecursionLimitError  — hare.dialects.sqlite.exceptions
│   ├── IntegrityError
│   │   ├── StaleObjectError              — .model, .pk, .expected_version
│   │   └── ProtectedError                — .protected_objects: list[Model]
│   └── DBConnectionError(DatabaseError, ConnectionError)
│       └── PoolTimeoutError
├── NonExistentTimeError(HareError, ValueError)
└── DecryptionError(HareError, ValueError)
```

Любое исключение ядра hare — это `HareError` ([другие пакеты](#package-exceptions) добавляют подклассы и
несколько собственных исключений). Дерево разделяет два вида ошибок:

- **Неверный вызов** — `QueryError`, `FieldError`, `ConfigurationError`, `ValidationError`,
  `UnSupportedError`, `NoValuesFetched`, `IncompleteInstanceError`: их поднимает сам hare, до того как
  что-либо уйдёт в базу.
- **Отказала база** — `DatabaseError` и его подклассы: то, о чём сообщил драйвер подключения, и проверки
  целостности, которые hare выполняет вместо базы (`PROTECT`, `RESTRICT`, оптимистическая блокировка).

`QueryError` и `ValidationError` — ещё и `ValueError`, `DoesNotExist` — `LookupError`, `NoValuesFetched`
— `AttributeError`, `DBConnectionError` — `ConnectionError`: обычный
`except ValueError:`/`except LookupError:`, написанный без знания о hare, их перехватывает.

Ошибка, о которой сообщила база (`IntegrityError`, `OperationalError`, `DBConnectionError` и т. п.), на
любой базе создаётся из исключения самого драйвера: оно лежит в `__cause__` (и в `args[0]`) и в
PostgreSQL содержит `sqlstate`, `constraint_name`, `table_name` и другие сведения сервера.

| Исключение | Когда возникает |
|---|---|
| `FieldError` | Ошибка, связанная с полем модели: неизвестное поле или поиск в запросе, неверная ссылка на поле. |
| `QueryError` | Вызов нельзя выполнить в таком виде: неверный аргумент (`clone()` без обязательного `pk=`), сочетание методов запроса, которое SQL не выражает, или операция с объектом в неподходящем состоянии (несохранённым, чужого арендатора). В базу ничего не отправляется. Это ещё и `ValueError`. |
| `ConfigurationError` | Неверная настройка: параметры, подключения, приложения, `Meta` модели, объявление поля или менеджер — обнаруженная при настройке hare или при первом использовании. Сюда же относится выполнение запроса до `Hare.init()`. |
| `TransactionManagementError` | Неверное использование API транзакций. |
| `DistributedTransactionPartiallyCommittedError` | Распределённая транзакция зафиксирована, но участник не завершил `COMMIT PREPARED` — см. [`DistributedTransactionPartiallyCommittedError`](#distributedtransactionpartiallycommittederror) |
| `DistributedTransactionCommitAmbiguousError` | Фиксация самого координатора не удалась, или её исход неизвестен — см. [`DistributedTransactionCommitAmbiguousError`](#distributedtransactioncommitambiguouserror) |
| `DatabaseError` | Основа всего, в чём отказала база; содержит `.sql`/`.parameters`, если они известны (см. [ниже](#sqlerrormixin)). |
| `OperationalError` | База не смогла выполнить запрос. |
| `TransactionRetryError` | Параллельная транзакция прервала команду, и повтор транзакции может удаться — см. [`TransactionRetryError`](#transactionretryerror) |
| `IntegrityError` | Запись нарушила бы целостность данных: ограничение, о котором сообщила база, либо связь `RESTRICT`/`PROTECT`, которую hare проверил вместо базы. |
| `StaleObjectError` | `Meta.optimistic_lock_field` обнаружил одновременное изменение — `UPDATE` или `DELETE` строки объекта не затронул ни одной строки, потому что версия не совпала — см. [`StaleObjectError`](#staleobjecterror) |
| `ProtectedError` | Удаление строки, которое запрещает связь с `on_delete=PROTECT`. Содержит `.protected_objects: list[Model]`. |
| `NoValuesFetched` | Чтение связи (или поля, пропущенного `.only()`/`.defer()`), которая не загружена — через `select_related()`, `prefetch_related()`, `prefetch_related_objects()` или `await`; или значения, которое вычисляет база (`GeneratedField`) и которое ещё не прочитано, — у нового экземпляра на базе, не возвращающей строк записи (ClickHouse), у ещё не сохранённой копии `clone()`. Это ещё и `AttributeError`. |
| `DoesNotExist` | `get()`/`Model[pk]` не нашёл подходящей строки. Содержит `.model`. Это ещё и `LookupError`. |
| `MultipleObjectsReturned` | `get()` нашёл больше одной строки. Содержит `.model`. |
| `IncompleteInstanceError` | Сохранение частично загруженного (`.only()`/`.defer()`) объекта, у которого нет первичного ключа или сохраняемых полей, а также передача такого объекта в `bulk_create()`/`bulk_update()` без всех полей, которые записывает вызов. |
| `TooManyParametersError` | Команда передаёт больше параметров, чем за одну команду принимает драйвер или база (`SQLITE_LIMIT_VARIABLE_NUMBER` в SQLite, 32767 у `asyncpg`, 65535 по протоколу PostgreSQL у `rust_pg`), — соединение остаётся рабочим, поэтому это `OperationalError`, а не `DBConnectionError`. |
| `CascadeDepthLimitError` | Собственный `ON DELETE CASCADE` базы при настоящем удалении остановился на пределе глубины рекурсии и не дошёл до конца (клиент с `cascade_depth_limit` в `Features` — см. [`CascadeDepthLimitError`](#cascadedepthlimiterror) |
| `DBConnectionError` | Ошибка на уровне соединения; это ещё и `ConnectionError`, поэтому её ловит и простой `except ConnectionError:`. Команда, которую драйвер отказывается отправлять (слишком много параметров, сообщение, которое он не может закодировать), — это `OperationalError`: соединение в порядке. |
| `PoolTimeoutError` | Ожидание соединения пула исчерпало `pool_acquire_timeout` соединения — все соединения оставались заняты. Это `DBConnectionError`; пул занят, а не сломан, и следующий вызов может получить соединение ([Метрики пула и здоровье](../observability/pool-health.ru.md#events)). |
| `ValidationError` | Не прошла проверка значения поля, или значение нельзя привести к типу поля. Текст с одиночным суррогатом (половиной суррогатной пары, `"\ud800"`) среди значений команды тоже даёт её, на любом драйвере: в UTF-8 у него нет кодировки. Это ещё и `ValueError`. |
| `NonExistentTimeError` | Дата-время без пояса попадает в пропуск при переходе на летнее время — время на часах, которое пояс пропускает и которому не соответствует ни один момент (`Timezone.make_aware()`). Это ещё и `ValueError`. |
| `DecryptionError` | Значение, прочитанное из `EncryptedTextField`/`EncryptedJSONField`, не удаётся расшифровать: оно записано другим ключом, чем передан в `FieldEncryption.configure()`, или в колонке не зашифрованный токен. Это ещё и `ValueError`. |
| `UnSupportedError` | База, диалект, драйвер или версия сервера не умеют того, что попросили — см. [`UnSupportedError`](#unsupportederror) |

## <a id="distributedtransactionpartiallycommittederror"></a>`DistributedTransactionPartiallyCommittedError`

Бросает `Transactions.distributed()`: решение распределённой транзакции уже надёжно зафиксировано
(собственный `COMMIT` координатора удался), но один или несколько участников не успели выполнить
`COMMIT PREPARED` до возврата из вызова — транзакция ЗАФИКСИРОВАНА, это не откат. Содержит `.xid`,
`.coordinator_alias`, `.pending_participant_aliases`. Завершите участников через
`hare distributed-recover` или повторите позже вручную.

## <a id="distributedtransactioncommitambiguouserror"></a>`DistributedTransactionCommitAmbiguousError`

Бросает `Transactions.distributed()`: не удалась сама фиксация решения координатором, или её исход
неизвестен (например, соединение оборвалось после того, как сервер выполнил `COMMIT`, но до получения
подтверждения), — в отличие от `DistributedTransactionPartiallyCommittedError`, неизвестно, зафиксирована
ли транзакция вообще. Содержит `.xid`, `.coordinator_alias`, `.participant_aliases`. Каждый участник всё
ещё держит подготовленную транзакцию; выполните `hare distributed-recover`, чтобы правильно их завершить,
а не угадывать.

## <a id="transactionretryerror"></a>`TransactionRetryError`

База прервала команду из-за параллельной транзакции, и повтор всей транзакции может удаться: ошибка
сериализации при `REPEATABLE READ`/`SERIALIZABLE`, взаимная блокировка, которую база разрешила, прервав
эту сторону, или база, занятая другим пишущим (`database is locked` в SQLite), или транзакция ClickHouse,
наткнувшаяся на часть таблицы, которую держит другая (ошибка сериализации). Какие это ошибки, решает
драйвер подключения (`Driver.is_retryable`). Внутри `atomic()` транзакция откатывается, когда ошибка
выходит из блока, — повторяйте блок целиком, а не одну его команду.

## <a id="staleobjecterror"></a>`StaleObjectError`

`Meta.optimistic_lock_field` обнаружил одновременное изменение — `UPDATE` или `DELETE` строки объекта не
затронул ни одной строки, потому что версия не совпала. Если строки больше нет — `IntegrityError`.
Содержит `.model`, `.pk`, `.expected_version` (версия, с которой объект был прочитан); из
`bulk_update()` `.pk` и `.expected_version` равны `None`: пакет знает, сколько объектов устарело, но не
какие.

## <a id="cascadedepthlimiterror"></a>`CascadeDepthLimitError`

Собственный `ON DELETE CASCADE` базы при настоящем удалении остановился на пределе глубины рекурсии и не
дошёл до конца (клиент с `cascade_depth_limit` в `Features`; в SQLite это
`SqliteTriggerRecursionLimitError`, глубже `SQLITE_LIMIT_TRIGGER_DEPTH`). `delete()` выполняет такое
удаление в транзакции, перехватывает эту ошибку, откатывает попытку и проходит каскад в Python; до вашего
кода она доходит только из «сырого» DELETE через `execute()`.

## <a id="unsupportederror"></a>`UnSupportedError`

База, диалект, драйвер или версия сервера подключения не умеют того, что попросили: оператор фильтра,
`select_for_update()`, `stream()`, `distinct(*fields)`, `bulk_create(returning=/use_copy=)`, транзакцию
или уровень изоляции, двухфазную фиксацию, тип поля, ограничение, параметр индекса или тип триггера,
`.explain(output_format=...)` в SQLite (простой `.explain()` там тоже работает, через
`EXPLAIN QUERY PLAN`), или сервер старше того, с которым работает диалект. Её же даёт оператор фильтра,
получивший аргумент другого вида: `__year=` не `int`, `__isnull=` не `bool`, `__in=` не список, `has_key=`
не текст; значение, которое нельзя привести к типу поля, — это `ValidationError`. Бросается до отправки
чего-либо. Ошибка в самом объявлении, на любой базе, — это `ConfigurationError`.

## <a id="package-exceptions"></a>Исключения других пакетов

Несколько пакетов бросают подклассы исключений выше, поэтому их перехватывает тот же `except`:

| Модуль | Исключения |
|---|---|
| `hare.migrations.exceptions` | `HareMigrationError` и его подклассы — см. [Ошибки миграций](migration-errors.ru.md). |
| `hare.fields.validators.exceptions` | `ValidationError` встроенных валидаторов: `InvalidURL` (и его `InvalidScheme`), `InvalidDomainName`, `InvalidEmailAddress`, `InvalidSlug`, `InvalidPhoneNumber`. |
| `hare.inspectdb.exceptions` | `ManyToManyThroughTableSkippedError`, `DuplicateModelClassNameError`, `SchemaNotFoundError` (каждое — `ConfigurationError`), `TableNotFoundError` (`OperationalError`), `UnsupportedDialectError` (`UnSupportedError`) — см. [Модели по существующей базе](../migrations/inspectdb.ru.md). |
| `hare.sql.exceptions` | `HareSqlException` (`QueryError`) и его `QueryException`, `GroupingException`, `CaseException`, `JoinException`, `SetOperationException`, `FunctionException`: команда, которую сборщик SQL не может составить. |
| `hare.dialects.sqlite.exceptions` | `SqliteTriggerRecursionLimitError` (`CascadeDepthLimitError`). |

Пакеты contrib, которые отвечают на запросы или доставляют события, бросают вместо этого исключения типов
стандартной библиотеки, чтобы окружающий код обрабатывал их, ничего не зная о hare:

| Модуль | Исключения |
|---|---|
| `hare.contrib.request_query.exceptions` | `InvalidRequestQuery` (`ValueError`, `.errors`): значения запроса не составляют корректный запрос — адаптер Litestar отвечает `400 Bad Request`, адаптеры FastAPI и Robyn — `422 Unprocessable Content`. `RequestQueryForbidden` (`PermissionError`, `.parameter`): запрос просит строки, которые ему видеть нельзя, — `403 Forbidden`. |
| `hare.contrib.outbox.exceptions` | `DeliveryError`: событие outbox не доставлено. |
| `hare.cli.exceptions` | `CLIError`: команда `hare` не выполнилась — код выхода 1. `CLIUsageError` (это `CLIError`): команду вызвали неверно — код выхода 2. См. [Команда `hare`](../migrations/cli.ru.md#adding-your-own-commands). |

## <a id="sqlerrormixin"></a>SQL ошибки базы данных

Каждое `DatabaseError` — `OperationalError`, `IntegrityError`, `DBConnectionError` и их подклассы —
содержит запрос, который не выполнился:

```python
class DatabaseError(HareError):
    sql: str | None
    parameters: list[Any] | None

    def __init__(self, *args: Any, sql: str | None = None, parameters: list[Any] | None = None) -> None: ...
```

Если задан `sql`, `str(error)` его включает — удобно для журналов и Sentry, не нужно доставать его из
исключения вручную. Параметры запроса в `str(error)` никогда не попадают (как и в событие исключения,
которое записывает трассировка OpenTelemetry): в них могут быть пароли, токены или персональные данные.
Они остаются в `error.parameters` для отладки — пишите их в журнал только туда, куда этим данным можно:

```python
try:
    await Book.objects.raw(bad_sql)
except OperationalError as error:
    logger.error("query failed", extra={"sql": error.sql})
    logger.debug("query params", extra={"parameters": error.parameters})
```

Сообщение, которое дают драйвер или сам сервер, всё равно может назвать значение, например у `asyncpg`:
`invalid input for query argument $1: 'value'`. hare преобразует значение через его поле везде, где поле
известно, — обычное значение фильтра или `update()`, `Value(...)`, сравниваемое с колонкой или
записываемое в неё, текст в арифметике с числовой колонкой (`F("pin") + "5"`), — поэтому неверное
значение сначала даёт `ValidationError` поля (и скрывает значение поля с `sensitive=True`). Константа,
которую база встречает без поля (`annotate(x=Value(...))`, сравниваемое позже, параметр готового SQL,
аргумент функции), доходит до драйвера как есть, и текст его ошибки может её показать.

## <a id="handling-patterns"></a>Как обрабатывать

Как перехватить исключения hare там, где приложение превращает их в свои ошибки.

Удаление автора, на которого книги ещё ссылаются через `on_delete=PROTECT`:

```python
try:
    await author.delete()
except ProtectedError as error:
    raise ValidationError(f"{len(error.protected_objects)} object(s) still reference this row")
```

Выдача, которую другой запрос изменил после того, как её прочитали, с `Meta.optimistic_lock_field`:

```python
try:
    await loan.save()
except StaleObjectError as error:
    # кто-то изменил строку раньше — перечитайте и повторите или покажите пользователю конфликт
    logger.warning("stale write on %s pk=%s, expected version %s", error.model.__name__, error.pk, error.expected_version)
    ...
```

Строка, которой может не быть, — с `does_not_exist_exception=None` метод `get()` возвращает `None`, а не
бросает `DoesNotExist`:

```python
book = await Book.objects.get(pk=book_id, does_not_exist_exception=None)
if book is None:
    raise NotFound()
```
