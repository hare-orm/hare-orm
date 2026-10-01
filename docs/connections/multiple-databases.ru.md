# Несколько баз данных

## `using=` {: #using-db }

Подключение везде выбирается одинаково — одним именем `using`, которое принимает имя подключения
(`"replica"`) или клиент (клиент транзакции, клиент из `autonomous()`, маршрутизатора или
`Connections.get(alias)`):

**У QuerySet**: `.using(alias_or_client)` — копия запроса, закреплённая за этим подключением. С
него начинается любой запрос, поэтому это покрывает `create`, `get`, `get_or_create`,
`update_or_create`, `filter`, `bulk_create`, `bulk_update`, `raw`, `count`, ...:
`Book.objects.using("replica").filter(...)`, `Book.objects.using(transaction).create(...)`.

**У объекта**: аргумент `using=` у `save`, `delete`, `hard_delete`, `delete_preview`, `restore` и
`refresh_from_db`; у записей через связь (`add`, `remove`, `set`, `clear`, `create`, ...); и у
`prefetch_related_objects()`.

**В остальных местах**: `Transactions.atomic(using)`, `on_commit(..., using=)`,
`on_rollback(..., using=)`, `execute_sql(..., using=)`.

!!! note "`select_for_update()` требует открытой транзакции"
    В отличие от остальных методов выше, выполнение запроса `select_for_update()` вне блока
    `Transactions.atomic()` на базе, которая умеет блокировать строки, даёт
    `QueryError`: иначе блокировка была бы взята и снята в той же команде с автоматической
    фиксацией, не давая настоящей защиты, но создавая её видимость.

**У `ManyToManyRelation`**: `create`, `add`, `remove`, `clear`.

```python
async with Transactions.autonomous() as conn:
    job = await Job.objects.using(conn).create(status="running")
    await job.tags.add(tag, using=conn)
```

### Объект помнит своё подключение {: #instances-remember-their-connection }

Объект модели помнит имя подключения, из которого он загружен (включая объекты, построенные
`select_related()`/`prefetch_related()`, `stream()`, `union()` и `raw()`), или в которое последний раз
сохранён (`save()`, `create()`, `get_or_create()`, `bulk_create()`). Каждая следующая операция с ним без
`using=` по умолчанию идёт туда: `save()`, `delete()`, `restore()`, `refresh_from_db()`,
`prefetch_related_objects()`, ожидание прямой связи (`await book.author`) и связи объекта
(`author.books.all()`/`.filter()`/`.count()`/`.values()`, `add()`/`remove()`/`clear()` у
«многие-ко-многим»):

```python
book = await Book.objects.all().using("replica").get(title="Notes")
author = await book.author          # прочитан из "replica", а не из "default"
books = await author.books.all()    # тоже "replica"
```

Подключение ищется по имени, когда выполняется запрос, поэтому внутри открытого `atomic()` на
том же подключении используется транзакция. Явный `using=`/`.using()` всегда важнее, как и
маршрутизатор, у которого есть мнение о запрашиваемой модели. Связанная модель, подключение по
умолчанию которой отличается от подключения модели объекта (связь между разными базами), и связь,
прочитанная из объекта модели, которую направляет маршрутизатор, сохраняют собственный выбор
подключения.

## Маршрутизаторы {: #routers }

Напишите **обычный класс** — наследовать ничего не нужно, hare вызывает его методы по имени — с одним
или обоими методами, каждый из которых возвращает **имя подключения** (или `None`):

```python
class ReportingRouter:
    def db_for_read(self, model: type[Model]) -> str | None:
        if model._meta.app == "reporting":
            return "reporting_replica"
        return None

    def db_for_write(self, model: type[Model]) -> str | None: ...
```

Любого из методов может не быть вовсе — hare пропускает маршрутизатор для действия, которого тот не
реализует, а не выдаёт ошибку. Побеждает первый настроенный маршрутизатор, вернувший не `None` (как в
Django); если таких нет, используется обычное подключение модели по умолчанию. Возвращённое имя,
которому не соответствует ни одно настроенное подключение, даёт `ConfigurationError`: маршрутизатор,
который сработал, но назвал подключение с опечаткой или устаревшее, считается настоящей ошибкой
настройки и не пропускается молча.

Чтение, от которого зависит запись, идёт через подключение `db_for_write`, как в Django: проверка
существования в `get_or_create()`, чтение с блокировкой в `update_or_create()` и
`select_for_update()`. На реплике может ещё не быть строки, которая уже есть на основной базе
(отставание репликации или строка, созданная раньше в ещё открытой транзакции), и промах там создал
бы дубликат.

Регистрируются через `Hare.init(routers=[...])` — списком путей через точку
(`"my_app.routers.ReportingRouter"`) или самих классов; каждый создаётся без аргументов. У каждого
`HareContext` свой `hare.core.router.ConnectionRouter` — внутренний диспетчер контекста, который
хранит ваши маршрутизаторы и опрашивает их по очереди (`ctx.router`), — поэтому настройки разных
контекстов никогда не смешиваются.
