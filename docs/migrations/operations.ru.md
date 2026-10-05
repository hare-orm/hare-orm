# Операции

Операции, которые перечисляет файл миграции, из `hare.migrations.operations` (`from hare.migrations import
operations as ops`): что принимает каждая, что делает в базе и в состоянии миграций и как написать
свою.

## <a id="model-operations"></a>Модели

```python
CreateModel(name: str, fields: list[tuple[str, FieldLike]], options: dict | None = None, bases: list[str] | None = None, state_only: bool = False)
RenameModel(old_name: str, new_name: str)
DeleteModel(name: str, state_only: bool = False)
AlterModelOptions(name: str, options: dict)
AlterModelTable(name: str, table: str)
```

## <a id="partition-operations"></a>Партиции

```python
AddPartition(model_name: str, partition: Any)
RemovePartition(model_name: str, partition: Any)
```

## <a id="field-operations"></a>Поля

```python
AddField(model_name: str, name: str, field: FieldLike, *, not_valid: bool = False)
RemoveField(model_name: str, name: str)
AlterField(model_name: str, name: str, field: FieldLike)
RenameField(model_name: str, old_name: str, new_name: str, field: FieldLike | None = None)
BackfillColumn(model_name: str, field_name: str, value: Any | Callable[[], Any], *, batch_size: int = 1000)
AlterColumnNotNullSafe(model_name: str, field_name: str)
```

`field` у `RenameField` — поле под новым именем, если переименование меняет и его; без него поле
остаётся прежним.

`AddField(..., not_valid=True)` поля `ForeignKeyField`/`OneToOneField` с ограничением в базе добавляет
колонку, затем её внешний ключ `NOT VALID` (PostgreSQL) под тем именем, которое получил бы встроенный
`REFERENCES`: ему должны соответствовать только новые и изменённые строки, поэтому добавление не
сканирует таблицу, пока запись в обе таблицы ждёт; позже `ValidateConstraint(model_name, <имя внешнего
ключа>)` проверяет существующие строки с блокировкой, которая не мешает записи. Другое поле или связь с
`db_constraint=False` дают `ConfigurationError`. SQLite никогда не проверяет существующие строки при
добавлении колонки, поэтому там параметр ничего не меняет.

