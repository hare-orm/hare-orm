# hare-orm

Асинхронная ORM для Python, работающая с PostgreSQL, SQLite и ClickHouse, в которой связи между моделями — не
дополнение, а основа. `hare-orm` выросла из tortoise-orm и переписана почти целиком. По устройству
она близка к ORM Django, так что тем, кто работал с Django, всё будет знакомо: модели — обычные
классы, запрос собирается через `QuerySet` и выполняется только тогда, когда нужен результат, а
миграции создаются автоматически — сравнением текущих моделей с их прошлым состоянием.

Чем она отличается:

- **[Диалекты подключаются как модули](dialects/dialects-and-features.ru.md).** SQLite, PostgreSQL и
  [ClickHouse](dialects/clickhouse/connecting.ru.md) устроены как диалекты и используют тот же публичный API, что доступен стороннему пакету: текст SQL, типы
  колонок, команды создания таблиц, чтение схемы базы и возможности каждой версии сервера задаёт
  диалект. Если база не умеет транзакции, внешние ключи или ограничения уникальности, ORM сообщает
  об этом явной ошибкой, а не выдаёт молча неверный результат; модель может вообще не иметь
  первичного ключа. Подробнее — в руководстве [Как написать диалект](extending/writing-a-dialect.ru.md).
- **Драйвер PostgreSQL на Rust** (`hare.dialects.postgresql.drivers.rust_pg`) используется по
  умолчанию для адресов `postgresql://`: строки результата разбираются и значения преобразуются
  на Rust, а не на Python. Драйвер `asyncpg`, написанный на Python, подключается адресом
  `postgresql+asyncpg://`.
- **Составные первичные ключи работают так же полно, как обычные**, в том числе когда на них
  ссылается внешний ключ: настоящее ограничение `FOREIGN KEY` на уровне таблицы, каскадное
  удаление, соединение таблиц и `prefetch_related()`.
- **Оптимистическая блокировка, мягкое удаление и отслеживание изменённых полей** включаются
  опциями `Meta`, а не классами-примесями, которые приходится писать самому.
- **Триггеры и ограничения базы описываются в модели** (`Meta.triggers`, `Meta.constraints`) и
  действительно создаются операцией `CreateModel`, когда выполняется миграция.
- **[Собственные типы и индексы PostgreSQL](dialects/postgresql/fields.ru.md)** — `ArrayField`,
  `PostGISField`, `TSVectorField`, поля диапазонов и индексы GIN, GiST, BRIN, Bloom, Hash, SP-GiST,
  IVFFlat, HNSW.
- **[Полнотекстовый поиск](dialects/search-and-geodata/full-text-search.ru.md)** (`hare.search`) — `__search`, `SearchRank` и
  `SearchHeadline` с одним API в PostgreSQL (tsvector) и SQLite (FTS5).
- **[Поиск по векторам](dialects/search-and-geodata/vector-search.ru.md)** (`hare.vectors`) — `VectorField` и поиск похожих
  векторов с одним API в PostgreSQL (pgvector) и SQLite (sqlite-vec).
- **[Запросы из параметров HTTP-запроса](integrations/request-queries.ru.md)**
  (`hare.contrib.request_query`) — проверенный и разбитый на страницы запрос к базе, собранный из
  параметров запроса к API, с готовыми интеграциями для [Litestar](integrations/litestar.ru.md),
  [FastAPI](integrations/fastapi.ru.md) и [Robyn](integrations/robyn.ru.md).
- **[Очередь исходящих событий](integrations/outbox.ru.md)** (`hare.contrib.outbox`) — событие
  сохраняется в той же транзакции, что и изменение, о котором оно сообщает, — вручную или
  захватом каждой записи модели (`Meta.change_capture`), — а relay доставляет его как минимум один
  раз, по порядку для каждой строки, в Kafka, RabbitMQ, Redis, на webhook или в taskiq.
- **Встроенное [разделение данных по арендаторам](soft-delete-versions-tenants/multi-tenancy.ru.md)**
  (`Meta.tenant_field`) — каждый запрос через менеджер по умолчанию к модели, где это включено,
  сам ограничивается строками текущего арендатора, так же как `Meta.soft_delete_field` скрывает
  удалённые строки.
- **[Поиск повторяющихся запросов](observability/repeated-queries.ru.md)** во время
  работы приложения, а не только в тестах: он замечает проблему N+1 — один запрос за списком и
  затем ещё по запросу на каждый его элемент.

## <a id="quick-example"></a>Короткий пример

```python
from hare import fields
from hare.models import Model


class Author(Model):
    id = fields.UUIDField(primary_key=True)
    name = fields.CharField(max_length=120)
    email = fields.CharField(max_length=254, unique=True, null=True)

    class Meta:
        table_description = "Авторы книг"


async def main() -> None:
    author = await Author.objects.create(name="Урсула Ле Гуин")
    async for a in Author.objects.filter(name__icontains="ле гуин"):
        print(a.name)
```

## <a id="where-to-go-next"></a>Что читать дальше

- **[Начало работы](getting-started/installation.ru.md)** — установка hare-orm, вызов
  `Hare.init()`, первая модель.
- **[Работа с hare-orm](models/field-types.ru.md)** — полное и точное описание API: поля,
  опции `Meta`, `QuerySet`, миграции, транзакции, диалекты и их возможности, типы и индексы
  PostgreSQL, версии записей, мягкое удаление и все исключения.

Каждая страница справочника написана по исходному коду: сигнатуры скопированы из него, а не
пересказаны. Если документация и код расходятся, прав код — пожалуйста, сообщите об этом в issue.
