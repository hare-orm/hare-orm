# Транзакции и блокировки в ClickHouse

ClickHouse выполняет транзакции над таблицами семейства `MergeTree` на сервере с ClickHouse Keeper.
Это экспериментальная возможность сервера, поэтому hare пользуется ею, только когда об этом просит
подключение. Блокировки строк тоже хранятся в ClickHouse Keeper.

## <a id="transactions"></a>Транзакции

```python
"connections": {
    "analytics": "clickhouse+clickhouse-connect://default:secret@clickhouse.local:8123/analytics?transactions=true"
}
```

С `transactions=true` `Transactions.atomic()` выполняет транзакцию ClickHouse. Сервер должен работать
с ClickHouse Keeper и включённой `allow_experimental_transactions`: подключение проверяет это при
открытии командами `BEGIN TRANSACTION` и `ROLLBACK`, а иначе даёт `ConfigurationError`. Без
`transactions=true` `atomic()` даёт `UnSupportedError`.

```python
async with Transactions.atomic():
    await Entry.objects.create(id=1, account="a", amount=10)
    await Entry.objects.filter(account="b").update(amount=0)
    await Note.objects.filter(entry_id=3).delete()
```

- **Собственное соединение.** Транзакция держит HTTP-сессию (clickhouse-connect) или TCP-соединение
  (clickhouse-driver) от `BEGIN TRANSACTION` до `COMMIT`/`ROLLBACK`; её команды выполняются на нём по
  одной, команды параллельных задач ждут своей очереди.
- **Что в ней выполняется.** Вставки — и двоичные вставки `bulk_create()`, — `SELECT`, мутации и лёгкий
  `DELETE` таблиц `MergeTree`. Команда, которую сервер не выполняет в транзакции, — DDL, запрос к
  таблице `system`, любая команда таблицы `Replicated` — даёт `UnSupportedError`.
- **Первая неудачная команда завершает транзакцию.** Сервер откатывает её и принимает только
  `ROLLBACK`: следующая команда даёт `TransactionManagementError`, как и `COMMIT` — после отката.
- **Точек сохранения нет.** Вложенный `atomic()` присоединяется к транзакции, в которую вложен.
  Ошибку, покинувшую вложенный блок, нельзя отменить отдельно от остального: даже если внешний блок её
  перехватит, транзакция при завершении откатывается и даёт `TransactionManagementError` с этой ошибкой.
- **Снимок.** Транзакция читает строки такими, какими они были при её начале, вместе со своими
  записями: `isolation` до `REPEATABLE_READ` выполняется на этом уровне, `SERIALIZABLE` даёт
  `UnSupportedError`. `read_only=True` и `statement_timeout` дают `UnSupportedError` — у ClickHouse
  нет ни того, ни другого для транзакции; `lock_timeout` ограничивает ожидание
  [блокировок строк](#row-locks).
- **Чтение вне транзакции видит незафиксированные строки.** Команда вне всякой транзакции читает
  строки, которые другие транзакции записали, но ещё не зафиксировали. Читатель, которому их видеть
  нельзя, выполняется в собственной транзакции или с настройкой `implicit_transaction=1`.

## <a id="generated-keys"></a>Генерируемые ключи

Модель, ключ которой генерирует база, берёт ключи из серии ClickHouse Keeper (`generateSerialID`,
ClickHouse 25.1) — см. [Модели](models.ru.md#keys). Серии не нужен `transactions=true`.

## <a id="row-locks"></a>Блокировки строк

```python
"clickhouse+clickhouse-connect://default:secret@clickhouse.local:8123/analytics?transactions=true&keeper_hosts=keeper-1:9181,keeper-2:9181"
```

С `transactions=true` и `keeper_hosts` `select_for_update()` блокирует прочитанные строки до конца
транзакции:

```python
async with Transactions.atomic():
    account = await Account.objects.select_for_update().get(id=account_id)
    await Account.objects.filter(id=account_id).update(balance=account.balance - amount)
```

1. Ключи строк читаются в транзакции — с фильтрами, сортировкой и срезом запроса.
2. Каждая строка блокируется эфемерным узлом ClickHouse Keeper `/hare/locks/<база>/<таблица>/<ключ>`
   в сессии, которую транзакция открывает с первой блокировкой. Блокировки берутся в порядке их имён,
   поэтому две транзакции, блокирующие одни и те же строки, никогда не ждут следующую блокировку друг
   друга.
3. Строки читаются по ключам в порядке запроса.

`COMMIT` или `ROLLBACK` закрывает сессию, и Keeper удаляет её узлы — блокировки отдаются.

| Вызов | Со строкой, которую держит другая транзакция |
|---|---|
| `select_for_update()` | Ждёт, пока та транзакция закончится, — до `lock_timeout` транзакции (`atomic(lock_timeout=...)`), затем `OperationalError`; без него — сколько потребуется. |
| `select_for_update(nowait=True)` | Сразу `OperationalError`. |
| `select_for_update(skip_locked=True)` | Пропускает строку; срез заполняется следующими свободными строками. |
| `select_for_update(of=("author",))` | Блокирует строки названных связей, присоединённых `select_related()`; `"self"` — строки самой модели. |

`no_key=True` берёт ту же блокировку; `share=True` и `key_share=True` дают `UnSupportedError` —
блокировка Keeper исключительная. `get()`, `first()`, `values()`, `iterator()` и `stream()`
блокируют строки так же.

- **Строка изменилась, пока её ждали.** Транзакция читает снимок на своё начало, поэтому после
  ожидания блокировки она ещё раз читает дождавшиеся строки вне транзакции. Если другая транзакция
  тем временем изменила или удалила строку, будет `TransactionRetryError` — выполните транзакцию
  заново, как при ошибке сериализации в PostgreSQL (это делает `atomic(retries=...)`).
- **Блокировки защищают только от `select_for_update()`.** Запись через hare без
  `select_for_update()` или команда не из hare их не ждёт. Блокируйте строки, которые собираетесь
  менять, в каждой транзакции, которая их меняет.
- **Одна блокировка на строку.** Транзакция держит не больше 100 000 блокировок; сверх этого —
  `QueryError`.
- **Потерянная сессия.** Если сессия Keeper с блокировками закончится раньше транзакции (сеть,
  перезапуск Keeper), строки могла уже заблокировать другая транзакция: следующая блокировка и
  `COMMIT` дают `TransactionManagementError`, и ничего не фиксируется.
- **Вне транзакции** `select_for_update()` даёт `QueryError`, как в любой базе; без `keeper_hosts` —
  `UnSupportedError`.

hare сам говорит с Keeper по протоколу ZooKeeper — отдельная библиотека для этого не ставится.
