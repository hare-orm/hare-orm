# Опции Meta

Всё, что относится к таблице модели целиком, а не к отдельному полю, задаётся во вложенном классе
`Meta`:

```python
class Foo(Model):
    ...
    class Meta:
        table = "custom_table"
        constraints = [UniqueConstraint(fields=("field_a", "field_b"))]
```

| Опция | Что задаёт |
|---|---|
| `abstract` | Таблица для этой модели не создаётся; поля и `Meta` наследуются конкретными подклассами (каждый получает свою глубокую копию, а не общий объект). |
| `table` | Имя таблицы в базе (по умолчанию выводится из имени модели). PostgreSQL обрезает имя до 63 байт, поэтому сгенерированное имя таблицы, промежуточной таблицы связи «многие-ко-многим» или её колонки, которое длиннее, укорачивается и получает в конце хэш — два длинных имени никогда не совпадут. Явно заданное имя таблицы, колонки (`source_field` или имя поля), промежуточной таблицы, индекса или ограничения длиннее 63 байт даёт `ConfigurationError` на любом диалекте. |
| `primary_key` | Только `None`: у таблицы нет первичного ключа, поле `id` не добавляется — см. [Модели без первичного ключа](#primary_key). Первичный ключ объявляется через `primary_key=True` у поля или через `CompositePrimaryKey`. |
| `managed` | `bool`, по умолчанию `True`. При `False` команды `makemigrations`/`migrate`, `Hare.generate_schemas()`, `hare drift` и `truncate_all_models()` полностью пропускают таблицу модели — она не создаётся, не изменяется и не считается расхождением со схемой, как будто для миграций её не существует. Подходит для модели поверх того, чем миграции управлять не должны (представление, таблица, схема которой читается во время работы) — см. [`Hare.register_live_models()`](runtime-models.ru.md). Смена `managed` у существующей модели в любую сторону меняет только её опции в состоянии миграций — таблица из-за этого не удаляется и не создаётся (см. [Операции](../migrations/operations.ru.md)). |
| `schema` | Схема базы (PostgreSQL). Две модели могут использовать одно имя `table` в разных схемах — `Hare.generate_schemas()` упорядочивает таблицы по схеме и имени. В SQLite схем нет — там таблица создаётся и читается без имени схемы. |
| `app` | Метка приложения для ссылок строкой вида `"app.Model"`. |
| `swappable` | Имя настройки `swappable` в конфигурации (например, `"USER_MODEL"`), через которую проект может заменить эту модель другой — см. [Заменяемые модели](relations.ru.md#swappable-models). Наследуется от абстрактной базовой модели, как и остальные ключи. |
| `extensions` | Кортеж имён расширений PostgreSQL, нужных модели (например, `("pg_trgm",)`) — см. [Операции миграций — расширения](../migrations/operations.ru.md#extensions). Тип поля, которому расширение нужно всегда (`CitextField`, `PostGISField`), объявляет его сам — повторять здесь не нужно. |
| `constraints` | Кортеж ограничений из `hare.ddl.constraints`: `UniqueConstraint`, `CheckConstraint`, `ExclusionConstraint`. Ограничение `UNIQUE` сразу по нескольким колонкам — это `UniqueConstraint(fields=("field_a", "field_b"))`: имя необязательно (оно генерируется), а `ManyToManyField` в него входить не может. |
| `triggers` | Кортеж объектов `hare.ddl.triggers.Trigger`; их создают и `migrate`, и `Hare.generate_schemas()`. |
| `indexes` | Кортеж объектов `hare.ddl.indexes.Index`/`PartialIndex` или просто кортежей имён полей. |
| `ordering` | Кортеж строк сортировки, например `("-created_at", "name")` — порядок `.order_by()` по умолчанию; `.order_by()` без аргументов его отменяет. |
| `soft_delete_field` | Имя `DatetimeField`, допускающего `NULL`, — см. [Мягкое удаление](../soft-delete-versions-tenants/soft-delete.ru.md). |
| `soft_delete_hard_cascade` | `bool`, по умолчанию `False`. Требует `soft_delete_field`. `True` заставляет мягкое удаление удалять по-настоящему каскадные строки моделей без `soft_delete_field` и строки автоматических промежуточных таблиц M2M, как при обычном удалении; по умолчанию они сохраняются, и `restore()` возвращает связи, — см. [Мягкое удаление](../soft-delete-versions-tenants/soft-delete.ru.md). |
| `optimistic_lock_field` | Имя `IntField` без `NULL` для оптимистической блокировки; увеличивается при каждом `save()`. Не может одновременно быть частью первичного ключа или генерироваться базой. |
| `tenant_field` | Имя колонки, в которой хранится арендатор строки, — см. [Разделение данных по арендаторам](../soft-delete-versions-tenants/multi-tenancy.ru.md). Имя прямой связи `ForeignKeyField`/`OneToOneField` означает её колонку ключа (`company` → `company_id`); связь с составным ключом или поле без своей колонки дают `ConfigurationError`. |
| `track_dirty_fields` | `bool`, по умолчанию `False` — включает для каждой строки снимок значений, на котором работает `get_dirty_fields()`. |
| `returning` | `bool`, по умолчанию `False` — значение параметра `returning` у `bulk_create()`/`bulk_update()`, если в вызове он оставлен `None`; явные `True`/`False` в вызове всегда важнее. См. [Методы QuerySet](../querying/queryset-methods.ru.md#method-reference). |
| `table_description` | Описание (комментарий) таблицы; если не задано, берётся первая строка docstring модели. |
| `table_options` | Как каждый диалект хранит таблицу — `SqliteTableOptions`, `PostgresqlTableOptions`, по одной записи на диалект; см. [Параметры хранения таблицы](#table_options). |
| `manager` | Объект `Manager()`, который задаёт запрос по умолчанию (например, автоматические фильтры). Каждый конкретный подкласс получает свою копию. Всё, что фильтрует его `get_queryset()`, применяется и к каждому соединению с этой моделью (`select_related()`, `.only()`, `.order_by()`, вложенный `.filter()`), поэтому присоединённая строка отбирается так же, как в `Model.objects.all()`. Там поддерживаются только обычные условия `.filter()`/`.exclude()` по собственным полям модели (вычисляемые значения, `LIMIT` или фильтр через ещё одну связь дают `ConfigurationError`). Его фильтр не применяют `refresh_from_db()`, а также обработка `CASCADE`/`SET_NULL`/`SET_DEFAULT` и проверки `PROTECT`/`RESTRICT` в `delete()`/`delete_preview()`: они видят все существующие строки (и все строки, которые действительно ссылаются на удаляемые), учитывая только `Meta.tenant_field`/`Meta.soft_delete_field`. `Manager`, присвоенный атрибутом класса (например, `all_objects = Manager()`) модели, абстрактной базовой модели или обычного класса-примеси, копируется вместе с аргументами конструктора и привязывается к каждой конкретной модели. |

`default_connection` — **не** опция `Meta`: подключение по умолчанию задаётся при регистрации
приложения ключом `apps.<name>.default_connection` в настройках `Hare.init()`, а не внутри
`class Meta`.

## Модели без первичного ключа {: #primary_key }

У журнала, в который только добавляют записи, у потока событий или у таблицы колоночного хранилища
первичного ключа часто нет. `Meta.primary_key = None` объявляет такую модель: поле `id` не
добавляется, и таблица создаётся без `PRIMARY KEY`:

```python
class VisitLog(Model):
    venue = fields.ForeignKeyField("models.Venue", related_name="visits")
    visitor = fields.CharField(max_length=50)
    visited_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        primary_key = None
```

Что работает: `create()`/`save()` новой строки, `bulk_create()`, любое чтение (`filter()`,
`exclude()` — в том числе через связи, `values()`, `aggregate()`, `annotate()`, `group_by()`,
`select_related()`, `prefetch_related()` прямых связей, `iterator()`, `get()`, `first()`),
`QuerySet.update()` и `QuerySet.delete()` по любому условию, а также доступ к строкам со стороны
связанной модели (`venue.visits.all()`, `Venue.objects.filter(visits__visitor=...)`, `Count("visits")`,
`visits__isnull=True`, `prefetch_related("visits")`). Условие через связь в `exclude()`, `update()`
или `delete()` находит записанную строку по всем её колонкам, причём `NULL` совпадает с `NULL`:
строки, равные во всех колонках, всё равно неразличимы.

То, для чего нужен первичный ключ, даёт `ConfigurationError` с именем модели: `save()` прочитанной
строки (это `UPDATE`), `delete()`/`restore()`/`refresh_from_db()` одного объекта,
`update()`/`delete()` запроса со срезом или сортировкой, `ForeignKeyField`/`OneToOneField` на
эту модель без `to_field=` и `ManyToManyField` в ней или на неё. `pk` в фильтре или сортировке —
`FieldError`; `instance.pk` равен `None`, а объект равен только самому себе. Через связь со стороны
другой модели связанные строки целиком сравнивают только `isnull`/`not_isnull`
(`visits__isnull=True`); `visits=` и `visits__in=` указывают на строку по её ключу и дают
`FieldError`.

Миграции создают и изменяют такую таблицу, как любую другую (в состоянии хранится
`primary_key: None`); чтобы добавить модели первичный ключ или убрать его, нужна миграция,
написанная вручную, — как и при любом изменении первичного ключа. `inspectdb` превращает в такую модель
таблицу, у которой нет ни первичного ключа, ни уникального индекса по всем колонкам.

## Параметры хранения таблицы {: #table_options }

`Meta.table_options` перечисляет, как каждый диалект хранит таблицу модели, — по одной записи на
диалект. Подключение берёт запись своего диалекта и не смотрит на остальные, поэтому одна модель
работает на любой базе, к которой её направят:

```python
from hare.dialects.postgresql.table_options import PostgresqlTableOptions
from hare.dialects.sqlite.table_options import SqliteTableOptions


class Measurement(Model):
    sensor = fields.CharField(max_length=40, primary_key=True)
    value = fields.FloatField()

    class Meta:
        table_options = [
            SqliteTableOptions(without_rowid=True),
            PostgresqlTableOptions(unlogged=True, storage_parameters={"fillfactor": 70}),
        ]
```

| Класс | Параметр | В `CREATE TABLE` |
|---|---|---|
| `SqliteTableOptions` | `without_rowid` | `WITHOUT ROWID` — строки хранятся прямо в индексе первичного ключа; нужен первичный ключ, который база не генерирует сама. |
| `PostgresqlTableOptions` | `unlogged` | `CREATE UNLOGGED TABLE` — без журнала предзаписи (WAL): запись быстрее, но после сбоя таблица очищается и не реплицируется. |
| | `storage_parameters` | `WITH (...)` — `{"fillfactor": 70, "autovacuum_enabled": False}`; значение — число, `True`/`False` или строка. |
| | `tablespace` | `TABLESPACE имя`; `None` — табличное пространство базы по умолчанию. |
| | `partitioning` | `PARTITION BY ...` и по таблице на партицию — см. [Партиционированные таблицы](#partitioning). |

Две записи для одного диалекта или запись, которая не является `TableOptions`, дают
`ConfigurationError`. Пакет диалекта объявляет свой класс параметров (см.
[Как написать диалект](../extending/writing-a-dialect.ru.md#table-options)).

Миграции записывают эти записи в файлы миграций; изменение записи для диалекта подключения — это
операция `AlterModelOptions`. PostgreSQL меняет таблицу на месте (`SET TABLESPACE`,
`SET LOGGED`/`SET UNLOGGED`, `SET (...)`/`RESET (...)`), SQLite пересоздаёт её. `hare drift`
сравнивает запись диалекта подключения с тем, с какими параметрами таблица создана (табличное
пространство, названное явно, но совпадающее с табличным пространством базы по умолчанию,
считается значением по умолчанию), а `inspectdb` записывает параметры таблицы в
`Meta.table_options`.

### Партиционированные таблицы (PostgreSQL) {: #partitioning }

`PostgresqlTableOptions(partitioning=...)` делает таблицу партиционированной: она создаётся с
`PARTITION BY <стратегия> (<колонки ключа>)`, а каждая партиция — отдельной таблицей с именем
`<таблица>_<имя партиции>` (в пределах 63 байт, как любое имя, которое генерирует hare). Классы
лежат в `hare.dialects.postgresql.partitioning`:

```python
import datetime

from hare import fields
from hare.dialects.postgresql.partitioning import (
    HashPartitioning, ListPartition, ListPartitioning, RangeBound, RangePartition, RangePartitioning,
)
from hare.dialects.postgresql.table_options import PostgresqlTableOptions
from hare.models import Model


class UnreadReport(Model):
    user_id = fields.BigIntField()
    report_id = fields.BigIntField()
    pk = fields.CompositePrimaryKey("user_id", "report_id")

    class Meta:
        table_options = [
            PostgresqlTableOptions(
                # unreadreport_p0 ... unreadreport_p31, FOR VALUES WITH (MODULUS 32, REMAINDER i)
                partitioning=HashPartitioning(fields=("user_id",), partition_count=32),
                storage_parameters={"autovacuum_vacuum_scale_factor": 0.02},
            )
        ]


class RegionSale(Model):
    region = fields.CharField(max_length=10)
    number = fields.IntField()
    pk = fields.CompositePrimaryKey("region", "number")

    class Meta:
        table_options = [
            PostgresqlTableOptions(
                partitioning=ListPartitioning(
                    fields=("region",),
                    partitions=[
                        ListPartition("west", values=["us", "ca"]),  # regionsale_west FOR VALUES IN ('us', 'ca')
                        ListPartition("east", values=["jp"]),
                    ],
                    default_partition="other",  # regionsale_other DEFAULT — все остальные регионы
                )
            )
        ]


class DailyEvent(Model):
    day = fields.DateField()
    number = fields.IntField()
    pk = fields.CompositePrimaryKey("day", "number")

    class Meta:
        table_options = [
            PostgresqlTableOptions(
                partitioning=RangePartitioning(
                    fields=("day",),
                    partitions=[
                        RangePartition("old", from_values=(RangeBound.MINVALUE,), to_values=(datetime.date(2026, 1, 1),)),
                        RangePartition(
                            "y2026", from_values=(datetime.date(2026, 1, 1),), to_values=(datetime.date(2027, 1, 1),)
                        ),
                    ],
                    default_partition="later",
                )
            )
        ]
```

| Класс | Аргументы | Партиции |
|---|---|---|
| `HashPartitioning` | `fields`, `partition_count` (от 1 до 1024) | `<таблица>_p0` ... `<таблица>_p<n-1>`, строки распределяются равномерно по хешу ключа. |
| `ListPartitioning` | `fields` (одно поле), `partitions` (`ListPartition(name, values)`), `default_partition` | Каждая партиция хранит строки с перечисленными значениями ключа; `None` среди них означает строки с ключом `NULL`. |
| `RangePartitioning` | `fields`, `partitions` (`RangePartition(name, from_values, to_values)`), `default_partition` | Каждая партиция хранит строки от нижней границы до верхней, не включая её, — по одному значению на колонку ключа. `RangeBound.MINVALUE`/`RangeBound.MAXVALUE` стоят ниже/выше любого значения. |

`fields` задаёт ключ: поля с колонкой или внешний ключ, который означает свои ключевые колонки.
`default_partition` — имя партиции для строк, которые не подходят ни одной другой; без неё строку,
которой не подходит ни одна партиция, база отвергает (`IntegrityError`). Порядок, в котором
перечислены партиции, не важен.

То, что требует PostgreSQL, проверяется до записи любого DDL — `ConfigurationError` называет
проблему:

- поля ключа существуют и имеют колонки;
- первичный ключ, каждое уникальное поле, `UniqueConstraint`, уникальный `Index` и
  `ExclusionConstraint` (с `=`) включают все колонки ключа — PostgreSQL проверяет каждое из них
  внутри одной партиции. Модель, чей ключ — один генерируемый `id`, можно партиционировать только
  по этому `id`;
- партиционированная таблица не может быть `unlogged`;
- имена партиций различаются, значение `ListPartitioning` перечислено один раз, диапазоны
  `RangePartition` не пересекаются и дают по одному значению на колонку ключа.

`ExclusionConstraint` на партиционированной таблице требует PostgreSQL 17 (на более старом сервере
— `UnSupportedError`). Внешние ключи на партиционированную таблицу и из неё работают как у обычной.

Сама партиционированная таблица строк не хранит, поэтому `storage_parameters` задаются на каждой
партиции (при создании и при изменении), а `tablespace` — на таблице (туда попадают новые
партиции) и на каждой партиции. Индексы и ограничения создаются на таблице и распространяются на
все партиции. Конкурентный индекс (`AddIndex(concurrently=True)`) нельзя построить на
партиционированной таблице целиком: hare создаёт его только на самой таблице, строит конкурентно
на каждой партиции и присоединяет каждый — он становится действительным, когда присоединён
последний; удаление индекса конкурентным не бывает.

Как изменение попадает в базу:

| Изменение | Миграция |
|---|---|
| Добавлена `ListPartition`/`RangePartition` или партиция по умолчанию | `AddPartition` — новая пустая партиция. PostgreSQL отказывает, пока в партиции по умолчанию лежат строки, которые относятся к новой. |
| Партиция убрана | `RemovePartition` — партиция отсоединяется и удаляется **вместе со строками**; `makemigrations` предупреждает. |
| Изменены значения или границы партиции | `RemovePartition` и `AddPartition` с тем же именем — её строки теряются. |
| `storage_parameters`, `tablespace` | `AlterModelOptions`, на месте на каждой партиции. |
| Партиционирование включено или снято, другая стратегия, ключ или `partition_count` | `AlterModelOptions`, который создаёт таблицу заново: строки копируются, индексы, ограничения, комментарии, триггеры и внешние ключи других таблиц, ссылающиеся на неё, ставятся снова. Вся таблица переписывается под исключительной блокировкой. |
| Изменена `Meta.table`, `RenameModel` | Партиции переименовываются вместе с таблицей. |

В SQLite (и в любом другом диалекте) запись `PostgresqlTableOptions` не используется: таблица
обычная, а операции над партициями меняют там только состояние миграций.
