# Операции

Операции, которые перечисляет файл миграции, из `hare.migrations.operations`
(`from hare.migrations import operations as ops`): что принимает каждая, что делает в базе и в состоянии
миграций и как написать свою.

## <a id="model-operations"></a>Модели

Операции таблицы модели: создать, переименовать и удалить её, изменить её опции и имя таблицы.

```python
class CreateModel:
    def __init__(
        self,
        name: str,
        fields: list[tuple[str, FieldLike]],
        options: dict[str, Any] | None = None,
        bases: list[str] | None = None,
        state_only: bool = False,
    ) -> None: ...

class RenameModel:
    def __init__(self, old_name: str, new_name: str) -> None: ...

class DeleteModel:
    def __init__(self, name: str, state_only: bool = False) -> None: ...

class AlterModelOptions:
    def __init__(self, name: str, options: dict[str, Any]) -> None: ...

class AlterModelTable:
    def __init__(self, name: str, table: str) -> None: ...

class AlterModelSchema:
    def __init__(self, name: str, schema: str | None) -> None: ...
```

`RenameModel` и `AlterModelTable` оставляют готовый SQL представлений и материализованных представлений
модели таким, как он написан: готовый SQL — ваш, hare его никогда не переписывает. Представление, запрос
которого вы переписали под новое имя таблицы, — изменённое объявление: `makemigrations` удаляет его до
переименования и добавляет после.

