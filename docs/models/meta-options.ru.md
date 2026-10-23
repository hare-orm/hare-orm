# Опции Meta

Всё, что относится к таблице модели целиком, а не к отдельному полю, задаётся во вложенном классе `Meta`:

```python
class Foo(Model):
    ...
    class Meta:
        table = "custom_table"
        constraints = [UniqueConstraint(fields=("field_a", "field_b"))]
```

| Опция | Что задаёт |
|---|---|
| `abstract` | Таблицы нет; поля и `Meta` переходят к конкретным подклассам — см. [`abstract`](#abstract). |
| `table` | Имя таблицы — см. [`table`](#table). |
| `primary_key` | `None` для таблицы без первичного ключа — см. [Модели без первичного ключа](#primary_key). |
| `managed` | `False` оставляет таблицу не миграциям — см. [`managed`](#managed). |
| `schema` | Схема таблицы — подключённая база в SQLite, база сервера в ClickHouse — см. [`schema`](#schema). |
| `app` | Метка приложения для ссылок `"app.Model"` — см. [`app`](#app). |
| `swappable` | Настройка, через которую проект заменяет модель, — см. [Заменяемые модели](relations.ru.md#swappable-models). |
| `extensions` | Расширения PostgreSQL, нужные модели, — см. [Расширения](../migrations/operations.ru.md#extensions). |
| `constraints` | Ограничения уникальности, проверки и исключения — см. [Ограничения](constraints-and-triggers.ru.md#constraints). |
| `triggers` | Триггеры таблицы — см. [Триггеры](constraints-and-triggers.ru.md#triggers). |
| `views` | Представления из SQL или queryset — см. [Представления](schema-objects.ru.md#views). |
| `materialized_views` | Материализованные представления — см. [Материализованные представления](schema-objects.ru.md#materialized-views). |
| `dictionaries` | Словари строк модели, которые читаются по ключу (ClickHouse), — см. [Словари](../dialects/clickhouse/schema-objects.ru.md#dictionaries). |
| `functions` | Функции, хранимые в базе, — см. [Функции](schema-objects.ru.md#functions). |
| `sequences` | Последовательности, создаются до таблицы, — см. [Последовательности](schema-objects.ru.md#sequences). |
| `row_level_security` | Построчная защита таблицы, включённая или принудительная, — см. [Защита на уровне строк](schema-objects.ru.md#row-level-security). |
| `policies` | Политики построчной защиты таблицы — см. [Защита на уровне строк](schema-objects.ru.md#row-level-security). |
| `grants` | Права на таблицу и другие объекты модели — см. [Права](schema-objects.ru.md#grants). |
| `indexes` | Индексы таблицы — см. [Индексы](indexes.ru.md). |
| `ordering` | Порядок запросов модели по умолчанию — см. [`ordering`](#ordering). |
| `get_latest_by` | По чему сортируют `latest()` и `earliest()` — см. [`get_latest_by`](#get-latest-by). |
| `soft_delete_field` | `DatetimeField`, отмечающий мягко удалённую строку, — см. [Мягкое удаление](../soft-delete-versions-tenants/soft-delete.ru.md). |
| `soft_delete_hard_cascade` | `True` по-настоящему удаляет строки, до которых доходит мягкое удаление, — см. [`soft_delete_hard_cascade`](#soft-delete-hard-cascade). |
| `optimistic_lock_field` | Версия, которую проверяет и увеличивает каждая запись экземпляра, — см. [Оптимистичная блокировка](model-methods.ru.md#optimistic-locking). |
| `tenant_field` | Колонка с арендатором строки — см. [`tenant_field`](#tenant-field). |
| `tenant_schema` | `True` держит таблицу в схеме каждого арендатора — см. [Схема на арендатора](../soft-delete-versions-tenants/schema-per-tenant.ru.md). |
| `track_dirty_fields` | `True` хранит снимок каждой строки, с которым сравнивает `get_dirty_fields()`, — см. [Отслеживание изменений](model-methods.ru.md#tracking-changes). |
| `change_capture` | Каждая запись модели пишет и изменённые строки в outbox — см. [Захват изменений модели](../integrations/outbox.ru.md#change-capture). |
| `returning` | Значение `returning` у `bulk_create()` и `bulk_update()` по умолчанию — см. [`returning`](#returning). |
| `table_description` | Комментарий таблицы — см. [`table_description`](#table-description). |
| `table_options` | Как каждый диалект хранит таблицу — см. [Параметры хранения таблицы](#table_options). |
| `manager` | Менеджер `Model.objects` — см. [`manager`](#manager). |

## <a id="abstract"></a>`abstract`

`True`: таблица для модели не создаётся; поля и `Meta` наследуются конкретными подклассами, и каждый
получает свою глубокую копию, а не общий объект.

Конкретная модель, унаследованная от другой конкретной, — самостоятельная модель: у неё своя таблица с
копиями полей и нет опций `Meta` другой модели. Связь, которой другие модели ссылаются на ту модель, не
принадлежит подклассу — его строк нет в той таблице, — поэтому чтение её у подкласса (`child.refs`) даёт
`AttributeError`.

## <a id="table"></a>`table`

Имя таблицы в базе (по умолчанию выводится из имени модели). PostgreSQL обрезает имя до 63 байт, поэтому
сгенерированное имя таблицы, промежуточной таблицы связи «многие-ко-многим» или её колонки, которое
длиннее, укорачивается и получает в конце хэш — два длинных имени никогда не совпадут. Явно заданное имя
таблицы, колонки (`source_field` или имя поля), промежуточной таблицы, индекса или ограничения длиннее 63
байт даёт `ConfigurationError` на любом диалекте. Имя с точкой тоже даёт `ConfigurationError` — схему
задаёт `Meta.schema`.

## <a id="managed"></a>`managed`

`bool`, по умолчанию `True`. При `False` команды `makemigrations`/`migrate`, `Hare.generate_schemas()`,
`hare drift` и `truncate_all_models()` полностью пропускают таблицу модели — она не создаётся, не
изменяется и не считается расхождением со схемой, как будто для миграций её не существует. Подходит для
модели поверх того, чем миграции управлять не должны (представление, таблица, схема которой читается во
время работы) — см. [`Hare.register_live_models()`](runtime-models.ru.md). Смена `managed` у существующей
модели в любую сторону меняет только её опции в состоянии миграций — таблица из-за этого не удаляется и
не создаётся (см. [Операции](../migrations/operations.ru.md)).

## <a id="schema"></a>`schema`

Схема, в которой лежит таблица: схема в PostgreSQL, подключённая база в SQLite (её подключает соединение —
см. [`attached_databases`](../dialects/sqlite/connecting.ru.md#attached-databases)), база сервера в
ClickHouse. Две модели могут использовать одно имя `table` в разных схемах — `Hare.generate_schemas()`
упорядочивает таблицы по схеме и имени. Миграции создают и удаляют схему там, где у базы есть
`CREATE SCHEMA` (`Features.supports_schemas`, PostgreSQL); в остальных база должна существовать. В SQLite
связь с `db_constraint=True` на модель другой схемы отклоняется при привязке модели — внешний ключ там
ссылается только на таблицу своей базы (`Features.references_other_schemas`).

## <a id="app"></a>`app`

Метка приложения для ссылок строкой вида `"app.Model"`.

## <a id="ordering"></a>`ordering`

Кортеж строк сортировки, например `("-created_at", "name")`, или `F("field").asc()`/`.desc()`, или
выражения (`Lower("name").desc(nulls_last=True)`, `F("price") * F("quantity")`) — порядок `.order_by()`
по умолчанию, в том числе для `first()`/`last()`/`reverse()`, `iterator()`, среза, связанного менеджера и
`prefetch_related()`, а также запроса с агрегатом; запрос `.values()`, сгруппированный по названным
полям, его не применяет ([Неявный `GROUP BY`](../querying/aggregation.ru.md#implicit-group-by));
`.order_by()` без аргументов его отменяет.

## <a id="get-latest-by"></a>`get_latest_by`

Имя поля или последовательность имён (`"-created_at"` — по убыванию), например `("created_at", "id")`, —
то, по чему сортируют `latest()`/`earliest()`, если поля не переданы. Иное значение или имя, которого нет
среди полей модели, даёт `ConfigurationError` при объявлении модели.

## <a id="soft-delete-hard-cascade"></a>`soft_delete_hard_cascade`

`bool`, по умолчанию `False`. Требует `soft_delete_field`. `True` заставляет мягкое удаление удалять
по-настоящему каскадные строки моделей без `soft_delete_field` и строки автоматических промежуточных
таблиц M2M, как при обычном удалении; по умолчанию они сохраняются, и `restore()` возвращает связи, — см.
[Мягкое удаление](../soft-delete-versions-tenants/soft-delete.ru.md).

## <a id="tenant-field"></a>`tenant_field`

Имя колонки, в которой хранится арендатор строки, — см. [Разделение данных по
арендаторам](../soft-delete-versions-tenants/multi-tenancy.ru.md). Имя прямой связи
`ForeignKeyField`/`OneToOneField` означает её колонку ключа (`company` → `company_id`); связь с составным
ключом или поле без своей колонки дают `ConfigurationError`.

## <a id="returning"></a>`returning`

`bool`, по умолчанию `False` — значение параметра `returning` у `bulk_create()`/`bulk_update()`, если в
вызове он оставлен `None`; явные `True`/`False` в вызове всегда важнее — см.
[`bulk_create()`](../querying/queryset-methods.ru.md#bulk-create).

## <a id="table-description"></a>`table_description`

Описание таблицы, которое пишется её комментарием; если не задано, берётся первая строка docstring
модели. SQLite хранит его комментарием внутри скобок `CREATE TABLE` — миграция, которая его меняет,
пересоздаёт таблицу.

## <a id="manager"></a>`manager`

Объект `Manager()`, который задаёт запрос по умолчанию (например, автоматические фильтры). Каждый
конкретный подкласс получает свою копию. Всё, что фильтрует его `get_queryset()`, применяется и к каждому
соединению с этой моделью (`select_related()`, `.only()`, `.order_by()`, вложенный `.filter()`), поэтому
присоединённая строка отбирается так же, как в `Model.objects.all()`. Там поддерживаются только обычные
условия `.filter()`/`.exclude()` по собственным полям модели (вычисляемые значения, `LIMIT` или фильтр
через ещё одну связь дают `ConfigurationError`). Его фильтр не применяют `refresh_from_db()`, а также
обработка `CASCADE`/`SET_NULL`/`SET_DEFAULT` и проверки `PROTECT`/`RESTRICT` в
`delete()`/`delete_preview()`: они видят все существующие строки (и все строки, которые действительно
ссылаются на удаляемые), учитывая только `Meta.tenant_field`/`Meta.soft_delete_field`. `Manager`,
присвоенный атрибутом класса (например, `all_objects = Manager()`) модели, абстрактной базовой модели или
обычного класса-примеси, копируется вместе с аргументами конструктора и привязывается к каждой конкретной
модели.

## <a id="extension-options"></a>Опции расширений

Расширения hare тоже читают из `Meta` свои опции: `outbox_wakeup` у [модели очереди исходящих
событий](../integrations/outbox.ru.md#outboxevent), `new_version_excluded_fields` у [версионной
модели](../soft-delete-versions-tenants/versioned-models.ru.md). Они описывают работу расширения, а не
таблицу, поэтому миграции их не записывают. Расширение объявляет опции, которые читает, вызовом
`ExtensionModelOptions.allow("name", ...)` (`hare.models.class_building.extension_model_options`) при
импорте.

## <a id="refused-options"></a>Что отклоняет `Meta`

Любой другой атрибут `Meta` — в том числе унаследованный от базового класса `Meta` — даёт
`ConfigurationError` при объявлении класса с названием ближайшей опции
(`Meta.ordring isn't a model option - did you mean Meta.ordering?`): иначе опечатка в опции молча ничего
бы не делала. Для `db_table`, `app_label` и `db_table_comment` ошибка называет опцию hare, которую нужно
указать (`table`, `app`, `table_description`).

Опция, которая что-то называет, принимает непустой текст, а каждая запись `extensions` — имя
расширения; другое значение даёт `ConfigurationError`: оно попало бы в DDL.

`default_connection` — **не** опция `Meta`: подключение по умолчанию задаётся при регистрации приложения
ключом `apps.<name>.default_connection` в настройках `Hare.init()`, а не внутри `class Meta`; записанное
в `Meta` значение даёт `ConfigurationError` с этим пояснением.

## <a id="primary_key"></a>Модели без первичного ключа

У журнала, в который только добавляют записи, у потока событий или у таблицы колоночного хранилища
первичного ключа часто нет. `Meta.primary_key = None` объявляет такую модель: поле `id` не добавляется, и
таблица создаётся без `PRIMARY KEY`:

```python
class ReadingLog(Model):
    book = fields.ForeignKeyField("models.Book", related_name="readings")
    reader = fields.CharField(max_length=50)
    read_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        primary_key = None
```

Что работает: `create()`/`save()` новой строки, `bulk_create()`, любое чтение (`filter()`, `exclude()` —
в том числе через связи, `values()`, `aggregate()`, `annotate()`, `group_by()`, `select_related()`,
`prefetch_related()` прямых связей, `iterator()`, `get()`, `first()`), `QuerySet.update()` и
`QuerySet.delete()` по любому условию, а также доступ к строкам со стороны связанной модели
(`book.readings.all()`, `Book.objects.filter(readings__reader=...)`, `Count("readings")`,
`readings__isnull=True`, `prefetch_related("readings")`). Условие через связь в `exclude()`, `update()`
или `delete()` находит записанную строку по всем её колонкам, причём `NULL` совпадает с `NULL`: строки,
равные во всех колонках, всё равно неразличимы.

То, для чего нужен первичный ключ, даёт `ConfigurationError` с именем модели: `save()` прочитанной строки
(это `UPDATE`), `delete()`/`restore()`/`refresh_from_db()` одного объекта, `ForeignKeyField`/`OneToOneField`
на эту модель без `to_field=` и
`ManyToManyField` в ней или на неё. `pk` в фильтре или сортировке — `FieldError`; `instance.pk` равен
`None`, а объект равен только самому себе. Через связь со стороны другой модели связанные строки целиком
сравнивают только `isnull`/`not_isnull` (`readings__isnull=True`); `readings=` и `readings__in=`
указывают на строку по её ключу и дают `FieldError`.

Миграции создают и изменяют такую таблицу, как любую другую (в состоянии хранится `primary_key: None`);
чтобы добавить модели первичный ключ или убрать его, нужна миграция, написанная вручную, — как и при
любом изменении первичного ключа. `inspectdb` превращает в такую модель таблицу без первичного ключа;
уникальный индекс таблицы остаётся уникальным индексом.

## <a id="table_options"></a>Параметры хранения таблицы

`Meta.table_options` перечисляет, как каждый диалект хранит таблицу модели, — по одной записи на диалект.
Подключение берёт запись своего диалекта и не смотрит на остальные, поэтому одна модель работает на любой
базе, к которой её направят:

```python
from hare.dialects.postgresql.postgresql_table_options import PostgresqlTableOptions
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.sqlite.sqlite_table_options import SqliteTableOptions


class Book(Model):
    isbn = fields.CharField(max_length=13, primary_key=True)
    copies = fields.IntField()

    class Meta:
        table_options = [
            SqliteTableOptions(without_rowid=True),
            PostgresqlTableOptions(unlogged=True, storage_parameters={"fillfactor": 70}),
            ClickhouseTableOptions(engine="ReplacingMergeTree", order_by=("isbn",)),
        ]
```

| Класс | Параметр | В `CREATE TABLE` |
|---|---|---|
| `SqliteTableOptions` | `without_rowid` | `WITHOUT ROWID` — строки хранятся прямо в индексе первичного ключа; нужен первичный ключ, который база не генерирует сама. |
|  | `strict` | `STRICT` (SQLite 3.37+) — колонка хранит значения только своего типа — см. [`strict`](#strict). |
| `PostgresqlTableOptions` | `unlogged` | `CREATE UNLOGGED TABLE` — без журнала предзаписи (WAL): запись быстрее, но после сбоя таблица очищается и не реплицируется. |
| | `storage_parameters` | `WITH (...)` — `{"fillfactor": 70, "autovacuum_enabled": False}`; значение — число, `True`/`False` или строка. |
| | `tablespace` | `TABLESPACE name`; `None` — табличное пространство базы по умолчанию. |
| | `partitioning` | `PARTITION BY ...` и по таблице на партицию — см. [Партиционированные таблицы](../dialects/postgresql/indexes-and-tables.ru.md#partitioning). |
| `ClickhouseTableOptions` | `engine` | `ENGINE = ...` — по умолчанию `"MergeTree"`, любой движок со своими аргументами (`"ReplacingMergeTree(version)"`). |
| | `order_by` | `ORDER BY (...)` — поля, по которым упорядочены строки, и [выражения](../dialects/clickhouse/models.ru.md#expressions) hare или `RawSQLTerm`; пусто — по первичному ключу (`tuple()`, если его нет). Первичный ключ должен быть его началом. |
| | `partition_by` | `PARTITION BY ...` — выражение (`Trunc("viewed_at", "month")`); `None` — одна партиция. |
| | `ttl` | `TTL ...` — выражение момента, когда строка удаляется (`F("viewed_at") + timedelta(days=365)`), или `RawSQLTerm` всего предложения `TTL` таблицы; `None` — без него. |
| | `settings` | `SETTINGS name = value, ...` — пары из имени и значения. |
| | `sample_by`, `column_codecs`, `column_ttls`, `projections`, `distributed_over`, `sharding_key`, `lightweight_updates` | См. [Параметры таблицы ClickHouse](../dialects/clickhouse/models.ru.md#table-options). |

Две записи для одного диалекта или запись, которая не является `TableOptions`, дают `ConfigurationError`.
Пакет диалекта объявляет свой класс параметров (см. [Как написать
диалект](../extending/writing-a-dialect/schema.ru.md#table-options)).

Миграции записывают эти записи в файлы миграций; изменение записи для диалекта подключения — это операция
`AlterModelOptions`. PostgreSQL меняет таблицу на месте (`SET TABLESPACE`, `SET LOGGED`/`SET UNLOGGED`,
`SET (...)`/`RESET (...)`), SQLite пересоздаёт её. `hare drift` сравнивает хранение таблицы с тем,
которое даёт таблице запись диалекта подключения, а `inspectdb` записывает параметры таблицы в
`Meta.table_options`.

### <a id="strict"></a>`strict` (SQLite)

`STRICT` (SQLite 3.37+) — колонка хранит значения только своего типа, неверное значение отвергает база;
колонка другого типа создаётся с типом `STRICT` своей аффинности (`INTEGER`, `REAL`, `TEXT`, `BLOB`,
`ANY`). Более старая SQLite даёт `UnSupportedError` до DDL; смена опции перестраивает таблицу.
