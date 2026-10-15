# Распределённые транзакции

`Transactions.distributed()` записывает в несколько баз PostgreSQL одной транзакцией через двухфазную
фиксацию: либо каждая база сохраняет свою запись, либо ни одна, в том числе при падении процесса
посередине.

```python
class Transactions:
    @staticmethod
    def distributed(
        coordinator: str, participants: Sequence[str]
    ) -> AbstractAsyncContextManager[DistributedTransactions]: ...
```

Выдача книги записывается в базу библиотеки, экземпляр снимается с полки в базе склада, а залог читателя
— в базе расчётов: все три записи или ни одной:

```python
from hare.query.expressions import F
from hare import Transactions

async with Transactions.distributed(coordinator="library", participants=["inventory", "billing"]) as distributed:
    await Loan.objects.using(distributed.coordinator).create(book_id=book.pk, reader_id=reader.pk)
    await Inventory.objects.using(distributed["inventory"]).filter(book_id=book.pk).update(
        available=F("available") - 1
    )
    await Deposit.objects.using(distributed["billing"]).create(reader_id=reader.pk, amount=5)
```

`distributed.coordinator` и `distributed[alias]` дают уже открытый клиент координатора и каждого
участника, чтобы передавать его как `using=`; `distributed[coordinator]` — тоже клиент координатора.
Протокол — собственные команды PostgreSQL `PREPARE TRANSACTION`/`COMMIT PREPARED`/`ROLLBACK PREPARED`:
если [`on_rollback()`](transactions.ru.md#on-rollback) лишь выполняет обратное действие, то
распределённая транзакция никогда не оставит одну базу записанной, а другую нет.

## <a id="how-the-commit-runs"></a>Как проходит фиксация

При обычном выходе из блока:

1. Каждый участник выполняет `PREPARE TRANSACTION` — если это не удаётся хотя бы у одного, всё (включая
   ещё не тронутого координатора) откатывается, и исходное исключение передаётся дальше.
2. Координатор обычным `COMMIT` записывает строку в свою таблицу `hare_distributed_decisions` (создаётся
   автоматически при первом использовании). **Эта фиксация — единственный атомарный момент, в который вся
   распределённая транзакция надёжно происходит**; внешний координатор или отдельный журнал не нужны.
3. Каждому участнику отправляется `COMMIT PREPARED`. Если у кого-то это не удалось, распределённая
   транзакция уже зафиксирована на шаге 2 — поэтому даётся
   `DistributedTransactionPartiallyCommittedError`, а не ошибка, будто не удалось всё; выполните
   `hare distributed-recover`, чтобы довести дело до участников, названных в исключении.

Если собственный `COMMIT` координатора на шаге 2 завершился с неизвестным исходом (соединение оборвалось
после того, как сервер его получил), даётся `DistributedTransactionCommitAmbiguousError`: каждый участник
всё ещё держит подготовленную транзакцию, и `hare distributed-recover` завершает их по записи о решении.

Исключение внутри самого блока (до шага 1) откатывает всё как обычно. Пустой `participants`, подключение,
указанное дважды (в том числе координатор среди участников), и подключение, уже находящееся в транзакции,
дают `QueryError` до того, как что-либо открывается.

Отмена, пришедшая во время выполнения `PREPARE TRANSACTION` у участника, ждёт его завершения, поэтому
участник, который успел подготовиться, всегда завершается через `ROLLBACK PREPARED`, а не остаётся без
записи о решении.

## <a id="databases"></a>Базы с двухфазной фиксацией

Она есть только в PostgreSQL: в SQLite и ClickHouse `PREPARE TRANSACTION` нет. Каждое подключение,
переданное в `distributed()`, — координатор и все участники — должно поддерживать двухфазную фиксацию
(`Features.supports_two_phase_commit`); иначе `UnSupportedError`.

## <a id="max-prepared-transactions"></a>Настройка сервера `max_prepared_transactions`

Во многих установках PostgreSQL по умолчанию `max_prepared_transactions = 0`, что полностью отключает
`PREPARE TRANSACTION`. `distributed()` в этом случае даёт `ConfigurationError` с именем подключения —
задайте ненулевое значение в `postgresql.conf` и перезапустите PostgreSQL. Когда же заняты все места
(«maximum number of prepared transactions reached»), `ConfigurationError` указывает на
`hare distributed-recover` и `pg_prepared_xacts` — места занимают оставшиеся подготовленные транзакции.
Любая другая ошибка `PREPARE TRANSACTION` передаётся как есть.

## <a id="recovering-stuck-prepared-transactions"></a>Восстановление зависших подготовленных транзакций

Если `COMMIT PREPARED` у участника не удался сразу (шаг 3 выше) или процесс упал посреди протокола,
`hare distributed-recover` находит и завершает такие транзакции:

```console
$ hare distributed-recover --coordinator library
$ hare distributed-recover --coordinator library --finish
$ hare distributed-recover --coordinator library --finish --older-than 60
```

Без `--finish` команда только сообщает, что нашла. Запись о решении без `resolved_at` означает, что
распределённая транзакция уже зафиксирована, — каждому участнику, у которого ещё висит эта подготовленная
транзакция, нужен `COMMIT PREPARED`. Подготовленная транзакция без соответствующей записи о решении нигде
так и не была зафиксирована — считается отменённой, `ROLLBACK PREPARED`. `--older-than` (секунды, по
умолчанию 300) пропускает всё, что ещё может выполняться в живом вызове; `0` ничего не пропускает, что бы
ни показывали часы сервера. Повторный запуск безопасен.