Модель, перенесённая в другое приложение с той же таблицей (то же имя таблицы, поля и опции), когда
`makemigrations` подтвердили перенос (см. [Переименования](migrations.ru.md#renames)), становится
`CreateModel(..., state_only=True)` в новом приложении и `DeleteModel(..., state_only=True)` в старом:
оба меняют только состояние миграций, таблица и её строки остаются, а связи, указывающие на модель,
перенаправляются без команд изменения схемы. Миграция нового приложения зависит от миграций старого.
Перенесённая модель, у которой в том же запуске меняются и поля или опции, отклоняется — сначала
перенесите её без изменений, потом меняйте.

`makemigrations` упорядочивает операции всех приложений, для которых запущен, вместе — каждую после того,
что ей нужно: связь — после модели, на которую она указывает, полей, которые называет её `to_field`, и её
промежуточной модели; модель или поле — после того, как указывающие на них связи удалены или
перенаправлены; модель, занимающая таблицу, — после удаления модели, у которой эта таблица была. Операции,
которым ничего друг от друга не нужно, сохраняют порядок, в котором созданы. Затем он режет их на
миграции по приложениям — операция, которой нужна новая миграция другого приложения, уже зависящая от
миграции её собственного, идёт в следующую миграцию своего приложения, — и каждая миграция зависит от
новых миграций того, что ей нужно, и от существующих миграций приложения, чья модель ей нужна. Связь,
замыкающая цикл среди моделей, создаваемых вместе, добавляется `AddField` после них (связь «многие ко
многим» прежде внешнего ключа), а замыкающая цикл среди моделей, удаляемых вместе, — модель удаляется
вместе с through-моделью своей связи «многие ко многим», две модели ссылаются друг на друга, —
удаляется `RemoveField` перед ними.

Удаление модели, на которую ссылается другое приложение, делает удаляющую миграцию зависимой от новой
миграции другого приложения, которая удаляет или перенаправляет ссылку. `makemigrations` только для
удаляющего приложения отклоняется, пока миграции другого приложения ещё ссылаются на модель. О файлах
миграций, которые нельзя выполнить по порядку (связь осталась указывать на удалённую или так и не
созданную модель), любая команда сообщает обычной ошибкой с названием связи.

Модель, удалённая в том же запуске, в котором новая связь забирает одну из её обратных ссылок (например,
`Book` заменена `Novel`, обе с `related_name="books"` у `Author`), удаляется до создания новой модели;
если на неё ещё ссылается другая модель до более поздней части миграции, сначала удаляется только её
конфликтующее поле связи. Добавленная модель, у которой в точности те же поля и опции, что у удалённой
модели того же приложения, но которая не переименована (на вопрос о переименовании ответили «нет» или он
не задавался — например, две одинаковые модели переименованы одновременно), получает от `makemigrations`
предупреждение «возможно, нераспознанное переименование»: созданная пара `CreateModel`/`DeleteModel`
теряет строки старой таблицы; если это переименование, укажите его через `--rename` — для нескольких
одинаковых моделей по `--rename` на каждую.

`AlterModelOptions.options` — это весь набор опций модели, у которых нет своей операции (всё, кроме
таблицы, схемы, индексов, ограничений, триггеров, первичного ключа, представлений, материализованных
представлений, функций, последовательностей, защиты строк, политик и прав): опция, которой в нём нет,
удаляется из модели. Пустая опция, `managed=True` и опции, которые состояние миграций не хранит, —
`ordering`, `get_latest_by`, `manager`, `returning`, `track_dirty_fields`, `change_capture`, схему они не
меняют, — в состояние не попадают, как их не пишет и `makemigrations`. Изменённый `table_description`
обновляет комментарий таблицы в PostgreSQL.
`Meta.tenant_field`, `soft_delete_field` и `optimistic_lock_field` называют поле в любом состоянии: когда
опция переходит на другое поле, `makemigrations` пишет сначала новое поле, затем `AlterModelOptions` и
последним — удаление старого поля.

Перевод существующей модели на `Meta.managed = False` создаёт `AlterModelOptions`, который оставляет
модель в состоянии миграций с `managed: False`: её таблица и строки остаются, и пока модель не
управляется миграциями, ни одно её дальнейшее изменение таблицу не трогает. `CreateModel`/`DeleteModel`
модели, которая в состоянии не управляется, тоже не выполняют команд изменения схемы, поэтому удаление
такой модели из кода оставляет её таблицу на месте. Возврат к управлению — снова `AlterModelOptions`, за
которым идут изменения полей и индексов, накопившиеся за это время. Модель, которая никогда не
управлялась миграциями (с самого начала `managed = False`), в состоянии миграций вообще не присутствует —
включение управления создаёт `CreateModel`; если её таблица уже существует, примените эту миграцию через
`migrate --fake`.

`AlterModelSchema(name: str, schema: str | None)` переносит таблицу модели (`ALTER TABLE ... SET SCHEMA` в
PostgreSQL, `RENAME TABLE` в другую базу сервера в ClickHouse — с отказом для модели, чьи представления,
материализованные представления, словари или таблица `Distributed` называют таблицу по её базе; SQLite
не умеет переносить таблицу в другую подключённую базу и отказывает) вместе с партициями партиционированной
таблицы и автоматическими промежуточными таблицами её собственных `ManyToManyField`, которые находятся в схеме
объявившей их модели, и с представлениями, материализованными представлениями, функциями и
последовательностями, которые объявляет модель (тип `ENUM` поля остаётся в своей схеме); база, которая не
умеет перенести один из них, даёт `UnSupportedError` до любого DDL. `schema=None` переносит их в текущую
схему подключения, где создаётся таблица без `Meta.schema`.

## <a id="partition-operations"></a>Партиции

Добавление и удаление секции секционированной таблицы (PostgreSQL) — см. [Секционированные
таблицы](../dialects/postgresql/indexes-and-tables.ru.md#partitioning).

```python
class AddPartition:
    def __init__(self, model_name: str, partition: Any) -> None: ...

class RemovePartition:
    def __init__(self, model_name: str, partition: Any) -> None: ...
```

`AddPartition`/`RemovePartition` добавляют и удаляют одну партицию [партиционированной
таблицы](../dialects/postgresql/indexes-and-tables.ru.md#partitioning) — `ListPartition`,
`RangePartition` или `DefaultPartition` из `PostgresqlTableOptions` модели. `makemigrations` пишет по
операции на каждую добавленную или убранную партицию, поэтому в файле миграции видно, какие именно.
`AddPartition` создаёт пустую партицию с параметрами хранения и табличным пространством таблицы.
`RemovePartition` отсоединяет партицию и удаляет её **вместе со строками** — `makemigrations` об этом
предупреждает; партиция, у которой изменились значения или границы, удаляется и добавляется заново. Обе
операции обратимы: откат `AddPartition` удаляет партицию вместе со строками, которые в неё попали, откат
`RemovePartition` возвращает её пустой. На подключении другого диалекта они меняют только состояние. Всё
остальное в том, как таблица партиционирована, — партиционирование включено или снято, другая стратегия,
ключ или число хеш-партиций — PostgreSQL на месте изменить не может: такой `AlterModelOptions` создаёт
таблицу заново и копирует строки (индексы, ограничения, комментарии, триггеры и внешние ключи других
таблиц, ссылающиеся на неё, ставятся снова), переписывая всю таблицу под исключительной блокировкой.

## <a id="field-operations"></a>Поля

Операции над полями модели: добавить, удалить, изменить и переименовать поле, заполнить колонку большой
таблицы.

```python
class AddField:
    def __init__(
        self,
        model_name: str,
        name: str,
        field: FieldLike,
        *,
        not_valid: bool = False,
    ) -> None: ...

class RemoveField:
    def __init__(self, model_name: str, name: str) -> None: ...

class AlterField:
    def __init__(self, model_name: str, name: str, field: FieldLike) -> None: ...

class RenameField:
    def __init__(
        self,
        model_name: str,
        old_name: str,
        new_name: str,
        field: FieldLike | None = None,
    ) -> None: ...

class BackfillColumn:
    def __init__(
        self,
        model_name: str,
        field_name: str,
        value: Any | Callable[[], Any],
        *,
        batch_size: int = 1000,
    ) -> None: ...

class AlterColumnNotNullSafe:
    def __init__(self, model_name: str, field_name: str) -> None: ...
```

### <a id="add-field"></a>`AddField`

`AddField` добавляет колонку и собственный индекс поля — `db_index=True` и индекс связи — после
заполнения существующих строк; индекс — это флаг поля, а не отдельная операция индекса. `AlterField`
создаёт или удаляет его при смене флага, `RemoveField` удаляет вместе с колонкой.

`AddField(..., not_valid=True)` поля `ForeignKeyField`/`OneToOneField` с ограничением в базе добавляет
колонку, затем её внешний ключ `NOT VALID` (PostgreSQL) под тем именем, которое получил бы встроенный
`REFERENCES`: ему должны соответствовать только новые и изменённые строки, поэтому добавление не
сканирует таблицу, пока запись в обе таблицы ждёт; позже
`ValidateConstraint(model_name, <the foreign key's name>)` проверяет существующие строки с блокировкой,
которая не мешает записи. Другое поле или связь с `db_constraint=False` дают `ConfigurationError`. SQLite
никогда не проверяет существующие строки при добавлении колонки, поэтому там параметр ничего не меняет.

`AddField` поля `NOT NULL`, у которого значение по умолчанию есть только на стороне Python (`default=`,
без `db_default`), работает и на таблице, где уже есть строки: колонка добавляется с `NULL`, каждой
существующей строке записывается значение по умолчанию, затем колонка становится `NOT NULL`
(`ALTER COLUMN ... SET NOT NULL` в PostgreSQL, пересоздание таблицы в SQLite). Функция по умолчанию
вычисляется **один раз**, поэтому все существующие строки получают одно и то же значение — для значения,
своего у каждой строки (например, `uuid4` в уникальной колонке), добавьте колонку с `NULL`, заполните её
шагом `RunPython`, затем сделайте `NOT NULL`. `DatetimeField`/`TimeField` `NOT NULL` с
`auto_now=True`/`auto_now_add=True` (и без `default`/`db_default`) заполняется так же — текущим временем,
записанным точно так, как его записывает обычный `save()`.

Поле `NOT NULL` вовсе без значения по умолчанию нельзя добавить в таблицу, где уже есть строки:
PostgreSQL и SQLite отказывают в колонке, и миграция откатывается. ClickHouse колонку добавляет, а его
сохранённые строки читают пустое значение типа (`0`, `''`) — и у поля с `unique=True` тоже, так что его
строки получают одно и то же значение.

### <a id="remove-field"></a>`RemoveField`

`RemoveField` на SQLite удаляет колонку командой `ALTER TABLE ... DROP COLUMN`, если модель ничем этого не
запрещает, — решает одно состояние миграций, из базы ничего не читается: поле — не ключ, не уникальное, без
индекса, не связь и не генерируемое; у модели нет CHECK-ограничений, генерируемых колонок и триггеров; ни один
её индекс не частичный, не по выражению и не называет поле, и ни одно уникальное ограничение не условное и не
называет его. Объект, о котором миграции не знают, над колонкой (индекс, триггер или представление, созданные
`RunSQL`) приводит к отказу SQLite выполнить `DROP COLUMN`. Иначе таблица пересобирается по модели, которую
знают миграции: создаётся заново, строки копируются, индексы, уникальные ограничения и триггеры создаются
снова. Пересборка сохраняет только колонки из состояния миграций, поэтому колонку, которой в состоянии уже нет
(убранную через [`SeparateDatabaseAndState`](#separate-database-and-state)), удалит следующая пересборка её
таблицы; индекс, триггер или представление, созданные через `RunSQL`, тоже не создаются заново.

### <a id="alter-field"></a>`AlterField`

`AlterField`, меняющий тип колонки первичного ключа (например, `IntField` на `BigIntField`) или другой
колонки, на которую ссылается связь через `to_field=`, меняет и ссылающиеся на неё колонки ключей: каждую
колонку `ForeignKeyField`/`OneToOneField` и каждую колонку автоматической промежуточной таблицы
`ManyToManyField`. В PostgreSQL их ограничения внешних ключей сначала удаляются и создаются заново, когда
у всех колонок уже новый тип; в SQLite ссылающиеся таблицы пересоздаются. Значения преобразуются простым
приведением типа, поэтому в PostgreSQL пара типов, которую он привести не может (`integer` в `uuid`),
даёт ошибку.

`AlterField` на тип колонки, в который не помещается уже хранящееся в таблице значение, на любом диалекте
вызывает `FieldNarrowingDataLossError` до каких-либо изменений. Это меньший `max_length` (или текст, число
либо логическое значение, превращённые в `CharField`, слишком короткий для их текста), `DecimalField` с
меньшим числом знаков после запятой или целых разрядов, меньший целочисленный тип, дробное число или decimal,
превращённые в целое или в decimal, куда значение не помещается, целое или decimal, превращённые в `float`,
который их округляет (`BigIntField` больше 2⁵³, `DecimalField` больше чем из 15 цифр), дата-время со временем
суток, превращённая в дату, целое, отличное от 0 и 1, превращённое в логическое, целое или текст, не
являющиеся элементом, превращённые в `IntEnumField`/`CharEnumField` (или перечисление, потерявшее элемент,
который хранит строка), а в ClickHouse — `Float64`, превращённый в `Float32` (`Float32Field`), момент за
пределами диапазона `DateTime` или с большим числом знаков после секунды, чем его точность
(`ClickhouseDatetimeField`), день вне диапазона `Date` (`ClickhouseDateField`) и текст, который в байтах
длиннее `FixedStringField`. Иначе PostgreSQL обрезал бы или округлил значение при приведении типа, SQLite
хранил бы его сверх объявленного размера колонки, а ClickHouse «завернул» бы его. Сначала исправьте или
расширьте такие значения; на более узкий тип, в который помещается каждое хранимое значение, колонка меняется
как обычно. SQLite копирует каждое значение в перестроенную таблицу как есть, поэтому значение, которое новое
поле не читает, — текст, который не является числом, датой или логическим значением, не `UUID`, не документ
JSON, не длительность, не байты, — даёт `OperationalError` до изменения таблицы (`the column holds "abc",
which is no UUIDField`).

В ClickHouse текстовая колонка, переведённая в другой тип, преобразует свои тексты уже после того, как
получила новый тип: текст, который сервер не читает как значение этого типа, сделал бы таблицу нечитаемой,
стал бы NULL в колонке, допускающей NULL, или (невозможная дата вроде `2024-02-30`) превратился бы в другое
значение. Такой текст даёт `OperationalError` до каких-либо изменений (`column "value" of table "note"
holds "abc", which is no Int32`). Преобразуется текст, который читается как значение типа: дата или момент
в виде `YYYY-MM-DD[ hh:mm:ss]`, decimal не длиннее, чем вмещает поле, объект JSON для `JSONField`, метка
перечисления. Колонка другого типа проверяется так же: значение, которого новый тип не вмещает (целое — не
`UUID`), даёт `OperationalError` до изменения, как и тип, который сервер не переводит ни в одно значение
нового (`DateTime` в `Int16`, `UUID` в `Int32`), — переход между двумя числовыми типами проверяет проверка
сужения. Поле, которое сервер хранит текстом, а hare читает как другой тип (`TimeField`), читает каждый
сохранённый текст так, как читает новое поле: непрочитанный текст (`x`, `25:00:00`) даёт `OperationalError`
до изменения.

`AlterField`, меняющий `to_field=` связи, направляет её колонку ключа (и ограничение внешнего ключа) на
новую целевую колонку. Сохранённые значения ключа указывают на строки через старую целевую колонку и
перевести их нельзя, поэтому миграция даёт `ForeignKeyTargetChangeError`, пока в таблице есть хоть одно
такое значение. Добавьте новое поле связи на новую колонку, заполните его через `RunPython`/`RunSQL`,
затем удалите старое поле. `makemigrations` выводит такое же предупреждение.

Обычное поле, превращённое в `ForeignKeyField`/`OneToOneField` над своей колонкой, —
`author_id = fields.IntField()`, заменённое на `author = fields.ForeignKeyField("app.Author")`, —
становится `RenameField` и `AlterField`: колонка и её значения остаются, её тип становится типом ключа
цели, а индекс связи, уникальность и ограничение внешнего ключа добавляются (в PostgreSQL каждая строка
проверяется по ограничению). Обратное изменение снимает их и оставляет колонку. У `ManyToManyField` и
связи на составной ключ нет одной колонки: такое изменение даёт `ConfigurationError` — удалите поле и
добавьте новое.

Переключение `ManyToManyField` с автоматической промежуточной таблицы hare на `through=SomeModel` (или
обратно) копирует строки связи в новую таблицу и удаляет автоматическую. Если у обеих одно и то же имя
таблицы, `makemigrations` сначала переименовывает автоматическую таблицу в `<table>__swap`, чтобы таблицу
промежуточной модели можно было создать под этим именем. Остальные колонки промежуточной модели у
скопированных строк получают своё значение по умолчанию на стороне Python (функция вычисляется один раз,
поэтому все строки получают одно и то же значение, — для уникальной колонки это отклоняется с
`ConfigurationError`), текущее время для `auto_now`/`auto_now_add` или свой `db_default`; колонка без
всего этого должна допускать `NULL`.

### <a id="rename-field"></a>`RenameField`

`field` у `RenameField` — поле под новым именем, если переименование меняет и его; без него поле остаётся
прежним. Переименование меняет имя поля везде, где объявление называет его как поле: в индексах модели —
в их полях, ключах-выражениях (`Lower("title")`, сортировка) и условиях `Q` — и в ограничениях (их поля,
условия и проверки `Q`, поля исключающего ограничения), в условии `Q` триггера, в `Q` политики, в
`unique_columns` материализованного представления, в `owned_by` последовательности, в
`Meta.tenant_field`/`soft_delete_field`/`optimistic_lock_field`, в опциях таблицы, называющих поля
(`order_by` ClickHouse), а безымянный индекс или ограничение уникальности, названные по своим ключам,
получают имя, которое дала бы им переименованная модель. Оно переименовывает колонки других таблиц,
названные по компоненту составного ключа, — колонку связи `<relation>_<component>`, колонку
автоматической промежуточной таблицы — на любой глубине связей: связь, `to_field` которой называет такое
ключевое поле или ключевые поля самой переименованной связи, переходит на новые имена.

Готовый SQL (`RawSQLTerm` — готовое условие, проверка или ключ, тело триггера, запрос представления,
выражение генерируемой колонки, готовая опция таблицы, запрос проекции) — ваш: переименование оставляет
его таким, как он написан. Объявление, готовый SQL которого вы переписали под новое имя, — изменённое
объявление: `makemigrations` удаляет его до переименования и добавляет после (генерируемое поле удаляется
и добавляется так же), поэтому откат миграции возвращает старое объявление, только когда колонка снова
получила старое имя. `RenameField`, написанный вручную при готовом SQL, который всё ещё называет старую
колонку, работает там, где база сама переименовывает колонку под ним (PostgreSQL, SQLite переписывают
свой DDL); где нет, база отказывает в переименовании — как и с `RunSQL`, готовый SQL меняете вы.

### <a id="backfill-column"></a>`BackfillColumn` и `AlterColumnNotNullSafe`

Две операции для большой работающей таблицы — заполнить колонку пакетами и сделать её NOT NULL без долгой
блокировки — см. [BackfillColumn и
AlterColumnNotNullSafe](zero-downtime.ru.md#backfillcolumn-and-altercolumnnotnullsafe).

## <a id="index-operations"></a>Индексы

```python
class AddIndex:
    def __init__(self, model_name: str, index: Index, *, concurrently: bool = False) -> None: ...

class RemoveIndex:
    def __init__(
        self,
        model_name: str,
        name: str | None = None,
        fields: list[str] | None = None,
        *,
        concurrently: bool = False,
    ) -> None: ...

class RenameIndex:
    def __init__(
        self,
        model_name: str,
        new_name: str,
        *,
        old_name: str | None = None,
        old_fields: list[str] | None = None,
    ) -> None: ...
```

`concurrently=True` создаёт или удаляет индекс, не блокируя запись в таблицу, —
`CREATE INDEX CONCURRENTLY`/`DROP INDEX CONCURRENTLY` в PostgreSQL, которые нельзя выполнять внутри
транзакции: задайте миграции `atomic = False`, иначе операция даст `ConfigurationError`. `makemigrations`
записывает обычные `AddIndex`/`RemoveIndex`; для большой нагруженной таблицы добавьте `concurrently=True`
вручную. База без этой возможности (`Features.supports_concurrent_indexes` — SQLite, ClickHouse) создаёт
и удаляет индекс обычным способом.

```python
class Migration(migrations.Migration):
    atomic = False
    operations = [
        ops.AddIndex("Order", Index(fields=["placed_at"], name="orders_placed_idx"), concurrently=True),
    ]
```

`RemoveIndex(name=...)` находит и безымянный индекс по выражению (например, `Index(Lower("name"))`) по
имени, под которым он создан (`idx_<table>_expr_<hash>`, `uidx_...` для уникального), — `makemigrations`
записывает это имя, когда такой индекс удаляется или меняется.

## <a id="constraint-operations"></a>Ограничения

```python
class AddConstraint:
    def __init__(
        self,
        model_name: str,
        constraint: UniqueConstraint | CheckConstraint | ExclusionConstraint,
        *,
        not_valid: bool = False,
        using_index: str | None = None,
    ) -> None: ...

class RemoveConstraint:
    def __init__(
        self,
        model_name: str,
        name: str | None = None,
        fields: list[str] | None = None,
    ) -> None: ...

class RenameConstraint:
    def __init__(self, model_name: str, old_name: str, new_name: str) -> None: ...

class ValidateConstraint:
    def __init__(self, model_name: str, name: str) -> None: ...
```

Все ограничения лежат в `Meta.constraints` модели, в том числе безымянное `UniqueConstraint(fields=...)`
— его `RemoveConstraint(fields=[...])` находит по полям.

`AddConstraint(..., not_valid=True)` добавляет ограничение CHECK, которому должны соответствовать только
новые и изменённые строки (`NOT VALID` в PostgreSQL), не просматривая и не блокируя большую таблицу;
позже `ValidateConstraint` проверяет существующие строки с блокировкой, которая не мешает записи, и даёт
`IntegrityError`, пока хоть одна строка ограничение нарушает. `not_valid=True` принимает только
`CheckConstraint` (иначе `ConfigurationError`). В состоянии модели ограничение в обоих случаях хранится
как объявлено, проверка расхождений не считает ограничение `NOT VALID` расхождением, а `inspectdb`
отмечает его. SQLite добавляет ограничение обычным способом (проверяя все строки), и проверять потом
нечего. ClickHouse пишет обычный `ADD CONSTRAINT ... CHECK`, который сервер проверяет только на строках,
записанных позже, — существующие строки не проверяются, и `ValidateConstraint` там ничего не проверяет.

`AddConstraint(..., using_index="<index name>")` делает так, что именованное `UniqueConstraint` без
`condition` забирает себе уникальный индекс, который у модели уже есть, — построенный раньше через
`AddIndex(..., unique=True, concurrently=True)`, — вместо того чтобы строить свой, пока запись в таблицу
ждёт: `ADD CONSTRAINT ... UNIQUE USING INDEX` в PostgreSQL, который переименовывает индекс в имя
ограничения. В состоянии миграций индекс уходит из `Meta.indexes` модели, поэтому модель объявляет только
ограничение. При откате ограничение удаляется, а индекс строится заново — для `AddIndex` перед ним.
SQLite оставляет индекс с именем ограничения как есть (её уникальное ограничение и есть уникальный индекс
своего имени); под другим именем строит индекс ограничения и удаляет прежний. Другое ограничение даёт
`ConfigurationError`. См. [миграции без остановки приложения](zero-downtime.ru.md#adding-a-constraint).

## <a id="trigger-operations"></a>Триггеры

Добавление, удаление, изменение и переименование триггера из `Meta.triggers` — см.
[Ограничения и триггеры](../models/constraints-and-triggers.ru.md).

```python
class AddTrigger:
    def __init__(self, model_name: str, trigger: Trigger) -> None: ...

class RemoveTrigger:
    def __init__(self, model_name: str, name: str) -> None: ...

class AlterTrigger:
    def __init__(self, model_name: str, trigger: Trigger) -> None: ...

class RenameTrigger:
    def __init__(self, model_name: str, old_name: str, new_name: str) -> None: ...
```

## <a id="raw-operations"></a>SQL, Python, схемы, расширения и правила сравнения

```python
class RunPython(HareOperation):
    # code(apps, schema_editor) -> None | Awaitable[None]
    def __init__(
        self,
        code: Callable[..., Any],
        reverse_code: Callable[..., Any] | None = None,
        *,
        atomic: bool | None = None,
        elidable: bool = False,
        tenant_schema: bool = False,
    ) -> None: ...

class RunSQL(HareOperation):
    # строка, список строк или список пар (sql, parameters) - каждый параметр на месте плейсхолдера %s на
    # любой базе, как у raw() (литеральный % в таком sql пишется %%)
    def __init__(
        self,
        sql: str | Sequence[Any],
        reverse_sql: str | Sequence[Any] | None = None,
        *,
        atomic: bool | None = None,
        elidable: bool = False,
        tenant_schema: bool = False,
    ) -> None: ...

class CreateSchema:
    def __init__(self, schema_name: str) -> None: ...

class DropSchema:
    def __init__(self, schema_name: str) -> None: ...

class CreateExtension(HareOperation):
    # PostgreSQL
    def __init__(self, extension_name: str) -> None: ...

class RemoveExtension(HareOperation):
    # PostgreSQL
    def __init__(self, extension_name: str) -> None: ...

class CreateCollation(HareOperation):
    # PostgreSQL
    def __init__(
        self,
        name: str,
        locale: str,
        *,
        provider: str = "libc",
        deterministic: bool = True,
    ) -> None: ...

class RemoveCollation(CreateCollation):
    # PostgreSQL
    def __init__(
        self,
        name: str,
        locale: str,
        *,
        provider: str = "libc",
        deterministic: bool = True,
    ) -> None: ...
```

`DropSchema` и откат `CreateSchema` удаляют только пустую схему — база отказывает, если в ней ещё есть
объекты, поэтому схема, созданная не миграциями, никогда не удаляется вместе с содержимым. В SQLite,
где схема не создаётся командой (её роль играет подключённая база), обе операции пропускаются.

`CreateCollation` создаёт правило сравнения строк (collation) для `Collate(...)` — например, без учёта
регистра на основе ICU:
`CreateCollation("case_insensitive", "und-u-ks-level2", provider="icu", deterministic=False)`.
`RemoveCollation` принимает те же аргументы, чтобы при откате создать его снова. В SQLite и ClickHouse нет
команд для правил сравнения (`Features.supports_collations`) — там обе операции пропускаются с
предупреждением.

`apps`, которые получают `code` и `reverse_code`, хранят модели такими, какими их оставили миграции,
применённые до этой. При откате в них только ещё применённые миграции: приложения, зависящего от этого и
уже откаченного тем же запуском, в них нет, поэтому удаление строк через `apps.get_model("app.Model")`
(метка модели, как у `HareContext.get_model()`) не идёт
каскадом в его удалённые таблицы.

`RunPython.noop` — готовая функция, которая ничего не делает; её можно передать как `reverse_code` для
миграции данных, которую откатывать не нужно. `elidable=True` выбрасывает операцию при
[сжатии](migrations.ru.md#squashing-migrations) её миграции — для данных, которые сжатой истории
записывать заново не нужно. `tenant_schema=True` выполняет операцию в схеме каждого арендатора вместо
общей — на подключении со [схемой на
арендатора](../soft-delete-versions-tenants/schema-per-tenant.ru.md#migrations); операция над таблицей
модели и так следует её `Meta.tenant_schema`.

## <a id="separate-database-and-state"></a>База и состояние по отдельности

```python
class SeparateDatabaseAndState:
    def __init__(
        self,
        database_operations: list[Operation] | None = None,
        state_operations: list[Operation] | None = None,
    ) -> None: ...
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

- **Применение:** операции состояния меняют состояние; операции базы выполняются по порядку, каждая — над
  моделями в том виде, в каком их оставили предыдущие операции `database_operations`. Их собственные
  изменения состояния дальше этого не идут, и миграция их не оставляет.
- **Откат:** операции базы откатываются в обратном порядке, состояние возвращается к тому, каким оно было
  до миграции. Операция откатываема, если откатываема каждая операция базы (`RunSQL`/`RunPython` с
  обратным действием, любая операция схемы); без операций базы — всегда.
- **`sqlmigrate`** показывает SQL операций базы; у операций состояния SQL нет.
- **Файл миграции:** записывается вызовом, как выше, каждый список — со своими операциями; пустой список
  не пишется.
- **Сжатие:** операции по разные стороны от неё никогда не объединяются через неё, и ничего внутри неё не
  объединяется с операциями вокруг.
- **SQLite:** колонку, которой в состоянии уже нет, удалит следующая пересборка её таблицы — см.
  [`RemoveField`](#remove-field). Удалите её через `RunSQL` раньше, чем другое поле этой таблицы
  изменится так, что SQLite пересоберёт таблицу.

## <a id="operation-deconstruct"></a>Как операция записывается в файл

Любая операция — и ваша собственная — записывается в файл миграции из `Operation.deconstruct()`, который
возвращает `(class path, positional args, keyword args)`; автор файла миграции выводит это как вызов. По
умолчанию параметры конструктора берутся из одноимённых атрибутов, а именованный аргумент со значением по
умолчанию пропускается, поэтому операции, чей `__init__` сохраняет каждый аргумент под его же именем,
больше ничего не нужно:

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

`migrate` откатывает миграции на одном состоянии, операция за операцией, откладывая то, что каждая
операция в нём изменила, — откат многих миграций стоит столько же, сколько их применение. Своя операция
может менять поле состояния модели на месте, поэтому состояние до неё сохраняется целиком; её
`database_backward()` получает состояние после операции (`old_state`) и копию состояния до неё
(`new_state`). Задайте `backward_reads_state_left = False`, если `database_backward()` не читает
`old_state`, или `backward_reads_state_reached = False`, если не читает `new_state`, — тогда состояние для
неё не копируется, а сторона, которую она не читает, — это другое состояние.

## <a id="extensions"></a>Расширения

Расширения PostgreSQL, нужные модели, объявляйте через `Meta.extensions`, а не пишите вручную
`RunSQL("CREATE EXTENSION IF NOT EXISTS ...")`:

```python
class Book(Model):
    ...
    class Meta:
        extensions = ("pg_trgm",)
```

Поле, индекс или ограничение, которому нужно расширение, называет его само — повторять его в
`Meta.extensions` не нужно: поле — через тип колонки своего диалекта (`TypeMapping.extension` — `citext`
для `CitextField`, `postgis` для `PostGISField` и `GeometryField`, `vector` для `VectorField`), индекс
метода доступа из расширения — через свой класс (`BloomIndex` нужен `bloom`; `GistIndex` или `GinIndex` по
простой скалярной колонке без класса операторов — `btree_gist`/`btree_gin`), ограничение — через редактор
схемы диалекта (GiST-`ExclusionConstraint` или ключу `WITHOUT OVERLAPS` по скалярной колонке нужен
`btree_gist`). Файл миграции выполняется на любой базе, поэтому автоматическое создание миграций создаёт
то, что нужно любому диалекту, — база без расширений пропускает `CreateExtension`, — а
`generate_schemas()` создаёт то, что нужно диалекту его соединения; `CREATE EXTENSION` выполняется раньше
любой зависящей от него таблицы. Расширения, созданные миграциями, — часть состояния миграций: модель,
которой расширение понадобится позже снова, второго `CreateExtension` не получает. Расширение, которое
больше не нужно ни одной модели, остаётся — база может использовать его где-то ещё; удаляет его
написанная вручную `RemoveExtension`.

## <a id="enum-types"></a>Типы ENUM

```python
class CreateEnumType:
    def __init__(self, name: str, labels: Sequence[str], schema: str | None = None) -> None: ...

class AlterEnumType:
    def __init__(
        self, name: str, old_labels: Sequence[str], new_labels: Sequence[str], schema: str | None = None
    ) -> None: ...

class DropEnumType:
    def __init__(self, name: str, labels: Sequence[str], schema: str | None = None) -> None: ...
```

Типы `ENUM` колонок [`NativeEnumField`](../dialects/postgresql/fields.ru.md#nativeenumfield). Как и
расширения, они не хранятся в состоянии: автоматическое создание миграций выводит их из полей
(`field.requires_enum_type`) при каждом сравнении двух состояний — `CreateEnumType` перед первой колонкой
типа, `AlterEnumType`, когда меняются его метки, `DropEnumType` после того, как ушла его последняя
колонка. Тип определяется схемой и именем: `schema` — это `type_schema` поля, None — схема подключения
по умолчанию, поэтому одноимённые типы двух схем — два разных типа. Два поля,
называющие один тип с разными метками, дают `ConfigurationError`. Enum такого поля
миграция объявляет сама (`Mood = Enum("Mood", {...})`) — с теми членами, какие есть на момент её записи,
а не импортирует класс: член, добавленный в класс позже, — изменение, которое увидит следующий
`makemigrations`.

- `AlterEnumType` добавляет новые метки на месте (`ALTER TYPE ... ADD VALUE ... BEFORE ...`), пока старые
  метки сохраняют порядок. Удалённая или переставленная метка заменяет тип одной командой: тип
  переименовывается, создаётся новый, каждая колонка этого типа (и каждая колонка-массив) преобразуется
  через текст вместе со значением по умолчанию, старый тип удаляется — строка, в которой ещё есть
  удалённая метка, останавливает это ошибкой, поэтому обновите такие строки в более ранней миграции.
  Откат выполняет обратное изменение.
- `DropEnumType` при откате снова создаёт тип с `labels`; `CreateEnumType` при откате удаляет его.
- На базе без типов `ENUM` (`features.supports_enum_types` ложно) все три пропускаются с предупреждением
  — файл миграции выполняется на любой базе.

## <a id="constraints-and-triggers"></a>Индексы, ограничения и триггеры как объекты

Индексы, ограничения и триггеры для миграций — одно и то же: именованный объект схемы у модели.
`AddIndex`/`AddConstraint`/`AddTrigger`, операции `Remove*`, `Rename*` и `AlterTrigger` — тонкие
подклассы четырёх общих операций (`AddSchemaObject`, `RemoveSchemaObject`, `RenameSchemaObject`,
`AlterSchemaObject` в `hare.migrations.operations.schema_objects`), а автоопределение изменений
сравнивает все три вида одним сравнением.

`Index`/`PartialIndex` описаны в разделе [Индексы](../models/indexes.ru.md), а
`GinIndex`/`GistIndex`/`BrinIndex`/`BloomIndex`/`HashIndex`/`SpGistIndex` — в разделе [Типы индексов
PostgreSQL](../dialects/postgresql/indexes-and-tables.ru.md#postgresql-index-types).

`UniqueConstraint`, `CheckConstraint`, `ExclusionConstraint` и `Trigger` описаны в разделах
[Ограничения](../models/constraints-and-triggers.ru.md#constraints) и
[Триггеры](../models/constraints-and-triggers.ru.md#triggers): те же классы данных, что используются в
`Meta.constraints`/`Meta.triggers`, передаются в `AddConstraint`/`AddTrigger`, когда миграция пишется
вручную.

## <a id="schema-objects"></a>Представления, функции, последовательности и доступ

```python
class AddView:
    def __init__(self, model_name: str, view: View) -> None: ...

class AlterView:
    def __init__(self, model_name: str, view: View) -> None: ...

class RemoveView:
    def __init__(self, model_name: str, name: str) -> None: ...

class RenameView:
    def __init__(self, model_name: str, old_name: str, new_name: str) -> None: ...

class AddMaterializedView:
    def __init__(self, model_name: str, view: MaterializedView) -> None: ...

class AlterMaterializedView:
    def __init__(self, model_name: str, view: MaterializedView) -> None: ...

class RemoveMaterializedView:
    def __init__(self, model_name: str, name: str) -> None: ...

class RenameMaterializedView:
    def __init__(self, model_name: str, old_name: str, new_name: str) -> None: ...

class RefreshMaterializedView:
    def __init__(self, model_name: str, name: str, concurrently: bool = False) -> None: ...

class AddFunction:
    def __init__(self, model_name: str, function: DatabaseFunction) -> None: ...

class AlterFunction:
    def __init__(self, model_name: str, function: DatabaseFunction) -> None: ...

class RemoveFunction:
    def __init__(self, model_name: str, name: str) -> None: ...

class RenameFunction:
    def __init__(self, model_name: str, old_name: str, new_name: str) -> None: ...

class AddSequence:
    def __init__(self, model_name: str, sequence: DatabaseSequence) -> None: ...

class AlterSequence:
    def __init__(self, model_name: str, sequence: DatabaseSequence) -> None: ...

class RemoveSequence:
    def __init__(self, model_name: str, name: str) -> None: ...

class RenameSequence:
    def __init__(self, model_name: str, old_name: str, new_name: str) -> None: ...

class AlterRowLevelSecurity:
    def __init__(self, model_name: str, row_level_security: RowLevelSecurity | None) -> None: ...

class AddPolicy:
    def __init__(self, model_name: str, policy: Policy) -> None: ...

class AlterPolicy:
    def __init__(self, model_name: str, policy: Policy) -> None: ...

class RemovePolicy:
    def __init__(self, model_name: str, name: str) -> None: ...

class RenamePolicy:
    def __init__(self, model_name: str, old_name: str, new_name: str) -> None: ...

class AddGrant:
    def __init__(self, model_name: str, grant: Grant) -> None: ...

class RemoveGrant:
    def __init__(self, model_name: str, grant: Grant) -> None: ...
```

Операции над тем, что модель объявляет в `Meta.views`, `Meta.materialized_views`, `Meta.functions`,
`Meta.sequences`, `Meta.row_level_security`, `Meta.policies` и `Meta.grants`, — сами объявления, что
каждое изменение делает в базе и в каком порядке `makemigrations` пишет операции, описано в разделе
[Представления, функции, последовательности и доступ](../models/schema-objects.ru.md). Как операции
индексов, ограничений и триггеров, они хранят объект в состоянии миграций модели: операция `Add` кладёт
его туда, `Alter` заменяет версию с тем же именем, `Rename` переименовывает, а `Remove` находит его там
по имени, — поэтому каждая из них обратима: при откате объект создаётся, восстанавливается или
переименовывается таким, каким его держало предыдущее состояние. `AlterRowLevelSecurity` при откате
возвращает прежнюю настройку. У права нет имени: `RemoveGrant` принимает право целиком и находит в
состоянии равное ему.

`AddView`/`AddMaterializedView`, написанные вручную, принимают и QuerySet
(`View("paid", query= Invoice.objects.filter(paid=True))`): состояние миграций хранит его SQL, а операция
выполняет его как SQL подключения, на котором идёт миграция. Файл миграции, который пишут
`makemigrations` или `squashmigrations`, содержит этот SQL как `RawSQLTerm(...)`.

`RefreshMaterializedView` заново наполняет материализованное представление строками его запроса —
например, после миграции данных, изменившей читаемые им строки. Состояние она не меняет, а при откате
ничего не делает. `concurrently=True` оставляет представление доступным для чтения во время обновления и
требует его `unique_columns` (иначе `ConfigurationError` до отправки SQL).

На базе без такого вида объектов (`features.supports_views`, `supports_materialized_views`,
`supports_database_functions`, `supports_sequences`, `supports_row_level_security`, `supports_grants`)
каждая операция вызывает `UnSupportedError` до отправки SQL — и в `sqlmigrate` тоже. `sqlmigrate`
показывает команды, которые каждая операция выполняет в PostgreSQL:

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
class AddDictionary:
    def __init__(self, model_name: str, dictionary: Dictionary) -> None: ...

class AlterDictionary:
    def __init__(self, model_name: str, dictionary: Dictionary) -> None: ...

class RemoveDictionary:
    def __init__(self, model_name: str, name: str) -> None: ...

class RenameDictionary:
    def __init__(self, model_name: str, old_name: str, new_name: str) -> None: ...

class SynchronizeKeySeries:
    def __init__(self, model_name: str) -> None: ...
```

Операции со словарями работают с тем, что модель объявляет в `Meta.dictionaries`, — см. [Словари
ClickHouse](../dialects/clickhouse/schema-objects.ru.md#dictionaries). `SynchronizeKeySeries` переводит
последовательность чисел, из которой берётся генерируемый ключ модели ClickHouse, за наибольший ключ её
таблицы — см. [Ключи ClickHouse](../dialects/clickhouse/models.ru.md#keys).
