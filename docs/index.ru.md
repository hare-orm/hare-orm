# hare-orm

Асинхронная ORM для Python с PostgreSQL, SQLite и ClickHouse, спроектированная с упором на связи между
моделями. Модели — обычные классы, запросы строятся лениво через `QuerySet`, а миграции создаются по
изменениям моделей. См. [Если вы пришли с Django](getting-started/coming-from-django.ru.md).

- **Быстрая.** Драйвер PostgreSQL на Rust разбирает строки и преобразует значения вне Python, а каждая
  форма запроса строится один раз: повторный запрос только подставляет новые значения — см.
  [Производительность](benchmarks/index.ru.md).
- **Связи как надо.** Составные первичные ключи везде — и как цель внешнего ключа, обобщённые связи из
  настоящих внешних ключей, `select_related()`/`prefetch_related()` с условиями, рекурсивные запросы по
  дереву и ошибка вместо незаметно завышенного агрегата — см. [Модели и связи](#models-and-relations).
- **Схема — в объявлениях.** Ограничения, частичные индексы и индексы по выражениям, триггеры,
  представления, материализованные представления, функции, последовательности, политики защиты строк,
  права и секционирование описываются в `Meta` модели, а создают и меняют их миграции — см.
  [Схема в `Meta`](#schema-in-meta).
- **Готовые прикладные приёмы.** Мягкое удаление, версии строк, разделение данных по арендаторам (по
  колонке, по схеме или защитой строк в самой базе), оптимистичная блокировка, отслеживание изменённых
  полей и transactional outbox — опции `Meta` — см. [Поведение в `Meta`](#behavior-in-meta).
- **Миграциям можно доверять.** Они выводятся из моделей, проверяются на операции, блокирующие
  нагруженную таблицу, сравниваются с живой базой (`hare drift`) и создаются по существующей базе
  (`hare inspectdb`) — см. [Миграции](#migrations).
- **Каждая база — на своих условиях.** SQLite, PostgreSQL и ClickHouse — диалекты на том же публичном
  API, которым пользуется сторонний пакет; с PostgreSQL приходят его типы, индексы, PostGIS, pgvector,
  полнотекстовый поиск и `LISTEN`/`NOTIFY`, а база без нужной возможности отвечает понятной ошибкой
  вместо неверного результата — см. [Базы данных](#databases).
- **Готова к продакшену.** Наблюдатели, OpenTelemetry, теги в комментариях SQL, детектор N+1, метрики
  пула, распределённые транзакции, реплики для чтения и маршрутизаторы, поддержка PgBouncer, схемы
  Pydantic и интеграции с Litestar, FastAPI и Robyn — см. [Наблюдаемость](#observability).
- **Проверяется до запуска.** Плагин mypy отклоняет опечатку в фильтре и выводит типы строк `values()`;
  плагин pytest, фабрики и проверки числа запросов покрывают тесты — см.
  [Тесты и проверка типов](#testing-and-type-checking).

## <a id="quick-start"></a>Быстрый старт

Установите пакет, объявите две модели и запросите их:

```bash
pip install hare-orm
```

```python
# library/models.py
from hare import fields
from hare.models import Model


class Author(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=120)


class Book(Model):
    id = fields.IntField(primary_key=True)
    author = fields.ForeignKeyField("models.Author", related_name="books")
    title = fields.CharField(max_length=300)
    rating = fields.IntField(default=0)
```

```python
# main.py
import asyncio

from hare import Hare, HareConfig
from library.models import Author, Book


async def main() -> None:
    await Hare.init(HareConfig.from_db_url("sqlite+aiosqlite://db.sqlite3", {"models": ["library.models"]}))
    await Hare.generate_schemas()

    author = await Author.objects.create(name="Ursula K. Le Guin")
    await Book.objects.create(author=author, title="The Left Hand of Darkness", rating=5)

    async for book in Book.objects.filter(author__name__icontains="le guin").select_related("author"):
        print(book.title, "—", book.author.name)

    await Hare.close_connections()


asyncio.run(main())
```

В настоящем проекте схема ведётся миграциями, а не `generate_schemas()`: `hare makemigrations`, затем
`hare migrate`. См. [Первая модель](getting-started/first-model.ru.md).

## <a id="models-and-relations"></a>Модели и связи

Модель — класс из полей, и связь — тоже поле — см. [Типы полей](models/field-types.ru.md) и
[Связи](models/relations.ru.md).

```python
from hare import fields
from hare.ddl import RawSQLTerm
from hare.models import Model


class Author(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=120)


class Shelf(Model):
    store = fields.CharField(max_length=20)
    number = fields.IntField()
    pk = fields.CompositePrimaryKey("store", "number")      # составной первичный ключ


class Tag(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50, unique=True)


class Book(Model):
    id = fields.IntField(primary_key=True)
    isbn = fields.CharField(max_length=13, unique=True)
    title = fields.CharField(max_length=300)
    author = fields.ForeignKeyField("models.Author", related_name="books")
    shelf = fields.ForeignKeyField("models.Shelf", related_name="books", null=True)  # обе колонки ключа
    tags = fields.ManyToManyField("models.Tag", related_name="books")
    rating = fields.IntField(default=0)
    published_at = fields.DateField(null=True)
    price = fields.DecimalField(max_digits=10, decimal_places=2)
    quantity = fields.IntField(default=0)
    stock_value = fields.GeneratedField(                     # вычисляет база
        RawSQLTerm("price * quantity"), output_field=fields.DecimalField(max_digits=12, decimal_places=2)
    )
    supplier_token = fields.EncryptedTextField(null=True)   # шифруется в базе, скрыто в логах


class Category(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=100)
    parent = fields.ForeignKeyField("models.Category", related_name="children", null=True)


class Comment(Model):
    id = fields.IntField(primary_key=True)
    target = fields.GenericForeignKeyField({"book": Book, "shelf": Shelf}, related_name="comments")
    text = fields.TextField()
```

- **Составные первичные ключи** работают везде, где работает ключ из одной колонки: как цель внешнего
  ключа с настоящим табличным `FOREIGN KEY`, в соединениях, каскадах, `prefetch_related()`, фильтрах
  `__in` и курсорах. Таблица может быть и
  [вовсе без первичного ключа](models/meta-options.ru.md#primary_key).
- **`GenericForeignKeyField`** — исключающая дуга из настоящих внешних ключей: на каждую цель колонка,
  внешний ключ и индекс, а `CHECK` следит, чтобы был задан ровно один, — так что согласованность держит
  сама база.
- Каждое `on_delete` — `CASCADE`, `RESTRICT`, `PROTECT`, `SET_NULL`, `SET_DEFAULT` — выполняется и для
  строк, которые удаляет `QuerySet.delete()`, а `delete_preview()` показывает, до чего дойдёт удаление
  строки.
- Есть поля для JSON (с поиском по путям), перечислений, десятичных чисел, UUID, двоичных данных, дат и
  времени с часовым поясом, массивов, диапазонов, геометрии и векторов;
  [свои поля](extending/custom-fields.ru.md) подключаются к тому же механизму.

## <a id="queries"></a>Запросы

`QuerySet` строит запрос лениво и выполняет его при `await` — см.
[Методы QuerySet](querying/queryset-methods.ru.md).

```python
from hare import Transactions
from hare.query.expressions import Case, Exists, F, OuterReference, Q, Subquery, Value, When, Window
from hare.query.functions import Avg, Count, Sum
from hare.query.functions.window import Lag, Rank

# Фильтры через связи, объекты Q и выражения F
await Book.objects.filter(Q(title__icontains="dark") | Q(rating__gte=4), shelf__store="north")
await Book.objects.filter(quantity=0).update(quantity=F("quantity") + 10, rating=F("rating") - 1)

# Агрегаты с группировкой по выбранному
await Author.objects.annotate(book_count=Count("books"), avg_rating=Avg("books__rating")).filter(book_count__gte=3)
await Book.objects.values("author__name").annotate(total=Sum("quantity")).order_by("-total")

# Подзапросы, Exists и условные выражения
latest = Book.objects.filter(author=OuterReference("pk")).order_by("-published_at").values("title")[:1]
await Author.objects.annotate(
    latest_title=Subquery(latest),
    has_bestseller=Exists(Book.objects.filter(author=OuterReference("pk"), rating=5)),
)
await Book.objects.annotate(level=Case(When(rating__gte=4, then=Value("good")), default=Value("other")))

# Оконные функции
await Book.objects.annotate(
    rank_in_author=Window(Rank(), partition_by=["author_id"], order_by=["-rating"]),
    previous_price=Window(Lag("price"), partition_by=["author_id"], order_by=["published_at"]),
)

# Категория и все её потомки одним запросом WITH RECURSIVE
await Category.objects.filter(pk=root.pk).with_recursive("children", max_depth=5)

# Связи, загруженные вместе со строками, постраничный вывод по ключу, заблокированная строка
await Book.objects.select_related("author").prefetch_related("tags")
newest = Book.objects.order_by("-published_at", "id")
next_page = await newest.after_cursor(*newest.cursor_values(last_book)).limit(20)
async with Transactions.atomic():
    book = await Book.objects.select_for_update(skip_locked=True).get(id=book_id)

# Вставка с обновлением при конфликте одной командой
await Book.objects.bulk_create(books, on_conflict=["isbn"], update_fields=["price", "quantity"])
```

- **Типизированные строки.** `values()` и `values_list()` возвращают словари и кортежи, с путями через
  связи, ключи JSON и части даты (`"published_at__year"`).
- **Безопасные агрегаты.** Агрегат рядом с соединением по связи «ко многим», который посчитал бы строку
  дважды, даёт `QueryError` вместо завышенного числа; есть наборы группировок и подытоги.
- **Операции над множествами** (`union()`, `intersection()`, `difference()`), `FilteredRelation`,
  `Lateral`, `JsonTable`, CTE (`with_cte()`), `distinct(<fields>)`, случайная выборка и
  `iterator()`/`stream()` для больших результатов.
- **[Кэш планов запросов](querying/query-plan-cache.ru.md)** хранит SQL каждой формы запроса: второй
  `filter(title__icontains=...)` с другим значением не строит запрос заново, а только подставляет
  значение.
- **[Готовый SQL](querying/raw-sql.ru.md)**, когда он нужен: `RawSQL(...)` внутри запроса, `raw()` с
  экземплярами моделей, `execute_sql()`.

## <a id="behavior-in-meta"></a>Поведение в `Meta`

Арендаторы, мягкое удаление, оптимистичная блокировка, отслеживание изменённых полей и захват изменений —
опции `Meta` — см. [Опции Meta](models/meta-options.ru.md).

```python
from hare.contrib.outbox import ChangeCapture, OutboxEvent
from hare.contrib.versioning import VersionedModel
from hare.models.tenancy.tenancy import Tenancy


class LoanEvent(OutboxEvent):                  # таблица outbox; события уходят в Kafka, RabbitMQ, Redis...
    pass


class Loan(Model):
    id = fields.IntField(primary_key=True)
    library_id = fields.IntField(db_index=True)
    status = fields.CharField(max_length=20)
    version = fields.IntField(default=1)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        tenant_field = "library_id"          # каждый запрос — в пределах активного арендатора
        soft_delete_field = "deleted_at"     # delete() помечает строку; запросы её скрывают
        optimistic_lock_field = "version"    # save() устаревшей копии даёт StaleObjectError
        track_dirty_fields = True            # get_dirty_fields(); save() пишет только изменения
        change_capture = ChangeCapture(LoanEvent)  # каждая запись попадает и в outbox


with Tenancy.scope(library.id):
    overdue = await Loan.objects.filter(status="overdue")    # WHERE library_id = ... AND deleted_at IS NULL
    await overdue[0].delete()                                # мягкое удаление
    restored = await Loan.objects.only_deleted().get(id=loan_id)
    await restored.restore()


class CatalogRecord(VersionedModel):                # новая строка на каждую версию, ключ (id, version)
    title = fields.CharField(max_length=200)


current = await CatalogRecord.get_last_version_or_exception(id=record_id)
draft = await current.create_new_version(title="Second draft")
```

- **[Разделение данных по арендаторам](soft-delete-versions-tenants/multi-tenancy.ru.md)** по колонке, по
  [схеме на арендатора](soft-delete-versions-tenants/schema-per-tenant.ru.md) или силами самой базы через
  защиту строк: ограничение арендатором доходит и до присоединённых моделей и заполняет арендатора новой
  строки.
- **[Мягкое удаление](soft-delete-versions-tenants/soft-delete.ru.md)** каскадом помечает связанные
  строки, которые тоже удаляются мягко, а `restore()` возвращает их.
- **[Transactional outbox](integrations/outbox.ru.md)** записывает событие в транзакции изменения, о
  котором оно сообщает, и доставляет его хотя бы один раз, по порядку для каждой строки, в Kafka,
  RabbitMQ, Redis, вебхук или taskiq.

## <a id="schema-in-meta"></a>Схема в `Meta`

Ограничения, индексы, триггеры, представления, функции и построчная защита тоже объявляются в `Meta`, и
их создают миграции — см. [Ограничения и триггеры](models/constraints-and-triggers.ru.md) и
[Представления, функции, последовательности и доступ](models/schema-objects.ru.md).

```python
from hare.ddl import (
    CheckConstraint, DatabaseFunction, MaterializedView, PartialIndex, Policy, RawSQLTerm,
    RowLevelSecurity, TenantCondition, Trigger, TriggerEvent, TriggerTiming, UniqueConstraint, View,
)
from hare.dialects.postgresql.indexes import GinIndex
from hare.query.expressions import Q


class Invoice(Model):
    id = fields.IntField(primary_key=True)
    tenant_id = fields.IntField()
    number = fields.CharField(max_length=20)
    amount = fields.DecimalField(max_digits=12, decimal_places=2)
    status = fields.CharField(max_length=10)
    tags = fields.JSONField(default=list)

    class Meta:
        tenant_field = "tenant_id"
        constraints = (
            CheckConstraint(name="invoice_amount_positive", check=Q(amount__gt=0)),
            UniqueConstraint(fields=("tenant_id", "number"), condition=Q(status="open")),
        )
        indexes = (PartialIndex(fields=("tenant_id",), condition=Q(status="open")), GinIndex(fields=("tags",)))
        triggers = (
            Trigger(
                name="invoice_no_negative",
                on=TriggerEvent.UPDATE,
                timing=TriggerTiming.BEFORE,
                when=Q(amount__lt=0),
                body=RawSQLTerm("RAISE EXCEPTION 'negative amount'; RETURN NEW;"),
            ),
        )
        views = [View("open_invoices", query=lambda: Invoice.objects.all_tenants().filter(status="open").values("id"))]
        materialized_views = [
            MaterializedView(
                "invoice_totals",
                query=RawSQLTerm("SELECT tenant_id, sum(amount) AS total FROM invoice GROUP BY tenant_id"),
                unique_columns=("tenant_id",),
            )
        ]
        functions = [
            DatabaseFunction(
                "current_tenant",
                returns="integer",
                body=RawSQLTerm("SELECT current_setting('app.tenant')::integer"),
                language="sql",
            )
        ]
        row_level_security = RowLevelSecurity.FORCED
        policies = [Policy(name="invoice_tenant", using=TenantCondition())]
```

Каждый из этих объектов создаёт `migrate`, меняет при изменении объявления и удаляет, когда его убирают;
`hare drift` показывает таблицы, колонки, индексы, ограничения и триггеры, которые в базе разошлись с
моделями. Условия — это объекты `Q` по собственным полям модели, поэтому при переименовании поля оно
переименовывается и в них; готовый SQL — всегда явный `RawSQLTerm(...)`. Таблицы PostgreSQL можно ещё и
[секционировать](models/meta-options.ru.md#table_options) по хешу, списку или диапазону, а рядом объявить
`ExclusionConstraint`, откладываемые ограничения, последовательности и права.

## <a id="migrations"></a>Миграции

Команда `hare` находит изменения моделей, применяет их и проверяет на работающей базе — см.
[Команда `hare`](migrations/cli.ru.md).

```bash
hare makemigrations          # найти изменения моделей
hare migrate                 # применить их
hare checkmigrations         # найти операции, которые заблокируют или перепишут большую таблицу
hare drift library              # сравнить модели приложения с живой базой
hare inspectdb > models.py   # создать модели по существующей базе
hare squashmigrations library 0010
```

```text
WARNING: risky on a database in use if the tables are large - `hare checkmigrations` checks against the database:
  app.0007_remove_book_pages: Remove field pages from Book [remove_field]
    The column of Book.pages is dropped while the code still running reads it.
    Safely: First remove the field from the models only - SeparateDatabaseAndState(state_operations=[RemoveField(model_name='Book', name='pages')]) - and drop the column with RunSQL in a later migration, once no running code reads it.
```

- **[Правила миграций без простоя](migrations/zero-downtime.ru.md)** знают, какие операции переписывают
  таблицу, надолго её блокируют или ломают ещё работающий код, и подсказывают, как сделать каждую
  безопасно: `AddIndex(..., concurrently=True)`, `AddConstraint(..., not_valid=True)` с
  `ValidateConstraint`, `BackfillColumn` порциями и `AlterColumnNotNullSafe` уже есть.
- Переименования, миграции данных (`RunPython`, `RunSQL`), слияние разошедшейся истории, миграции между
  приложениями и подключениями, [миграции без файлов](migrations/migrations-without-files.ru.md) для
  моделей, зарегистрированных во время работы, и команда `hare` с `shell`, `dbshell`, `sqlmigrate` и
  `stubs`.

## <a id="transactions-and-connections"></a>Транзакции и подключения

`Transactions` открывает транзакции, точки сохранения, независимые и распределённые транзакции — см.
[Транзакции](connections/transactions.ru.md).

```python
from hare import Transactions

async with Transactions.atomic(lock_timeout=2.0):
    await loan.save()
    Transactions.on_commit(lambda: send_due_date_reminder(loan.id))      # выполнится после фиксации

async for attempt in Transactions.atomic(isolation="serializable", retries=3):
    async with attempt:                                             # повтор после сбоя сериализации
        await lend(book_id, reader_id)

async with Transactions.autonomous() as connection:                     # фиксируется, даже если вызывающий откатится
    await CatalogImport.objects.using(connection).create(status="started")

async with Transactions.distributed(coordinator="library", participants=["billing"]) as distributed:
    await Loan.objects.using(distributed.coordinator).create(...)       # двухфазная фиксация
    await Deposit.objects.using(distributed["billing"]).create(...)

book = await Book.objects.using("replica").get(id=book_id)              # его связи тоже читаются из "replica"
```

Точки сохранения вкладываются друг в друга, `statement_timeout`/`lock_timeout`/`read_only` задаются для
каждой транзакции, [маршрутизатор](connections/multiple-databases.ru.md) направляет чтение и запись в
свои базы, а за [PgBouncer](connections/connection-poolers.ru.md) 1.21+ подготовленные команды продолжают
работать.

## <a id="databases"></a>Базы данных

SQLite, PostgreSQL и ClickHouse — диалекты одного API; что есть у каждой базы — см.
[Диалекты и их возможности](dialects/dialects-and-features.ru.md).

| | SQLite | PostgreSQL | ClickHouse |
|---|---|---|---|
| Драйвер | `aiosqlite` | `rust_pg` (по умолчанию) или `asyncpg` | `clickhouse-connect` (HTTP) или `clickhouse-driver` (родной протокол TCP) |
| Транзакции | ✓ | ✓ | с `transactions=true` на сервере с ClickHouse Keeper |
| Внешние ключи и `on_delete` | ✓ | ✓ | проверяет и выполняет hare |
| `UPDATE` на месте | ✓ | ✓ | мутация, которая завершается до возврата из вызова, или лёгкий `UPDATE` |
| Полнотекстовый поиск (`__search`, `SearchRank`, `SearchHeadline`) | FTS5 | `tsvector` | — |
| Векторный поиск (`VectorField`, `CosineDistance`) | sqlite-vec | pgvector | — |
| Геометрия (`PointField`, `__dwithin`, `__contains`) | SpatiaLite | PostGIS | гео-типы ClickHouse |

```python
from hare import Connections
from hare.dialects.postgresql.functions import TrigramSimilarity
from hare.search import SearchQuery, SearchRank, SearchType
from hare.vectors import CosineDistance

await Book.objects.filter(description__search=SearchQuery("white whale", search_type=SearchType.PHRASE))
await Book.objects.annotate(rank=SearchRank(("title", "description"), "white whale")).order_by("-rank")
await Book.objects.annotate(distance=CosineDistance("embedding", query_vector)).order_by("distance")[:10]
await Author.objects.annotate(score=TrigramSimilarity("name", "Gerard")).filter(score__gt=0.3)

listener = await Connections.get("default").listen("book_returned", on_notify)   # LISTEN/NOTIFY в PostgreSQL
```

PostgreSQL добавляет `ArrayField`, поля диапазонов и мультидиапазонов, `HStoreField`, `CitextField`,
[PostGIS](dialects/search-and-geodata/gis.ru.md), поиск по триграммам и `unaccent`, а также индексы GIN,
GiST, BRIN, Bloom, Hash, SP-GiST, IVFFlat и HNSW. Таблицы
[ClickHouse](dialects/clickhouse/connecting.ru.md) объявляют движок, ключ сортировки, секционирование,
TTL, проекции и распределение по кластеру; ClickHouse добавляет поля массивов, словарей, кортежей и
`Variant`/`Dynamic`, `final()`, `prewhere()`, `limit_by()` и `with_totals()`, свои агрегаты,
материализованные представления и словари, а также блокировки строк в ClickHouse Keeper. Другая база —
это [пакет диалекта](extending/writing-a-dialect/index.ru.md).

## <a id="web-apis-and-integrations"></a>Веб-API и интеграции

Запрос из параметров HTTP-запроса превращает их в запрос, а интеграции с фреймворками открывают ORM
вместе с приложением — см. [Веб-фреймворки](integrations/web-frameworks.ru.md).

```python
from pydantic import BaseModel, ConfigDict

from hare.contrib.frameworks import PageSchema
from hare.contrib.frameworks.fastapi import HareFastAPI, RequestQueryDependency
from hare.contrib.request_query import OffsetPagination, OrderingConfig, Page, RequestQuery, SearchConfig


class BookSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str


class BookQuery(RequestQuery[Book]):        # ?author=3&search=dark&ordering=-published_at&limit=20
    author: int | None = None
    title__icontains: str | None = None

    class Meta:
        queryset = Book.objects.all().select_related("author")
        search = SearchConfig(fields=("title", "author__name"))
        ordering = OrderingConfig(fields=("title", "published_at"), default=("-published_at",))
        pagination = OffsetPagination(default_limit=50, max_limit=200)


app = HareFastAPI(hare_config=HARE_CONFIG, atomic_requests=True)   # каждый запрос — в транзакции


@app.get("/books", response_model=PageSchema[BookSchema])
async def list_books(books: BookQuery = RequestQueryDependency.provide(BookQuery)) -> Page[Book]:
    return await books.page()
```

[Запросы из параметров](integrations/request-queries.ru.md) превращают параметры HTTP-запроса в
проверенный запрос с фильтрами, поиском, сортировкой и постраничным выводом. Интеграции с
[Litestar](integrations/litestar.ru.md), [FastAPI](integrations/fastapi.ru.md) и
[Robyn](integrations/robyn.ru.md) открывают ORM вместе с приложением, могут выполнять каждый запрос в
транзакции и превращают ошибки ORM в HTTP-статусы; схемы [Pydantic](integrations/pydantic.ru.md)
создаются по моделям, а воркеры [taskiq](integrations/taskiq.ru.md) выполняют каждую задачу в пределах
арендатора кода, который её отправил, помечают её запросы задачей, а сама задача отправляется только
после фиксации транзакции, которая её поставила.

## <a id="observability"></a>Наблюдаемость

Наблюдатели видят каждый запрос, изменённые строки и транзакции; на них построены OpenTelemetry и
детектор N+1 — см. [Наблюдатели](observability/observers.ru.md) и [Поиск повторяющихся запросов
(N+1)](observability/repeated-queries.ru.md).

```python
from hare.contrib.opentelemetry import OpenTelemetryInstrumentor
from hare.contrib.repeated_queries import RepeatedQueryDetector
from hare.instrumentation import Observers, QueryExecuted, QueryTags

OpenTelemetryInstrumentor().instrument()                     # спан на каждый запрос, метрики пула
Observers.observe(QueryExecuted, lambda event: metrics.observe(event.duration_ms))

async with RepeatedQueryDetector(threshold=10, window_seconds=1.0):  # предупреждает о N+1 прямо во время работы
    with QueryTags.scope(job="sync_orders"):                 # ... /*job='sync_orders'*/ у каждого запроса
        await sync_orders()
```

Наблюдатели видят и изменённые строки, и события транзакций; журнал медленных запросов, проверки здоровья
пула и метрики дополняют картину. См. [Наблюдаемость](observability/observers.ru.md).

## <a id="testing-and-type-checking"></a>Тесты и проверка типов

Фабрики создают объекты теста, плагин pytest даёт каждому тесту базу, а плагин mypy проверяет запросы до
запуска — см. [Настройка тестов](testing/setup.ru.md).

```python
from hare.contrib.factories import ModelFactory, Sequence, SubFactory


class AuthorFactory(ModelFactory[Author]):
    name = Sequence(lambda number: f"Author {number}")


class BookFactory(ModelFactory[Book]):
    isbn = Sequence(lambda number: f"{number:013d}")
    title = Sequence(lambda number: f"Book {number}")
    price = 10
    author = SubFactory(AuthorFactory)


async def test_list_books(hare_db, hare_assert_query_count):     # фикстуры встроенного плагина pytest
    await BookFactory.create_batch(3)
    async with hare_assert_query_count(1):
        await Book.objects.select_related("author")
```

```python
Book.objects.filter(titel="War")
# error: Unknown filter param 'titel': Book has no field 'titel'  [hare-query]

rows = await Book.objects.values("id", "title", "author__name")
reveal_type(rows[0])   # TypedDict({'id': int, 'title': str, 'author__name': str})
```

Плагин pytest создаёт тестовые базы, изолирует каждый тест транзакцией, которая затем откатывается, и
запускает тест только там, где у базы есть нужные ему возможности;
[плагин mypy и заглушки для pyright](querying/type-checking.ru.md) проверяют фильтры, сортировки и
`values()` по моделям.

## <a id="where-to-go-next"></a>Что дальше

- **[Установка](getting-started/installation.ru.md)** и **[Первая
  модель](getting-started/first-model.ru.md)** — от пустого проекта до базы с применёнными миграциями.
- Разделы навигации описывают каждое поле, опцию `Meta`, метод `QuerySet`, операцию миграции и
  возможность базы по коду: что делает код, то и сказано на странице.
