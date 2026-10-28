# ClickHouse: транзакции и блокировки

ClickHouse выполняет транзакции над таблицами семейства `MergeTree` на сервере с ClickHouse Keeper. Это
экспериментальная возможность сервера, поэтому hare пользуется ею, только когда об этом просит
подключение. Блокировок строк в ClickHouse нет.

## <a id="transactions"></a>Транзакции

```python
config = {
    "connections": {
        "analytics": "clickhouse+clickhouse-connect://default:secret@clickhouse.local:8123/analytics?transactions=true"
    }
}
```

С `transactions=true` `Transactions.atomic()` выполняет транзакцию ClickHouse. Сервер должен работать с
ClickHouse Keeper и включённой `allow_experimental_transactions`: подключение проверяет это при открытии
командами `BEGIN TRANSACTION` и `ROLLBACK`, а иначе даёт `ConfigurationError`. Без `transactions=true`
`atomic()` даёт `UnSupportedError`. `Transactions.autonomous()` с параметром (`isolation`, `read_only`, ...)
тоже выполняет транзакцию — на своём новом соединении, с откатом, если блок бросает исключение.

```python
async with Transactions.atomic():
    await Loan.objects.create(id=1, book_id=7, reader_id=3)
    await Inventory.objects.filter(book_id=7).update(available=0)
    await Reservation.objects.filter(book_id=7).delete()
```

- **Собственное соединение.** Транзакция держит HTTP-сессию (clickhouse-connect) или TCP-соединение
  (clickhouse-driver) от `BEGIN TRANSACTION` до `COMMIT`/`ROLLBACK`; её команды выполняются на нём по
  одной, команды параллельных задач ждут своей очереди.
- **Что в ней выполняется.** Вставки — и двоичные вставки `bulk_create()`, — `SELECT`, мутации и лёгкий
  `DELETE` таблиц `MergeTree`. Команда, которую сервер не выполняет в транзакции, — DDL, запрос к таблице
  `system`, хранилище которой не участвует в транзакциях (`system.parts`; `system.one` и `system.tables`
  читаются), любая команда таблицы `Replicated` или `Distributed` (модели с `distributed_over`) — даёт
  `UnSupportedError`.
- **Первая неудачная команда завершает транзакцию.** Сервер откатывает её и принимает только `ROLLBACK`:
  следующая команда даёт `TransactionManagementError`, как и `COMMIT` — после отката. Команда, которую
  клиент перестал ждать (отменённая задача, `asyncio.wait_for`), продолжает выполняться в HTTP-сессии
  транзакции; откат ждёт её — не дольше таймаута сессии сервера, — и строки транзакции не остаются
  видимыми.
- **Точек сохранения нет.** Вложенный `atomic()` присоединяется к транзакции, в которую вложен. Ошибку,
  покинувшую вложенный блок, нельзя отменить отдельно от остального: даже если внешний блок её
  перехватит, транзакция при завершении откатывается и даёт `TransactionManagementError` с этой ошибкой.
- **Снимок.** Транзакция читает строки такими, какими они были при её начале, вместе со своими записями:
  `isolation` до `REPEATABLE_READ` выполняется на этом уровне, `SERIALIZABLE` даёт `UnSupportedError`.
  `read_only=True`, `statement_timeout` и `lock_timeout` дают `UnSupportedError` — у ClickHouse нет
  ничего из этого для транзакции и нет блокировок, которых можно ждать.
- **Чтение вне транзакции видит незафиксированные строки.** Команда вне всякой транзакции читает строки,
  которые другие транзакции записали, но ещё не зафиксировали. Читатель, которому их видеть нельзя,
  выполняется в собственной транзакции или с настройкой `implicit_transaction=1`.
- **Часть, которую держит другая транзакция.** Мутация транзакции (`update()`, изменение при захвате
  изменений) может наткнуться на часть таблицы, которую держит фоновое слияние сервера; сервер отклоняет
  её ошибкой сериализации, которую hare бросает как `TransactionRetryError`: транзакция откатывается, а
  её повтор удаётся, когда слияние закончится (это делает `atomic(retries=...)`). Запись модели с
  `Meta.change_capture` идёт в такой транзакции и без `atomic()`.

## <a id="generated-keys"></a>Генерируемые ключи

Модель, ключ которой генерирует база, берёт ключи из серии ClickHouse Keeper (`generateSerialID`) —
см. [Модели](models.ru.md#keys). Серии не нужен `transactions=true`.

## <a id="row-locks"></a>Блокировки строк

ClickHouse не блокирует строк: `select_for_update()` даёт `UnSupportedError` до отправки SQL — в транзакции
и вне её, — и так же `RowChanges.get_for_update()` запроса из запроса API. Запись не может дождаться
окончания другой: два `update()` одной строки применяются оба, и остаются значения более поздней мутации.

ClickHouse Keeper (`keeper_hosts`) хранит [блокировку миграций](../../migrations/migrations.ru.md#migration-lock)
`migrate`, а удаление базы (`db_delete()`) удаляет её узлы там — [серии её ключей](models.ru.md#keys) и узел её
блокировки; hare сам говорит с ним по протоколу ZooKeeper — библиотека для этого не ставится.
