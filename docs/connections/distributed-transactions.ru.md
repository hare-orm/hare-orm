# Распределённые транзакции: `Transactions.distributed()`

```python
async with Transactions.distributed(coordinator="db1", participants=["db2", "db3"]) as txns:
    await Widget.objects.using(txns.coordinator).create(...)
    await Job.objects.using(txns["db2"]).create(...)
    await Audit.objects.using(txns["db3"]).create(...)
```

Если [`on_rollback()`](transactions.ru.md#on-rollback) даёт только обратное действие, то `distributed()` — настоящий протокол
двухфазной фиксации (2PC) на основе собственных команд PostgreSQL `PREPARE TRANSACTION`/
`COMMIT PREPARED`/`ROLLBACK PREPARED`: либо запись дойдёт до каждой базы, либо ни до одной — даже если
процесс упадёт посреди протокола.

`txns.coordinator` и `txns[alias]` дают уже открытый клиент координатора и каждого участника, чтобы
передавать его как `using=`. При обычном выходе из блока:

1. Каждый участник выполняет `PREPARE TRANSACTION` — если это не удаётся хотя бы у одного, всё
   (включая ещё не тронутого координатора) откатывается, и исходное исключение передаётся дальше.
2. Координатор обычным `COMMIT` записывает строку в свою таблицу `hare_distributed_decisions`
   (создаётся автоматически при первом использовании). **Эта фиксация — единственный атомарный
   момент, в который вся распределённая транзакция надёжно происходит**; внешний координатор или
   отдельный журнал не нужны.
3. Каждому участнику отправляется `COMMIT PREPARED`. Если у кого-то это не удалось, распределённая
   транзакция уже зафиксирована на шаге 2 — поэтому даётся
   `DistributedTransactionPartiallyCommittedError`, а не ошибка, будто не удалось всё; выполните
   `hare distributed-recover`, чтобы довести дело до участников, названных в исключении.

Исключение внутри самого блока (до шага 1) откатывает всё как обычно.

!!! note "Только PostgreSQL"
    В SQLite `PREPARE TRANSACTION` нет вовсе. Каждое подключение, переданное в `distributed()`
    (координатор и все участники), должно быть подключением PostgreSQL — иначе `UnSupportedError`.

!!! warning "`max_prepared_transactions`"
    Во многих установках PostgreSQL по умолчанию `max_prepared_transactions = 0`, что полностью
    отключает `PREPARE TRANSACTION`. `distributed()` в этом случае даёт понятную `ConfigurationError` с
    именем подключения — задайте ненулевое значение в `postgresql.conf` и перезапустите PostgreSQL.
    Когда же заняты все места («maximum number of prepared transactions reached»),
    `ConfigurationError` указывает на `hare distributed-recover` и `pg_prepared_xacts` — места занимают
    оставшиеся подготовленные транзакции. Любая другая ошибка `PREPARE TRANSACTION` передаётся как есть.

    Отмена, пришедшая во время выполнения `PREPARE TRANSACTION` у участника, ждёт его завершения,
    поэтому участник, который успел подготовиться, всегда завершается через `ROLLBACK PREPARED`, а не
    остаётся без записи о решении.

## Восстановление зависших подготовленных транзакций {: #recovering-stuck-prepared-transactions }

Если `COMMIT PREPARED` у участника не удался сразу (шаг 3 выше) или процесс упал посреди протокола,
`hare distributed-recover` находит и завершает такие транзакции:

```console
$ hare distributed-recover --coordinator db1
$ hare distributed-recover --coordinator db1 --finish
$ hare distributed-recover --coordinator db1 --finish --older-than 60
```

Без `--finish` команда только сообщает, что нашла. Запись о решении без `resolved_at` означает, что
распределённая транзакция уже зафиксирована, — каждому участнику, у которого ещё висит эта
подготовленная транзакция, нужен `COMMIT PREPARED`. Подготовленная транзакция без соответствующей записи
о решении нигде так и не была зафиксирована — считается отменённой, `ROLLBACK PREPARED`. `--older-than`
(секунды, по умолчанию 300) пропускает всё, что ещё может выполняться в живом вызове; `0` ничего не
пропускает, что бы ни показывали часы сервера. Повторный запуск безопасен.