`RemoveField` на SQLite удаляет колонку командой `ALTER TABLE ... DROP COLUMN` (SQLite 3.35.5 и новее,
`features.supports_drop_column`), если SQLite ничто этого не запрещает: поле — не ключ, не уникальное,
без индекса, не связь и не генерируемое; у модели нет CHECK-ограничений, генерируемых колонок и
триггеров, и ни другой индекс, ни уникальное ограничение не называют это поле; в самой базе нет индекса
по колонке (а также частичного индекса или индекса по выражению на таблице), CHECK в таблице, триггера
на ней и представления, где встречается колонка. Иначе таблица
пересобирается по модели, которую знают миграции: создаётся заново, строки копируются, индексы,
уникальные ограничения и триггеры создаются снова. Пересборка сохраняет только колонки из состояния
миграций, поэтому колонку, которой в состоянии уже нет (убранную через
[`SeparateDatabaseAndState`](#separate-database-and-state)), удалит следующая пересборка её таблицы;
индекс, триггер или представление, созданные через `RunSQL`, тоже не создаются заново.

`AddField` поля `NOT NULL`, у которого значение по умолчанию есть только на стороне Python
(`default=`, без `db_default`), работает и на таблице, где уже есть строки: колонка добавляется с
`NULL`, каждой существующей строке записывается значение по умолчанию, затем колонка становится
`NOT NULL` (`ALTER COLUMN ... SET NOT NULL` в PostgreSQL, пересоздание таблицы в SQLite). Функция по
умолчанию вычисляется **один раз**, поэтому все существующие строки получают одно и то же значение —
для значения, своего у каждой строки (например, `uuid4` в уникальной колонке), добавьте колонку с
`NULL`, заполните её шагом `RunPython`, затем сделайте `NOT NULL`. `DatetimeField`/`TimeField`
`NOT NULL` с `auto_now=True`/`auto_now_add=True` (и без `default`/`db_default`) заполняется так же —
текущим временем, записанным точно так, как его записывает обычный `save()`.

`AlterField`, меняющий тип колонки первичного ключа (например, `IntField` на `BigIntField`) или другой
колонки, на которую ссылается связь через `to_field=`, меняет и ссылающиеся на неё колонки ключей:
каждую колонку `ForeignKeyField`/`OneToOneField` и каждую колонку автоматической промежуточной
таблицы `ManyToManyField`. В PostgreSQL их ограничения внешних ключей сначала удаляются и создаются
заново, когда у всех колонок уже новый тип; в SQLite ссылающиеся таблицы пересоздаются. Значения
преобразуются простым приведением типа, поэтому пара типов, которую PostgreSQL привести не может
(`integer` в `uuid`), по-прежнему даёт ошибку.

`AlterField` на тип колонки, в который не помещается уже хранящееся в таблице значение, на любом
диалекте вызывает `FieldNarrowingDataLossError` до каких-либо изменений. Это меньший `max_length`
(или текст, число либо логическое значение, превращённые в `CharField`, слишком короткий для их
текста), `DecimalField` с меньшим числом знаков после запятой или целых разрядов, меньший
целочисленный тип, а также дробное число или decimal, превращённые в целое или в decimal, куда
значение не помещается. Иначе PostgreSQL обрезал бы или округлил значение при приведении типа, а
SQLite хранил бы его сверх объявленного размера колонки. Сначала исправьте или расширьте такие
значения; на более узкий тип, в который помещается каждое хранимое значение, колонка меняется как
обычно.

`AlterField`, меняющий `to_field=` связи, направляет её колонку ключа (и ограничение внешнего ключа) на
новую целевую колонку. Сохранённые значения ключа указывают на строки через старую целевую колонку и
перевести их нельзя, поэтому миграция даёт `ForeignKeyTargetChangeError`, пока в таблице есть хоть
одно такое значение. Добавьте новое поле связи на новую колонку, заполните его через
`RunPython`/`RunSQL`, затем удалите старое поле. `makemigrations` выводит такое же предупреждение.

Переключение `ManyToManyField` с автоматической промежуточной таблицы hare на `through=SomeModel`
(или обратно) копирует строки связи в новую таблицу и удаляет автоматическую. Если у обеих одно и то
же имя таблицы, `makemigrations` сначала переименовывает автоматическую таблицу в `<таблица>__swap`,
чтобы таблицу промежуточной модели можно было создать под этим именем. Остальные колонки
промежуточной модели у скопированных строк получают своё значение по умолчанию на стороне Python
(функция вычисляется один раз, поэтому все строки получают одно и то же значение, — для уникальной
колонки это отклоняется с `ConfigurationError`), текущее время для `auto_now`/`auto_now_add` или свой
`db_default`; колонка без всего этого должна допускать `NULL`.

Модель, перенесённая в другое приложение с той же таблицей (то же имя таблицы, поля и опции),
становится `CreateModel(..., state_only=True)` в новом приложении и `DeleteModel(..., state_only=True)`
в старом: оба меняют только состояние миграций, таблица и её строки остаются, а связи, указывающие на
модель, перенаправляются без команд изменения схемы. Миграция нового приложения зависит от миграций
старого. Перенесённая модель, у которой в том же запуске меняются и поля или опции, отклоняется —
сначала перенесите её без изменений, потом меняйте.

Удаление модели, на которую ссылается другое приложение, делает удаляющую миграцию зависимой от новой
миграции другого приложения, которая удаляет или перенаправляет ссылку. `makemigrations` только для
удаляющего приложения отклоняется, пока миграции другого приложения ещё ссылаются на модель. О файлах
миграций, которые нельзя выполнить по порядку (связь осталась указывать на удалённую или так и не
созданную модель), любая команда сообщает обычной ошибкой с названием связи.

Модель, удалённая в том же запуске, в котором новая связь забирает одну из её обратных ссылок
(например, `Book` заменена `Novel`, обе с `related_name="books"` у `Author`), удаляется до создания
новой модели; если на неё ещё ссылается другая модель до более поздней части миграции, сначала
удаляется только её конфликтующее поле связи. Добавленная модель, у которой в точности те же поля и
опции, что у удалённой модели того же приложения, но которая не распознана как её переименование
(например, две одинаковые модели переименованы одновременно), получает от `makemigrations`
предупреждение «возможно, нераспознанное переименование»: созданная пара `CreateModel`/`DeleteModel`
теряет строки старой таблицы; если это переименование, замените её вручную на `RenameModel`.

`AddPartition`/`RemovePartition` добавляют и удаляют одну партицию
[партиционированной таблицы](../models/meta-options.ru.md#partitioning) — `ListPartition`,
`RangePartition` или `DefaultPartition` из `PostgresqlTableOptions` модели. `makemigrations` пишет
по операции на каждую добавленную или убранную партицию, поэтому в файле миграции видно, какие
именно. `AddPartition` создаёт пустую партицию с параметрами хранения и табличным пространством
таблицы. `RemovePartition` отсоединяет партицию и удаляет её **вместе со строками** —
`makemigrations` об этом предупреждает; партиция, у которой изменились значения или границы,
удаляется и добавляется заново. Обе операции обратимы: откат `AddPartition` удаляет партицию вместе
со строками, которые в неё попали, откат `RemovePartition` возвращает её пустой. На подключении
другого диалекта они меняют только состояние. Всё остальное в том, как таблица партиционирована, —
партиционирование включено или снято, другая стратегия, ключ или число хеш-партиций — PostgreSQL на
месте изменить не может: такой `AlterModelOptions` создаёт таблицу заново и копирует строки (индексы,
ограничения, комментарии, триггеры и внешние ключи других таблиц, ссылающиеся на неё, ставятся
снова), переписывая всю таблицу под исключительной блокировкой.

`AlterModelOptions.options` — это весь набор опций модели, у которых нет своей операции (всё, кроме
таблицы, схемы, индексов, ограничений, триггеров, первичного ключа, представлений, материализованных представлений, функций, последовательностей, защиты строк, политик и прав): опция, которой
в нём нет, удаляется из модели. Изменённый `table_description` обновляет комментарий таблицы в
PostgreSQL.

Перевод существующей модели на `Meta.managed = False` создаёт `AlterModelOptions`, который оставляет
модель в состоянии миграций с `managed: False`: её таблица и строки остаются, и пока модель не
управляется миграциями, ни одно её дальнейшее изменение таблицу не трогает. `CreateModel`/`DeleteModel`
модели, которая в состоянии не управляется, тоже не выполняют команд изменения схемы, поэтому удаление
такой модели из кода оставляет её таблицу на месте. Возврат к управлению — снова `AlterModelOptions`,
за которым идут изменения полей и индексов, накопившиеся за это время. Модель, которая никогда не
управлялась миграциями (с самого начала `managed = False`), в состоянии миграций вообще не
присутствует — включение управления создаёт `CreateModel`; если её таблица уже существует, примените
эту миграцию через `migrate --fake`.

`AlterModelSchema(name: str, schema: str | None)` переносит таблицу модели (`ALTER TABLE ... SET
SCHEMA`, только PostgreSQL; в SQLite ничего не делает) вместе с автоматическими промежуточными
таблицами её собственных `ManyToManyField`, которые находятся в схеме объявившей их модели.
`schema=None` переносит их в текущую схему подключения, где создаётся таблица без `Meta.schema`.

## <a id="index-operations"></a>Индексы

```python
AddIndex(model_name: str, index: Index, *, concurrently: bool = False)
RemoveIndex(model_name: str, name: str | None = None, fields: list[str] | None = None, *, concurrently: bool = False)
RenameIndex(model_name: str, new_name: str, *, old_name: str | None = None, old_fields: list[str] | None = None)
```

`concurrently=True` создаёт или удаляет индекс, не блокируя запись в таблицу, —
`CREATE INDEX CONCURRENTLY`/`DROP INDEX CONCURRENTLY` в PostgreSQL, которые нельзя выполнять внутри
транзакции: задайте миграции `atomic = False`, иначе операция даст `ConfigurationError`.
`makemigrations` записывает обычные `AddIndex`/`RemoveIndex`; для большой нагруженной таблицы
добавьте `concurrently=True` вручную. SQLite создаёт и удаляет индекс обычным способом.

```python
class Migration(migrations.Migration):
    atomic = False
    operations = [
        ops.AddIndex("Order", Index(fields=["placed_at"], name="orders_placed_idx"), concurrently=True),
    ]
```

`RemoveIndex(name=...)` находит и безымянный индекс по выражению (например, `Index(Lower("name"))`) по
имени, под которым он создан (`idx_<таблица>_expr_<хэш>`, `uidx_...` для уникального), —
`makemigrations` записывает это имя, когда такой индекс удаляется или меняется.

## <a id="constraint-operations"></a>Ограничения

```python
AddConstraint(
    model_name: str,
    constraint: UniqueConstraint | CheckConstraint | ExclusionConstraint,
    *,
    not_valid: bool = False,
    using_index: str | None = None,
)
RemoveConstraint(model_name: str, name: str | None = None, fields: list[str] | None = None)
RenameConstraint(model_name: str, old_name: str, new_name: str)
ValidateConstraint(model_name: str, name: str)
```

Все ограничения лежат в `Meta.constraints` модели, в том числе безымянное
`UniqueConstraint(fields=...)` — его `RemoveConstraint(fields=[...])` находит по полям.

`AddConstraint(..., not_valid=True)` добавляет ограничение CHECK, которому должны соответствовать
только новые и изменённые строки (`NOT VALID` в PostgreSQL), не просматривая и не блокируя большую
таблицу; позже `ValidateConstraint` проверяет существующие строки с блокировкой, которая не мешает
записи, и даёт `IntegrityError`, пока хоть одна строка ограничение нарушает. `not_valid=True`
принимает только `CheckConstraint` (иначе `ConfigurationError`). В состоянии модели ограничение в обоих
случаях хранится как объявлено, проверка расхождений не считает ограничение `NOT VALID` расхождением,
а `inspectdb` отмечает его. SQLite добавляет ограничение обычным способом (проверяя все строки), и
проверять потом нечего.

`AddConstraint(..., using_index="<имя индекса>")` делает так, что именованное `UniqueConstraint` без
`condition` забирает себе уникальный индекс, который у модели уже есть, — построенный раньше через
`AddIndex(..., unique=True, concurrently=True)`, — вместо того чтобы строить свой, пока запись в таблицу
ждёт: `ADD CONSTRAINT ... UNIQUE USING INDEX` в PostgreSQL, который переименовывает индекс в имя
ограничения. В состоянии миграций индекс уходит из `Meta.indexes` модели, поэтому модель объявляет только
ограничение. При откате ограничение удаляется, а индекс строится заново — для `AddIndex` перед ним.
SQLite оставляет индекс с именем ограничения как есть (её уникальное ограничение и есть уникальный индекс
своего имени); под другим именем строит индекс ограничения и удаляет прежний. Другое ограничение даёт
`ConfigurationError`. См. [миграции без остановки приложения](zero-downtime.ru.md#adding-a-constraint).

## <a id="trigger-operations"></a>Триггеры

```python
AddTrigger(model_name: str, trigger: Trigger)
RemoveTrigger(model_name: str, name: str)
AlterTrigger(model_name: str, trigger: Trigger)
RenameTrigger(model_name: str, old_name: str, new_name: str)
```

## <a id="raw-operations"></a>SQL, Python, схемы, расширения и правила сортировки

```python
RunPython(code, reverse_code=None, *, atomic: bool | None = None, elidable: bool = False, tenant_schema: bool = False)   # code(apps, schema_editor) -> None | Awaitable[None]
RunSQL(sql, reverse_sql=None, *, atomic: bool | None = None, elidable: bool = False, tenant_schema: bool = False)        # строка, список строк или список пар (sql, parameters)
SQLOperation(query: str, values: list)                               # одна команда с параметрами; откатить нельзя
CreateSchema(schema_name: str)
DropSchema(schema_name: str)
CreateExtension(extension_name: str)   # только PostgreSQL
RemoveExtension(extension_name: str)   # только PostgreSQL
CreateCollation(name: str, locale: str, *, provider: str = "libc", deterministic: bool = True)   # только PostgreSQL
RemoveCollation(name: str, locale: str, *, provider: str = "libc", deterministic: bool = True)   # только PostgreSQL
```

`CreateCollation` создаёт правило сравнения строк (collation) для `Collate(...)` и `db_collation=` —
например, без учёта регистра на основе ICU:
`CreateCollation("case_insensitive", "und-u-ks-level2", provider="icu", deterministic=False)`.
`RemoveCollation` принимает те же аргументы, чтобы при откате создать его снова. В SQLite нет команд
для правил сравнения — там обе операции пропускаются с предупреждением.

`RunPython.noop` — готовая функция, которая ничего не делает; её можно передать как `reverse_code` для
миграции данных, которую откатывать не нужно.
`elidable=True` выбрасывает операцию при [сжатии](migrations.ru.md#squashing-migrations) её миграции —
для данных, которые сжатой истории записывать заново не нужно.
`tenant_schema=True` выполняет операцию в схеме каждого арендатора вместо общей — на подключении со
[схемой на арендатора](../soft-delete-versions-tenants/schema-per-tenant.ru.md#migrations); операция над
таблицей модели и так следует её `Meta.tenant_schema`.

## <a id="separate-database-and-state"></a>База и состояние по отдельности

```python
SeparateDatabaseAndState(database_operations: list[Operation] | None = None, state_operations: list[Operation] | None = None)
```

Применяет два списка операций по отдельности: `state_operations` меняют только модели, которые знают
миграции (с ними `makemigrations` сравнивает код), `database_operations` — только базу. Для изменения,
которое в базе уже есть, изменения в несколько шагов или SQL, который состояние описать не может:

```python
from hare.migrations import operations as ops

operations = [
    # Поле уходит из кода и состояния сейчас; колонку удалит следующая миграция,
    # когда её уже не читает ни один работающий код.
    ops.SeparateDatabaseAndState(
        state_operations=[ops.RemoveField(model_name="Book", name="legacy_code")],
    ),
]
```

```python
operations = [
    # Колонка и поле появляются вместе, индекс строится вручную.
    ops.SeparateDatabaseAndState(
        database_operations=[
            ops.AddField(model_name="Book", name="pages", field=fields.IntField(null=True)),
            ops.RunSQL(
                "CREATE INDEX book_pages ON book (pages)",
                reverse_sql="DROP INDEX book_pages",
            ),
        ],
        state_operations=[ops.AddField(model_name="Book", name="pages", field=fields.IntField(null=True))],
    ),
]
```

- **Применение:** операции состояния меняют состояние; операции базы выполняются по порядку, каждая —
  над моделями в том виде, в каком их оставили предыдущие операции `database_operations`. Их
  собственные изменения состояния дальше этого не идут, и миграция их не оставляет.
- **Откат:** операции базы откатываются в обратном порядке, состояние возвращается к тому, каким оно
  было до миграции. Операция откатываема, если откатываема каждая операция базы (`RunSQL`/`RunPython`
  с обратным действием, любая операция схемы); без операций базы — всегда.
- **`sqlmigrate`** показывает SQL операций базы; у операций состояния SQL нет.
- **Файл миграции:** записывается вызовом, как выше, каждый список — со своими операциями; пустой
  список не пишется.
- **Сжатие:** операции по разные стороны от неё никогда не объединяются через неё, и ничего внутри
  неё не объединяется с операциями вокруг.
- **SQLite:** колонку, которой в состоянии уже нет, удалит следующая пересборка её таблицы — см.
  `RemoveField` выше. Удалите её через `RunSQL` раньше, чем другое поле этой таблицы изменится так,
  что SQLite пересоберёт таблицу.

## <a id="operation-deconstruct"></a>Как операция записывается в файл

Индексы, ограничения и триггеры для миграций — одно и то же: именованный объект схемы у модели.
`AddIndex`/`AddConstraint`/`AddTrigger`, операции `Remove*`, `Rename*` и `AlterTrigger` — тонкие
подклассы четырёх общих операций (`AddSchemaObject`, `RemoveSchemaObject`, `RenameSchemaObject`,
`AlterSchemaObject` в `hare.migrations.operations.schema_objects`), а автоопределение изменений
сравнивает все три вида одним сравнением.

Любая операция — и ваша собственная — записывается в файл миграции из `Operation.deconstruct()`,
который возвращает `(путь класса, позиционные аргументы, именованные аргументы)`; автор файла
миграции выводит это как вызов. По умолчанию параметры конструктора берутся из одноимённых
атрибутов, а именованный аргумент со значением по умолчанию пропускается, поэтому операции, чей
`__init__` сохраняет каждый аргумент под его же именем, больше ничего не нужно:

```python
from hare.migrations.operations import Operation


class CreateReportSnapshot(Operation):
    def __init__(self, name: str, query: str, *, with_data: bool = True) -> None:
        self.name = name
        self.query = query
        self.with_data = with_data
    ...
```

Переопределяйте `deconstruct()`, только если аргументы хранятся не в том виде, в каком переданы.

Собственные операции hare происходят от `HareOperation` (`hare.migrations.operations`) — `Operation`,
которая меняет состояние и базу по отдельности; своя операция происходит от `Operation`.

## <a id="extensions"></a>Расширения

Расширения PostgreSQL, нужные модели, объявляйте через `Meta.extensions`, а не пишите вручную
`RunSQL("CREATE EXTENSION IF NOT EXISTS ...")`:

```python
class Widget(Model):
    ...
    class Meta:
        extensions = ("pg_trgm",)
```

Тип поля, которому расширение нужно всегда (`CitextField` нужен `citext`, `PostGISField` — `postgis`),
объявляет его сам — повторять в `Meta.extensions` не нужно. Автоматическое создание миграций при
каждом сравнении двух состояний моделей заново выводит полный набор нужных расширений прямо из
`Meta.extensions`, собственного `requires_extension` поля и расширения, которое нужно типу колонки
поля на каждом диалекте (`TypeMapping.extension` — `postgis` для `GeometryField`, `vector` для
`VectorField`), — так же, как наличие схем выводится из `Meta.schema`, а
не хранится отдельно, — и создаёт операции `CreateExtension`/`RemoveExtension` для изменившегося,
упорядочивая их так, чтобы `CREATE EXTENSION` выполнялся раньше любой зависящей от него таблицы, а
`DROP EXTENSION` — только после удаления всех зависящих таблиц.

## <a id="enum-types"></a>Типы ENUM

```python
CreateEnumType(name: str, labels: Sequence[str])                      # только Postgres
AlterEnumType(name: str, old_labels: Sequence[str], new_labels: Sequence[str])   # только Postgres
DropEnumType(name: str, labels: Sequence[str])                        # только Postgres
```

Типы `ENUM` колонок [`NativeEnumField`](../dialects/postgresql/fields.ru.md#nativeenumfield). Как и
расширения, они не хранятся в состоянии: автоматическое создание миграций выводит их из полей
(`field.requires_enum_type`) при каждом сравнении двух состояний — `CreateEnumType` перед первой
колонкой типа, `AlterEnumType`, когда меняются его метки, `DropEnumType` после того, как ушла его
последняя колонка. Два поля, называющие один тип с разными метками, дают `ConfigurationError`.

- `AlterEnumType` добавляет новые метки на месте (`ALTER TYPE ... ADD VALUE ... BEFORE ...`), пока старые
  метки сохраняют порядок. Удалённая или переставленная метка заменяет тип одной командой: тип
  переименовывается, создаётся новый, каждая колонка этого типа (и каждая колонка-массив) преобразуется
  через текст вместе со значением по умолчанию, старый тип удаляется — строка, в которой ещё есть
  удалённая метка, останавливает это ошибкой, поэтому обновите такие строки в более ранней миграции.
  Откат выполняет обратное изменение.
- `DropEnumType` при откате снова создаёт тип с `labels`; `CreateEnumType` при откате удаляет его.
- На базе без типов `ENUM` (`features.supports_enum_types` ложно) все три пропускаются с
  предупреждением — файл миграции выполняется на любой базе.

## <a id="constraints-and-triggers"></a>Ограничения и триггеры как объекты

`UniqueConstraint`, `CheckConstraint`, `ExclusionConstraint` и `Trigger` описаны в разделах
[Ограничения](../models/constraints-and-triggers.ru.md#constraints) и
[Триггеры](../models/constraints-and-triggers.ru.md#triggers): те же классы данных, что используются в
`Meta.constraints`/`Meta.triggers`, передаются в `AddConstraint`/`AddTrigger`, когда миграция пишется
вручную.

## <a id="schema-objects"></a>Представления, функции, последовательности и доступ

```python
AddView(model_name: str, view: View)
AlterView(model_name: str, view: View)
RemoveView(model_name: str, name: str)
RenameView(model_name: str, old_name: str, new_name: str)

AddMaterializedView(model_name: str, view: MaterializedView)
AlterMaterializedView(model_name: str, view: MaterializedView)
RemoveMaterializedView(model_name: str, name: str)
RenameMaterializedView(model_name: str, old_name: str, new_name: str)
RefreshMaterializedView(model_name: str, name: str, concurrently: bool = False)

AddFunction(model_name: str, function: DatabaseFunction)
AlterFunction(model_name: str, function: DatabaseFunction)
RemoveFunction(model_name: str, name: str)
RenameFunction(model_name: str, old_name: str, new_name: str)

AddSequence(model_name: str, sequence: DatabaseSequence)
AlterSequence(model_name: str, sequence: DatabaseSequence)
RemoveSequence(model_name: str, name: str)
RenameSequence(model_name: str, old_name: str, new_name: str)

AlterRowLevelSecurity(model_name: str, row_level_security: RowLevelSecurity | None)
AddPolicy(model_name: str, policy: Policy)
AlterPolicy(model_name: str, policy: Policy)
RemovePolicy(model_name: str, name: str)
RenamePolicy(model_name: str, old_name: str, new_name: str)

AddGrant(model_name: str, grant: Grant)
RemoveGrant(model_name: str, grant: Grant)
```

Операции над тем, что модель объявляет в `Meta.views`, `Meta.materialized_views`, `Meta.functions`,
`Meta.sequences`, `Meta.row_level_security`, `Meta.policies` и `Meta.grants`, — сами объявления, что
каждое изменение делает в базе и в каком порядке `makemigrations` пишет операции, описано в разделе
[Представления, функции, последовательности и доступ](../models/schema-objects.ru.md). Как операции
индексов, ограничений и триггеров, они хранят объект в состоянии миграций модели: операция `Add`
кладёт его туда, `Alter` заменяет версию с тем же именем, `Rename` переименовывает, а `Remove` находит
его там по имени, — поэтому каждая из них обратима: при откате объект создаётся, восстанавливается или
переименовывается таким, каким его держало предыдущее состояние. `AlterRowLevelSecurity` при откате
возвращает прежнюю настройку. У права нет имени: `RemoveGrant` принимает право целиком и находит в
состоянии равное ему.

`AddView`/`AddMaterializedView`, написанные вручную, принимают и QuerySet (`View("paid", query=
Invoice.objects.filter(paid=True))`): состояние миграций хранит его SQL, а операция выполняет его как
SQL подключения, на котором идёт миграция. Файл миграции, который пишут `makemigrations` или
`squashmigrations`, содержит этот SQL как `RawSQLTerm(...)`.

`RefreshMaterializedView` заново наполняет материализованное представление строками его запроса —
например, после миграции данных, изменившей читаемые им строки. Состояние она не меняет, а при откате
ничего не делает. `concurrently=True` оставляет представление доступным для чтения во время
обновления и требует его `unique_columns` (иначе `ConfigurationError` до отправки SQL); `concurrently`
не типа bool вызывает `ConfigurationError`.

На базе без такого вида объектов (`features.supports_views`, `supports_materialized_views`,
`supports_database_functions`, `supports_sequences`, `supports_row_level_security`,
`supports_grants`) каждая операция вызывает `UnSupportedError` до отправки SQL — и в `sqlmigrate`
тоже. `sqlmigrate` показывает команды, которые каждая операция выполняет в PostgreSQL:

```sql
CREATE SEQUENCE "invoice_number" INCREMENT BY 1 START WITH 1000 CACHE 1 NO CYCLE;
ALTER SEQUENCE "invoice_number" OWNED BY "invoice"."number";
CREATE MATERIALIZED VIEW "invoice_totals" AS
SELECT tenant_id, sum(amount) AS total FROM invoice GROUP BY tenant_id
WITH DATA;
CREATE UNIQUE INDEX "invoice_totals_unique" ON "invoice_totals" ("tenant_id");
ALTER TABLE "invoice" ENABLE ROW LEVEL SECURITY;
CREATE POLICY "tenant_rows" ON "invoice" AS PERMISSIVE FOR SELECT TO "reporting" USING (tenant_id = current_tenant());
GRANT SELECT ON TABLE "invoice" TO "reporting";
REFRESH MATERIALIZED VIEW CONCURRENTLY "invoice_totals";
```

## <a id="clickhouse-operations"></a>ClickHouse

```python
AddDictionary(model_name: str, dictionary: Dictionary)
AlterDictionary(model_name: str, dictionary: Dictionary)
RemoveDictionary(model_name: str, name: str)
RenameDictionary(model_name: str, old_name: str, new_name: str)
SynchronizeKeySeries(model_name: str)
```

Операции со словарями работают с тем, что модель объявляет в `Meta.dictionaries`, — см.
[Словари ClickHouse](../dialects/clickhouse/schema-objects.ru.md#dictionaries).
`SynchronizeKeySeries` переводит последовательность чисел, из которой берётся генерируемый ключ модели
ClickHouse, за наибольший ключ её таблицы — см. [Ключи ClickHouse](../dialects/clickhouse/models.ru.md#keys).

## <a id="indexes"></a>Классы индексов

`Index`/`PartialIndex` описаны в разделе [Индексы](../models/indexes.ru.md), а
`GinIndex`/`GistIndex`/`BrinIndex`/`BloomIndex`/`HashIndex`/`SpGistIndex` — в разделе
[Типы индексов PostgreSQL](../models/indexes.ru.md#postgresql-index-types).
