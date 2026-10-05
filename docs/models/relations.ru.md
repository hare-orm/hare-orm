# Связи

Поля связей — `ForeignKeyField`, `OneToOneField`, `ManyToManyField` и `GenericForeignKeyField`: что связь
читает и записывает с каждой из сторон, как загружаются связи, связи с моделями с составным первичным
ключом и с заменяемыми моделями.

## <a id="foreignkeyfield"></a>`ForeignKeyField`

```python
def ForeignKeyField(
    to: type[TModel] | str | SwappableModelReference,
    related_name: str | None | Literal[False] = None,
    on_delete: OnDelete = OnDelete.CASCADE,
    db_constraint: bool = True,
    null: bool = False,
    *,
    to_field: str | tuple[str, ...] | None = None,
    lazy: RelationLoadStrategy | None = None,
    **kwargs: Any,
) -> ForeignKeyRelation[TModel] | ForeignKeyNullableRelation[TModel]: ...
```

`db_index` передаётся именованным аргументом, вместе с [аргументами базового
`Field`](field-types.ru.md#the-base-field-class).

```python
class Book(Model):
    author = fields.ForeignKeyField("models.Author", related_name="books", on_delete=fields.CASCADE)
```

Ключевая колонка (`author_id`; у составного ключа — по одной на каждую его колонку) стоит среди колонок
таблицы там, где связь объявлена среди полей модели.

| Аргумент | Что задаёт |
|---|---|
| `to` | Связанная модель: класс, строка `"app.Model"` или `swappable("SETTING")` (см. [Заменяемые модели](#swappable-models)). |
| `related_name` | Имя обратной ссылки на связанной модели — см. [`related_name`](#related-name) |
| `on_delete` | Что делать при удалении связанной строки — см. ниже. |
| `to_field` | Поле связанной модели, на которое ссылается ключ (по умолчанию — её первичный ключ) — см. [`to_field`](#to-field) |
| `db_constraint` | Создавать ли в базе настоящее ограничение `FOREIGN KEY` (по умолчанию `True`). Для связи с моделью, приложение которой работает через другое подключение, нужно `False`: ограничение не может связывать две разные базы, иначе `Hare.init()` даёт `ConfigurationError`. |
| `db_index` | Индексировать ли колонку (колонки) ключа (у `ForeignKeyField` по умолчанию `True`) — см. [Индекс по колонке ключа](#index-on-the-key-column). |
| `lazy` | `RelationLoadStrategy.JOINED` (как будто всегда вызван `select_related`) или `.SELECT` (как будто всегда вызван `prefetch_related`) — см. [`lazy`](#lazy) |

### <a id="related-name"></a>`related_name`

Имя обратной ссылки на связанной модели. `False` — обратной ссылки нет (нет и обратного фильтра, и имени
для `prefetch_related()`), но `on_delete` по-прежнему действует на ссылающиеся строки, как и с именем;
`None` — имя создаётся автоматически. Имя, уже занятое атрибутом `Model` (`save`, `filter`, `pk` и т. п.)
или атрибутом связанной модели, даёт `ConfigurationError`.

### <a id="to-field"></a>`to_field`

Поле связанной модели, на которое ссылается ключ (по умолчанию — её первичный ключ). На связанную строку,
у которой значение `to_field` равно `NULL`, не ссылается ни одна строка: её обратная ссылка,
`prefetch_related()` и каскадное удаление никогда не находят строки с внешним ключом `NULL`, а создание
строки через такую обратную ссылку (`.create()`) даёт `QueryError`, а не вставляет строку без связи. Если
первичный ключ связанной модели — `OneToOneField(primary_key=True)`, внешний ключ ссылается на
собственную колонку этого поля (например, `user_id`).

### <a id="lazy"></a>`lazy`

`RelationLoadStrategy.JOINED` (как будто всегда вызван `select_related`) или `.SELECT` (как будто всегда
вызван `prefetch_related`); по умолчанию `None` — ничего заранее не загружается. Любая стратегия
загружает один уровень: запрос предзагрузки не применяет `lazy=RelationLoadStrategy.SELECT` самой
связанной модели (поэтому связь модели с самой собой останавливается после одного шага, даже если в
данных есть цикл) — более глубокие уровни указывайте явно, например `prefetch_related("mentor__mentor")`.
`refresh_from_db()` снова загружает обновлённую связь с любой из стратегий.

### <a id="on-delete"></a>`on_delete`

```python
class OnDelete(StrEnum):
    CASCADE = "CASCADE"
    RESTRICT = "RESTRICT"
    SET_NULL = "SET_NULL"     # требует null=True
    SET_DEFAULT = "SET_DEFAULT"  # требует db_default (или default при db_constraint=False) — см. ниже
    NO_ACTION = "NO_ACTION"
    PROTECT = "PROTECT"       # проверяется в Python; в базе — отложенное ограничение NO ACTION как страховка
```

У `PROTECT` нет своего ключевого слова в SQL: hare проверяет его перед отправкой `DELETE`, а в базе
ограничение записывается как `ON DELETE NO ACTION DEFERRABLE INITIALLY IMMEDIATE`. Обычный `delete()` или
`QuerySet.delete()`, каскад которого доходит до связи с `PROTECT`, откладывает эти ограничения только на
время своего `DELETE` (в PostgreSQL — `SET CONSTRAINTS <names> DEFERRED`, а сразу после — `IMMEDIATE`, в
той же транзакции, где идёт удаление). Поэтому защищающая строка, которую тот же каскад удаляет вместе с
защищаемой, удалению не мешает, а защищающая строка вне каскада по-прежнему его останавливает. В
остальной части транзакции проверки остаются немедленными: после записи не остаётся отложенных событий
триггеров, поэтому `ALTER TABLE` или `TRUNCATE` позже в той же транзакции (например, в миграции,
выполняемой целиком в транзакции) работают, а ваши собственные отложенные ограничения сохраняют свой
порядок проверки. SQLite проверяет ограничение в конце всей команды, и откладывать ничего не нужно.
Исключение — удаление, которое выполняет каскад в Python (несколько команд): оно идёт с
`PRAGMA defer_foreign_keys` и проверяет затронутые таблицы, прежде чем выключить его.

Удаление, которое запрещает `RESTRICT` или `NO_ACTION`, на любой базе даёт `IntegrityError`. Текст
сообщения — самой базы там, где ограничение проверяет база (PostgreSQL называет ограничение и таблицу,
SQLite пишет только `FOREIGN KEY constraint failed`), и hare
(`Cannot delete P because C.p has on_delete=RESTRICT and live rows point at it`) там, где проверяет сам
hare: у связи с `db_constraint=False` и у любой связи на ClickHouse.

Сокращения `CASCADE`, `RESTRICT`, `SET_NULL`, `SET_DEFAULT`, `NO_ACTION`, `PROTECT` можно импортировать
прямо из `hare.fields`.

`PROTECT` даёт `hare.exceptions.ProtectedError` (с `.protected_objects: list[Model]`), если вы пытаетесь
удалить строку, на которую ещё что-то ссылается. Проверка и удаление атомарны — отдельно проверять
существование ссылок перед удалением не нужно:

```python
class Loan(Model):
    book = fields.ForeignKeyField("models.Book", null=True, on_delete=fields.PROTECT)

try:
    await book.delete()
except ProtectedError as error:
    raise ValidationError(f"{len(error.protected_objects)} loan(s) still reference this book")
```

`QuerySet.filter(...).delete()` (массовое удаление) выполняет ту же проверку `PROTECT` — один раз для
всего найденного набора, а не для каждой строки, — прежде чем что-либо удалить.

Проверка идёт и по цепочкам `CASCADE`: удаление строки даёт `ProtectedError`, если каскад удалил бы
строку, которую ещё защищает связь с `PROTECT`, сколько бы шагов `CASCADE` до неё ни было (и для моделей
с `Meta.soft_delete_field` тоже). В `protected_objects` лежат защищающие строки; защищающая строка,
которую тот же каскад тоже удаляет, не считается. Модель, в цепочке `CASCADE` которой нигде нет
`PROTECT`, за эту проверку ничем не платит.

Чтобы заранее узнать, что удаление затронет каскадом, обнулит или что его остановит, — ничего не удаляя,
— используйте [`instance.delete_preview()`](model-methods.ru.md#delete-preview).

### <a id="set-default-needs-db-default"></a>`on_delete=SET_DEFAULT` и `db_default`

С настоящим ограничением внешнего ключа (`db_constraint=True`, так по умолчанию) команды создания таблицы
содержат `ON DELETE SET DEFAULT`, и при удалении связанной строки колонку сбрасывает **сама база**. Она
сбрасывает её в значение `DEFAULT` колонки в SQL, а единственное, что hare записывает в `DEFAULT`
колонки, — это `db_default`. Значение `default=` на стороне Python hare подставляет перед `INSERT`, и в
команды создания таблицы оно не попадает. Без `db_default` база молча сбросила бы колонку в `NULL` (или
остановила бы всё удаление, если колонка `NOT NULL`), а не в `default=`, тогда как каскад в Python
применил бы `default=`. Чтобы оба пути вели себя одинаково, поле без `db_default` отклоняется ещё при
объявлении (`ConfigurationError`):

```python
class Book(Model):
    # Строка с id=1 — заглушка «неизвестный автор», на которую переходят книги без автора.
    author = fields.ForeignKeyField("models.Author", on_delete=fields.SET_DEFAULT, db_default=1)
```

Указать здесь `default=` рядом с `db_default=` можно, и предупреждения `RedundantDbDefaultWarning` не
будет: база действительно читает `db_default` при удалении.

При `db_constraint=False` ограничения в базе нет, и колонку сбрасывает только каскад hare в Python. Он
сначала берёт `default=`, а если его нет — `db_default` (как выражение SQL), поэтому достаточно любого из
двух. Тот же каскад в Python выполняется и для связанной модели с `Meta.soft_delete_field`. У внешнего
ключа на составной ключ одно и то же значение `db_default` копируется во все его колонки и не может быть
разным для разных частей, поэтому разные значения для частей требуют `db_constraint=False` и `default=` в
виде кортежа.

Поля, объявленные внутри файла миграции, не проверяются: они описывают схему, которая уже применена.
Чтобы задать такой колонке `DEFAULT`, добавьте в модель `db_default` и создайте новую миграцию — она
задаст `DEFAULT` колонке существующей таблицы.

### <a id="index-on-the-key-column"></a>Индекс по колонке ключа

`ForeignKeyField` по умолчанию создаёт индекс по своей колонке ключа: фильтр по связи, соединения таблиц,
`prefetch_related()` и каждое каскадное удаление — и собственный `ON DELETE` базы, и каскад hare в Python
— ищут строки по этой колонке, и без индекса каждый такой поиск просматривает всю таблицу.

```python
class Review(Model):
    reader = fields.ForeignKeyField("models.Reader", related_name="reviews")  # покрыт индексом UniqueConstraint
    book = fields.ForeignKeyField("models.Book", related_name="reviews")  # свой индекс
    approved_by = fields.ForeignKeyField("models.Reader", related_name="approved_reviews", db_index=False)  # без индекса

    class Meta:
        constraints = [UniqueConstraint(fields=("reader", "book"))]
```

- `db_index=False` отключает индекс.
- Отдельный индекс не создаётся, если индекс, уже объявленный в модели, начинается с колонок ключа (в
  любом порядке): обычная запись `Meta.indexes` (`Index(fields=...)`, уникальный или нет), безусловное
  `UniqueConstraint` или составной первичный ключ. Частичный индекс, индекс по выражению и индекс не вида
  «дерево» (btree) не считаются — кроме безымянного индекса ровно по колонкам ключа: он получает то же
  сгенерированное имя, что и собственный индекс связи, поэтому индекс связи не создаётся. Задайте одному
  из них `name=`, чтобы получить оба.
- Связь с [составным первичным ключом](#targeting-a-composite-primary-key) получает один индекс по всем
  своим колонкам ключа.
- С `db_constraint=False` индекс остаётся: каскад hare ищет строки по этой колонке.
- `OneToOneField` отдельного индекса не получает: его ограничение `UNIQUE` уже индексирует колонку.
- Индекс называется так же, как собственный индекс любого поля: `idx_<table>_<column>_<hash>` — первые 11
  символов имени таблицы, первые 7 символов имени колонки и хэш из 12 символов.

## <a id="onetoonefield"></a>`OneToOneField`

Аргументы те же, что у `ForeignKeyField`; внутри поле всегда получает `unique=True`. Первичным ключом
модели (`primary_key=True`) может быть только `OneToOneField` — `ForeignKeyField(..., primary_key=True)`
даёт `ConfigurationError`. Так же и `ForeignKeyField(..., unique=True)` даёт `ConfigurationError`: связь,
в которой на каждый связанный объект приходится не больше одной строки, — это `OneToOneField`. Его
ограничение `UNIQUE` уже индексирует колонку, поэтому `db_index` по умолчанию выключен. У модели,
первичный ключ которой — это поле, `pk` в фильтре и есть эта связь: подходят и `filter(pk=account)`, и
`filter(pk=1)`, а `values("pk")` даёт ключ.

```python
def OneToOneField(
    to: type[TModel] | str | SwappableModelReference,
    related_name: str | None | Literal[False] = None,
    on_delete: OnDelete = OnDelete.CASCADE,
    db_constraint: bool = True,
    null: bool = False,
    *,
    to_field: str | tuple[str, ...] | None = None,
    lazy: RelationLoadStrategy | None = None,
    **kwargs: Any,
) -> OneToOneRelation[TModel] | OneToOneNullableRelation[TModel]: ...
```

## <a id="manytomanyfield"></a>`ManyToManyField`

```python
def ManyToManyField(
    to: type[TModel] | str | SwappableModelReference,
    through: str | type[Model] | SwappableModelReference | None = None,
    forward_key: str | None = None,
    backward_key: str = "",
    related_name: str = "",
    on_delete: OnDelete = OnDelete.CASCADE,
    db_constraint: bool = True,
    unique: bool = True,
    *,
    lazy: Literal[RelationLoadStrategy.SELECT] | None = None,
    **kwargs: Any,
) -> ManyToManyRelation[TModel]: ...
```

`db_index` передаётся именованным аргументом.

```python
class Book(Model):
    tags = fields.ManyToManyField("models.Tag", related_name="books")
```

| Аргумент | Что задаёт |
|---|---|
| `through` | Имя промежуточной таблицы (по умолчанию создаётся автоматически) или настоящий класс `Model` — для промежуточной таблицы со своими дополнительными колонками (см. ниже) — см. [`through`](#through) |
| `forward_key` / `backward_key` | Имена колонок промежуточной таблицы (по умолчанию создаются автоматически). |
| `unique` | Создаёт индекс `UNIQUE` по `(backward_key, forward_key)`; `False` разрешает повторяющиеся строки для одной и той же пары. |
| `db_index` | Индексирует колонки ключей автоматической промежуточной таблицы, с которых не начинается её индекс `UNIQUE` (по умолчанию `True`): колонки `forward_key`, а при `unique=False` — ещё и колонки `backward_key` — см. [`db_index`](#db-index) |
| `on_delete` | Допустимы `CASCADE`/`RESTRICT`/`NO_ACTION`/`PROTECT`/`SET_NULL` — см. [`on_delete`](#on-delete-row) |
| `lazy` | Только `.SELECT` или `None` — без `.JOINED`: соединение через промежуточную таблицу размножило бы строки. Полный `refresh_from_db()` загружает связь снова. |

### <a id="through"></a>`through`

Имя промежуточной таблицы (по умолчанию создаётся автоматически) или настоящий класс `Model` — для
промежуточной таблицы со своими дополнительными колонками (см. ниже). Если автоматическое имя уже занято
промежуточной таблицей другого поля «многие-ко-многим» или собственной таблицей какой-то модели
(например, два `ManyToManyField` между одной и той же парой моделей), будет `ConfigurationError` —
задайте `through=` явно у одного из них.

### <a id="db-index"></a>`db_index`

Индексирует колонки ключей автоматической промежуточной таблицы, с которых не начинается её индекс
`UNIQUE` (по умолчанию `True`): колонки `forward_key`, а при `unique=False` — ещё и колонки
`backward_key`. `db_index=False` отключает это. Промежуточная таблица-`Model` индексирует свои
`ForeignKeyField` [обычным образом](#index-on-the-key-column).

### <a id="on-delete-row"></a>`on_delete`

Допустимы `CASCADE`/`RESTRICT`/`NO_ACTION`/`PROTECT`/`SET_NULL`. `SET_DEFAULT` недопустим: колонки
промежуточной таблицы всегда `NOT NULL` и без значения по умолчанию, сбрасывать не во что, — он даёт
`ConfigurationError`. Если `through` — это `Model`, `on_delete` поля переносится на собственные внешние
ключи промежуточной модели (если у них оставлен `CASCADE`), а `SET_DEFAULT` принимается, только если оба
этих внешних ключа выполняют [правило `SET_DEFAULT`](#set-default-needs-db-default).

### <a id="many-to-many-across-connections"></a>Связь с моделью на другом подключении

`ManyToManyField` к модели на другом подключении (`db_constraint=False`) держит промежуточную таблицу на
подключении модели, которая объявляет поле, и ни один запрос не соединяет таблицы двух подключений:

- `book.tags` сначала читает связанные ключи из промежуточной таблицы, затем запрашивает `Tag` на его
  подключении с `pk__in=` по ним — при `await`, `async for`, `count()`, `exists()`, `first()`, `get()`,
  `aggregate()`, `in_bulk()`, `update()`, `delete()` и в querysets, которые из неё делают `filter()`/
  `order_by()`/`values_list()` и срез; область видимости `Tag` по умолчанию (мягкое удаление, арендатор)
  применяется там как обычно. Операция над множествами таких querysets (`union()`) даёт `QueryError`.
- `add()`, `remove()`, `set()`, `clear()`, `create()`, `get_or_create()` и `update_or_create()` читают и
  пишут строки каждой модели на её подключении — строки `Tag`, вместе с его областью видимости и
  арендатором, на подключении `Tag`, — поэтому это не одна транзакция на две базы.
- Модель, объявляющая поле, фильтрует только по связям: `Book.objects.filter(tags=tag)`,
  `tags__in=[...]`, `tags__id=`, `tags__isnull=...` — и для составного ключа — и `tag.books`, который и
  есть такой фильтр. Область видимости `Tag` там прочитать нельзя, поэтому связь с мягко удалённым тегом
  по-прежнему подходит. Фильтр, которому нужно поле `Tag` (`tags__name=...`) или запрос к нему
  (`tags__in=Tag.objects.filter(...)`), и `Tag.objects.filter(books=...)` дают `QueryError`.
- `prefetch_related("tags")` запрашивает каждое подключение отдельно.

### <a id="a-model-as-through"></a>Промежуточная таблица-модель

Передайте настоящую модель вместо имени таблицы, если самой связи нужны дополнительные колонки (время
`added_at`, роль автора в книге `role` и т. п.):

```python
class Credit(Model):
    book = fields.ForeignKeyField("models.Book")
    author = fields.ForeignKeyField("models.Author")
    role = fields.CharField(max_length=32, default="author")

    class Meta:
        constraints = [UniqueConstraint(fields=("book", "author"))]


class Book(Model):
    contributors = fields.ManyToManyField("models.Author", through=Credit, related_name="credited_books")
```

Промежуточная модель — обычная модель: к ней можно обращаться напрямую
(`Credit.objects.filter(role="translator")`), как к любой другой таблице, в дополнение к обычным
`.add()`/`.remove()`/`.clear()` у самой связи. hare управляет её таблицей и миграциями так же, как
автоматической промежуточной таблицей. Оба собственных внешних ключа промежуточной модели должны
ссылаться на первичный ключ своей модели — `to_field=`, указывающий на другую колонку, даёт
`ConfigurationError` при `Hare.init()`.

Дополнительные поля промежуточной модели в новых строках задаёт `add(..., through_defaults=...)`:

```python
await book.contributors.add(author, through_defaults={"role": "translator"})
```

Они применяются только к строкам, которые этот вызов действительно вставляет, — уже существующая пара
остаётся как есть. `through_defaults` не имеет смысла (даёт `QueryError`) без настоящего `through=Model`,
для имени поля, которого нет в промежуточной модели, и для её собственных внешних ключей, из которых
состоит сама связь. Поле промежуточной модели, которое ничто не заполняет, — не допускающее NULL, без
значения по умолчанию, не генерируемое — нужно задать в `through_defaults`: иначе `add()` даёт
`ValidationError` до записи, как `create()`. Промежуточная модель, ключи которой база выдаёт из серии до
записи строк (ClickHouse), берёт из неё ключи новых строк, как `create()`.

Если у промежуточной модели есть
[`Meta.tenant_field`](../soft-delete-versions-tenants/multi-tenancy.ru.md), `add()` заполняет его
значением области промежуточной модели из `Tenancy.scope()`, если его не задаёт `through_defaults`, — при
области из нескольких значений задать его обязан `through_defaults`; арендатор в `through_defaults` вне
этой области даёт `QueryError`.

### <a id="relation-operations"></a>Операции со связью

`ManyToManyField` читает и пишет свои ссылки через эти методы связи:

```python
class ManyToManyRelation(RelatedQuerySet):
    async def add(
        self,
        *objs: Any,
        through_defaults: dict[str, Any] | None = None,
        using: str | DatabaseClient | None = None,
    ) -> None: ...

    async def remove(self, *objs: Any, using: str | DatabaseClient | None = None) -> None: ...

    async def clear(
        self,
        using: str | DatabaseClient | None = None,
        *,
        all_tenants: bool = False,
    ) -> None: ...

    async def set(
        self,
        *objs: Any,
        through_defaults: dict[str, Any] | None = None,
        clear: bool = False,
        using: str | DatabaseClient | None = None,
    ) -> None: ...

    async def create(
        self,
        *,
        using: str | DatabaseClient | None = None,
        through_defaults: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> TModel: ...

    async def get_or_create(
        self,
        defaults: dict[str, Any] | None = None,
        *,
        through_defaults: dict[str, Any] | None = None,
        using: str | DatabaseClient | None = None,
        **kwargs: Any,
    ) -> tuple[TModel, bool]: ...

    async def update_or_create(
        self,
        defaults: dict[str, Any] | None = None,
        create_defaults: dict[str, Any] | None = None,
        *,
        through_defaults: dict[str, Any] | None = None,
        using: str | DatabaseClient | None = None,
        **kwargs: Any,
    ) -> tuple[TModel, bool]: ...
```

```python
tag = await book.tags.create(name="new")   # создаёт Tag и добавляет его, в одной транзакции
await book.tags.add(tag_a, tag_b)
await book.tags.remove(tag_a)
await book.tags.set(tag_b, tag_c)   # связь с tag_a удаляется, с tag_b остаётся как есть, tag_c добавляется
await book.tags.set([tag_b, 7])     # один итерируемый объект — объекты или значения первичного ключа
await book.tags.set(Tag.objects.filter(name__startswith="sci"))
await book.tags.clear()
tag, created = await book.tags.get_or_create(name="science")
```

`add()`, `remove()` и `set()` принимают связанные объекты или значения их первичного ключа (кортеж для
составного ключа). `add()` читает строки по этим значениям через запрос связанной модели по умолчанию и
даёт `IntegrityError` для значения, которому не соответствует ни одна строка; `remove()` по значению
отвязывает только строки, которые этот запрос показывает. `set()` принимает также один итерируемый или
ожидаемый (`await`) объект — список, множество, запрос, `values_list(..., flat=True)` ключей, другую
связь. `get_or_create()`/`update_or_create()` ищут только среди уже связанных строк; новый объект
создаётся и добавляется в той же транзакции (существующая строка, которая не входит в связь, создаётся
заново — тогда уникальный ключ даст `IntegrityError`).

`set()` в одной транзакции делает `objs` составом связи: удаляет связи, которых нет в `objs` (помечая их
промежуточные строки удалёнными, если у промежуточной модели есть `Meta.soft_delete_field`), и добавляет
новые — с `through_defaults`, как `add()`. Промежуточные строки оставшихся связей не меняются, вместе с
дополнительными полями. `clear=True` сначала очищает всю связь и заново добавляет все `objs`. `set()` без
аргументов очищает связь, как `clear()`.

`clear()` и `set()` удаляют только связи со строками, которые показывает запрос связанной модели по
умолчанию: связь с мягко удалённой строкой, со строкой другого арендатора (`Meta.tenant_field`) или со
строкой, которую скрывает свой `Meta.manager`, остаётся. `clear(all_tenants=True)` удаляет связи со
строками всех арендаторов, без выбора текущего арендатора (см. [Разделение данных по
арендаторам](../soft-delete-versions-tenants/multi-tenancy.ru.md)). `remove()` называет строки явно,
поэтому отвязывает и мягко удалённую строку, и скрытую `Meta.manager` (строка другого арендатора всё
равно отклоняется).

`add()` проверяет по базе, что владелец связи не помечен удалённым и что каждую добавляемую строку
показывает запрос связанной модели по умолчанию: строка, которую тем временем мягко удалили (объект в
памяти устарел) или которую скрывает её `Meta.manager`, даёт `IntegrityError`, и ничего не связывается.

Где уже связанную пару не пропускает уникальный индекс — таблица модели `through=`, база без уникальности
(ClickHouse), — `add()` и `set()` читают, какие пары уже связаны, и вставляют остальные; внутри одного
процесса они удерживают эти пары от чтения до вставки, поэтому одновременные вызовы связывают каждую
пару один раз. Другой процесс может связать её в промежутке: где это важно, объявите пару уникальной в
модели связи.

Пара, строка которой в промежуточной таблице мягко удалена (`remove()` у промежуточной модели с
`Meta.soft_delete_field`), связью не считается: `add()` и `set()` добавляют её снова новой строкой. Если
промежуточная модель объявляет пару уникальной — `UniqueConstraint` без `condition` по двум её связям, с
`Meta.tenant_field` или без него, — новую строку записать нельзя, поэтому вместо этого восстанавливается
мягко удалённая строка, с переданными `through_defaults`.

## <a id="genericforeignkeyfield"></a>`GenericForeignKeyField`

Связь со строкой одной из нескольких моделей — комментарий к книге, автору или изданию. Это **исключающая
дуга настоящих внешних ключей**: каждой цели — своя ветка, `ForeignKeyField`, допускающий `NULL`, с
колонкой, внешним ключом в базе, индексом и обратной связью, а `CHECK` следит, чтобы была заполнена ровно
одна ветка.

```python
class Comment(Model):
    id = fields.IntField(primary_key=True)
    target = fields.GenericForeignKeyField(
        {"book": Book, "author": "catalog.Author", "edition": Edition},
        related_name="comments",
        on_delete=CASCADE,
    )
```

```python
def GenericForeignKeyField(
    to: dict[str, BranchTarget] | SwappableModelReference,
    related_name: str | None = None,
    on_delete: OnDelete = OnDelete.CASCADE,
    db_constraint: bool = True,
    *,
    null: bool = False,
    default: Model | Callable[[], Model] | None = None,
    lazy: RelationLoadStrategy | None = None,
    # kwargs передаются каждой ветке: db_index, sensitive, description
    **kwargs: Any,
) -> GenericForeignKeyFieldInstance[Any]: ...
```

`to` сопоставляет **имени ветки** её цель, заданную так же, как `to` у `ForeignKeyField`: классом,
`"app.Model"` или `swappable("SETTING")`. Имя ветки — публичное имя цели: оно задаёт колонку ветки
(`book_id`, колонки ключа у составного ключа), её атрибут (`comment.book`) и путь фильтра
(`book__title`), а также `type`, который сообщает поле. От имени класса оно не зависит, поэтому

- две обобщённые связи одной модели могут иметь общую цель (`subject={"subject_reader": Reader}`,
  `actor={"actor_reader": Reader}`);
- два одноимённых класса из разных приложений — две ветки
  (`{"catalog_book": "catalog.Book", "store_book": "store.Book"}`);
- переименование модели-цели не меняет ни колонок, ни `type`; переименование ветки — это `RenameField` её
  колонки и смена `type`, который видит API.

У самого поля нет колонки, и его нет в `Model._meta.fields_map` — там его ветки; список таких полей —
`Model._meta.generic_foreign_key_fields`. `ConfigurationError` при объявлении модели: пустой словарь, имя
ветки не идентификатор Python или занято другим атрибутом модели, одна модель под двумя именами (объект
не определил бы свою ветку), `to_field=` (каждая ветка ссылается на первичный ключ цели),
`on_delete=SET_NULL` без `null=True`, `on_delete=SET_DEFAULT` без `default=`, цель из
`register_live_models()`.

### <a id="generic-foreign-key-check"></a>`CHECK`

Называется `<field>_exclusive_arc` — заполнена ровно одна ветка, с `null=True` — не больше одной; ветка к
составному ключу заполнена, когда заполнены все её колонки, и пуста, когда пусты все, любая смесь не
проходит. PostgreSQL считает заполненные ветки через `num_nonnulls(...)`, SQLite — через
`(book_id IS NOT NULL) + ...`.

### <a id="generic-foreign-key-access"></a>Чтение и присваивание

```python
comment = await Comment.objects.create(target=book)   # заполнен book_id, остальные ветки NULL
await comment.target                                  # книга — читается как внешний ключ
comment.target = author                               # заполнен author_id, book_id очищен
comment.target = None                                 # все ветки очищены — только при null=True
```

Чтение поля даёт связанный объект заполненной ветки — из кэша после
`select_related()`/`prefetch_related()`, иначе запросом, как читается `ForeignKeyField`. Если ни одна
ветка не заполнена, поле равно `None`, когда ветки загружены или присвоены, а иначе это объект, который
можно ждать через `await` и который даёт `None`, — тоже как у внешнего ключа. Присваивание объекта
заполняет его ветку и очищает остальные; объект другой модели даёт ту же `ValidationError`, что и внешний
ключ. Словарь `{"type": "book", "id": 1}` (поля ключа у составного) принимается везде, где принимается
объект, — он заполняет колонки ключа, не загружая строку. Если `create()`/конструктору переданы и поле, и
одна из его веток — `QueryError`.

### <a id="generic-foreign-key-queries"></a>Запросы

| Ключ | Значение |
|---|---|
| `target=obj` / `target__not=obj` | ветка модели `obj` хранит `obj` (подходит и словарь `{"type": ..., <key>}`) |
| `target__in=[...]` / `target__not_in=[...]` | любой из объектов — каждый сравнивается в своей ветке; составной ключ — строкой целиком |
| `target__isnull=True` | ни одна ветка не заполнена |
| `target__type="book"` | заполнена ветка `book`; также `__in`, `__not`, `__not_in` — имя без ветки не совпадает ни с чем |

Ключи работают через связи (`Like.objects.filter(comment__target__type="author")`), в `Q`, `exclude()`,
`get()`, `When()` и в `_filter=` агрегата. `__type` и `__isnull` читают колонку ключа — цель, скрытая
своей областью по умолчанию (мягко удалённая, другого арендатора), всё равно считается заполненной.

- `values("target__type")`, `values_list(...)`, `order_by("target__type")` и `F("target__type")` читают
  имя заполненной ветки (`NULL`, если нет) через `CASE` по веткам.
- `select_related("target")` присоединяет каждую ветку (`LEFT JOIN`), `prefetch_related("target")`
  загружает каждую ветку своим запросом. `Prefetch` с набором строк читает одну модель, поэтому он
  называет ветку (`Prefetch("book", queryset=...)`); имя самого поля даёт `FieldError`, как и поле в
  `bulk_create(on_conflict=...)` — у каждой ветки своя уникальность.
- `related_name` — обратная связь на каждой цели: `book.comments`, `author.comments`.
- `only("target")` загружает ключи всех веток, `defer("target")` их не загружает. Строка, запрос которой
  их не выбрал (`only("id")`), при чтении поля даёт `NoValuesFetched`, как внешний ключ;
  `refresh_from_db(fields=["target"])` их загружает.

### <a id="generic-foreign-key-writes"></a>Запись

Поле принимают `create(target=...)`, `bulk_create()` объектов, созданных с ним, `update(target=...)`
(заполняет его ветку, очищает остальные), `bulk_update(objs, fields=["target"])`,
`bulk_create(..., update_fields=["target"])`, `save(update_fields=["target"])` (колонки ключа всех
веток), `update_from_dict({"target": ...})` и `defaults` у `get_or_create()`/`update_or_create()`.

`UniqueConstraint` или `Index` с полем разворачиваются по одному на ветку, а их имя — если задано —
получает в конце имя ветки: `UniqueConstraint(fields=("reader", "target"), name="one_comment")` — это
`one_comment_book`, `one_comment_author`, ... — пустая ветка (`NULL`) никогда не конфликтует.

### <a id="generic-foreign-key-on-delete"></a>`on_delete`

Каждая ветка получает `on_delete` поля — `CASCADE`, `RESTRICT`, `PROTECT`, `NO_ACTION` действуют на
строки, чья удалённая цель — их. `SET_NULL` требует `null=True`. `SET_DEFAULT` требует `default=` —
сохранённый объект одной из целей или функцию, возвращающую его: hare заполняет эту ветку им и очищает
остальные одним `UPDATE`; внешних ключей в базе у веток тогда нет, как у любой связи, для которой hare
выполняет `on_delete` сам (`db_constraint=False`). Новый объект без заполненной ветки тоже получает
`default`. Мягкое удаление и арендаторы действуют на каждую ветку как на внешний ключ.

### <a id="generic-foreign-key-migrations"></a>Миграции

В состоянии и файлах миграций ветки — обычные `ForeignKeyField`, а `CHECK` — обычное ограничение
(`CheckConstraint(check=ExclusiveArcCondition(("book", "author", "edition")), name="target_exclusive_arc")`);
само поле не записывается, поэтому у модели в `RunPython` есть ветки, но нет `target`. `makemigrations`
даёт:

- новая цель — `AddField` её ветки и замена `CHECK` (`RemoveConstraint` + `AddConstraint`);
- убранная цель — `RemoveField` её ветки и замена `CHECK`;
- ветка переименована при той же цели — `RenameField` её колонки и замена `CHECK`;
- поле удалено — `RemoveField` всех веток и `RemoveConstraint`.

### <a id="generic-foreign-key-swappable"></a>`swappable`

Цель может быть `swappable("USER_MODEL")`, как у внешнего ключа. А `to` может быть настройкой, называющей
все цели сразу, — `to=swappable("COMMENT_TARGETS")` и в конфиге
`"swappable": {"COMMENT_TARGETS": {"book": "catalog.Book", "author": "catalog.Author"}}`: ветки строятся
при `init()` из словаря настройки. Файл миграции записывает ветки, которые дала настройка при его
создании; смена настройки даёт миграции добавленных и убранных веток.

### <a id="generic-foreign-key-pydantic"></a>Pydantic и запросы из параметров HTTP-запроса

`pydantic_model_creator()` ставит поле на место его веток. На выходе это объединение схем целей,
различаемых полем `type` с именем ветки (`{"type": "book", "id": 1, "title": ...}`); на входе
(`exclude_readonly=True`) и с `relations_as_ids=True` — `{"type": "book", "id": 1}`, составной ключ —
полями ключа. В [запросе из параметров HTTP-запроса](../integrations/request-queries.ru.md#meta-filters)
`FilterField("target")` принимает `?target=book:1` (значения составного ключа через запятую:
`edition:7,2`), а `FilterField("target__type")` — одно из имён веток.

## <a id="reverse-access"></a>Обратные ссылки

`ForeignKeyField`/`OneToOneField` сам создаёт обратную ссылку на связанной модели с типом
`BackwardForeignKeyRelation[TModel]`/`BackwardOneToOneRelation[TModel]`; `ManyToManyField` даёт
`ManyToManyRelation[TModel]` с обеих сторон.

Ссылка «ко многим», прочитанная у объекта, — `author.books`, `book.tags` — это **`RelatedQuerySet`**
(`ReverseRelation` для обратного внешнего ключа, `ManyToManyRelation` для «многие-ко-многим»): QuerySet
менеджера связанной модели, отфильтрованный по строкам этого объекта. Это именно QuerySet — отдельного
«менеджера связи» с копией части методов QuerySet нет:

```python
await author.books                                           # все книги автора
await author.books.filter(rating__gte=4).order_by("-rating").limit(5)
await author.books.count()
await author.books.values_list("title", flat=True)
await author.books.published()        # метод собственного подкласса QuerySet связанной модели
async for book in author.books: ...
```

- Работает каждый [метод QuerySet](../querying/queryset-methods.ru.md), с областями видимости связанной
  модели по умолчанию (`Meta.soft_delete_field`, `Meta.tenant_field`, свой `Meta.manager`). Метод,
  меняющий набор возвращаемых строк (`.filter()`, `.order_by()`, ...), даёт обычный QuerySet класса
  менеджера; записи самой связи (`add()`, `remove()`, `set()`, `clear()`, `create()`, ...) остаются у
  связи.
- Если у менеджера связанной модели свой подкласс `QuerySet` (`objects = Manager(BookQuerySet)`), у связи
  есть и его методы.
- Запрос выполняется на подключении, с которого объект загружен или на которое сохранён, если
  маршрутизатор или `.using()` не говорят иначе.
- Связь ещё и **хранит свои строки, когда они загружены** — через `prefetch_related()`,
  [`prefetch_related_objects()`](#prefetch-related-objects), `lazy=` или перебором через `async for`.
  Тогда сама связь перебирается, измеряется и индексируется как список: `for book in author.books`,
  `book in author.books`, `len(author.books)`, `bool(author.books)`, `author.books[0]`. Для незагруженной
  связи это даёт `NoValuesFetched`. Запись через связь сбрасывает загруженное, поэтому, чтобы увидеть
  изменение, загрузите связь снова (или переберите её через `async for`).
- Связь, прочитанную у ни разу не сохранённого объекта, запросить нельзя (`QueryError`): ключа, на
  который могли бы ссылаться связанные строки, ещё нет.

Менеджер обратной связи внешнего ключа тоже записывает данные:

```python
await author.books.create(title="New")                  # внешний ключ указывает на author
await author.books.bulk_create([Book(title="A"), Book(title="B")])  # каждая указывает на author
book, created = await author.books.get_or_create(title="Old", defaults={"year": 1999})
await author.books.update_or_create(title="Old", defaults={"year": 2000})
await author.books.add(book_a, book_b)                   # один UPDATE; bulk=False сохраняет каждый
await author.books.remove(book_a)                        # ставит внешний ключ в NULL
await author.books.clear()                               # все книги автора
await author.books.set([book_b, book_c])                 # аргументы, один итерируемый объект или запрос
```

`add()` при `bulk=True` (по умолчанию) требует сохранённые объекты и даёт `IntegrityError` для строки,
которую не показывает запрос связанной модели по умолчанию; `bulk=False` сохраняет каждый объект отдельно
(выполняются обработчики `save()`, несохранённый объект создаётся). `remove()` и `clear()` есть только у
внешнего ключа, допускающего `NULL` (иначе `QueryError`); `remove()` строки, которая указывает на другой
объект, даёт `DoesNotExist`. `set()` удаляет связи со строками, которых нет в новом составе, и добавляет
новые — для внешнего ключа без `NULL` он только добавляет; `clear=True` сначала очищает. Значение в
`kwargs`, которое направляет внешний ключ на другую строку, а не на родительскую, даёт `QueryError` в
`create()`, `get_or_create()` и `update_or_create()`, как и объект с таким внешним ключом в
`bulk_create()`.

```python
class Book(Model):
    author: fields.ForeignKeyRelation["Author"] = fields.ForeignKeyField("models.Author", related_name="books")

class Author(Model):
    books: fields.ReverseRelation["Book"]  # не объявляется полем — только аннотация типа
```

Псевдонимы для аннотаций типов в `hare.fields`:

```python
OneToOneNullableRelation = OneToOneFieldInstance[TModel] | None
OneToOneRelation = OneToOneFieldInstance[TModel]
ForeignKeyNullableRelation = ForeignKeyFieldInstance[TModel] | None
ForeignKeyRelation = ForeignKeyFieldInstance[TModel]
```

## <a id="forward-access"></a>Прямые ссылки

`book.author` возвращает загруженного `Author`, если связь загружена (`select_related()`,
`prefetch_related()`, `prefetch_related_objects()`, `lazy=` или присваивание), а иначе — запрос, который
можно ждать через `await`: `await book.author` работает в обоих случаях, если связанная строка
существует. Связь без связанной строки (внешний ключ `NULL`, обратный `OneToOneField`, на который никто
не ссылается, или строка, не прошедшая `Select(extra_condition=...)`) читается по-разному в зависимости
от того, загружена ли она. Незагруженная, она возвращает ложный объект `NoneAwaitable` (импортируется из
`hare.models`), а `await book.author` возвращает `None`. Загруженная, она равна обычному `None` —
проверяйте её через `book.author is None` (или смотрите `book.author_id`) и не используйте `await`.

Фильтр или присваивание по объекту читают поле (поля), на которое ссылается связь (`to_field`, по
умолчанию первичный ключ); объект, загруженный через `.only()`/`.defer()` без них, даёт `QueryError` —
как и его обратные ссылки и их `prefetch_related_objects()`.

## <a id="loading-relations"></a>Загрузка связей

Каждый способ загрузить связь делает одно дело:

| Как | Когда | Что выполняется |
|---|---|---|
| [`select_related("author")`](../querying/queryset-methods.ru.md) | Вместе с запросом, для связей «к одному» (внешний ключ, «один-к-одному», обратный «один-к-одному»). | `JOIN` в том же запросе. |
| [`prefetch_related("books", Prefetch(...))`](../querying/queryset-methods.ru.md#prefetch) | Вместе с запросом, для любой связи. | Ещё один запрос на связь для всех строк — для связи «многие-ко-многим» соединённый со связующей таблицей, или два, если связующая таблица читается первой (см. [`prefetch_related()`](../querying/queryset-methods.ru.md)). |
| `lazy=RelationLoadStrategy.JOINED` / `.SELECT` у поля | Всегда, для каждого запроса модели, — объявленное поведение по умолчанию; `defer_related("author")` выключает его для одного запроса. | Тот же `JOIN` / дополнительный запрос. |
| `await book.author`, `await author.books` | По требованию, для одного объекта. | Один запрос для этого объекта. |
| `prefetch_related_objects(objs, ...)` | Позже, для уже имеющихся объектов. | Один запрос на связь для всех объектов. |

### <a id="prefetch-related-objects"></a>`prefetch_related_objects()`

```python
from hare import Prefetch, prefetch_related_objects

async def prefetch_related_objects(
    objs: Iterable[Model],
    *lookups: str | Prefetch,
    using: str | DatabaseClient | None = None,
) -> None: ...
```

Загружает связи уже имеющихся объектов — ровно то, что `prefetch_related()` делает для строк запроса, с
теми же аргументами: путь связи (`"books__reviews"`) или `Prefetch(...)` со своим QuerySet или
`to_attribute`. Один запрос на связь загружает её для всех объектов, сколько бы их ни было; один объект
передаётся списком из одного элемента.

```python
authors = await Author.objects.filter(name__startswith="A")
...
await prefetch_related_objects(authors, "awards", Prefetch("books", Book.objects.filter(in_print=True)))
for author in authors:
    print(len(author.awards), [book.title for book in author.books])

await prefetch_related_objects([book], "author", "tags")   # один объект
```

- `objs` могут быть объектами нескольких моделей, и тогда каждый аргумент начинается со связи каждой
  из них (`[book, profile]` с `"author"`); пустая коллекция ничего не делает.
- Запросы выполняются на подключении, с которого пришёл первый объект, если `using=` не называет другое.
- Аргумент, который не называет связь модели объекта, даёт `FieldError`.

## <a id="targeting-a-composite-primary-key"></a>Связь с составным первичным ключом

`ForeignKeyField`/`OneToOneField` может ссылаться на модель, первичный ключ которой —
[`CompositePrimaryKey`](field-types.ru.md#compositeprimarykey):

```python
class Shelf(Model):
    store = fields.CharField(max_length=20)
    number = fields.IntField()
    pk = CompositePrimaryKey("store", "number")


class Book(Model):
    # ссылается на пару (id, version) настоящим составным FOREIGN KEY на уровне таблицы
    shelf = fields.ForeignKeyField("models.Shelf", related_name="books")
```

hare создаёт по одной колонке на каждую часть первичного ключа связанной модели и настоящее ограничение
`FOREIGN KEY (col1, col2) REFERENCES table (col1, col2)` — а не две независимые колонки без ограничения.
Соединения таблиц, каскады (`CASCADE`/`SET_NULL`/`SET_DEFAULT`/`PROTECT`) и `prefetch_related()` работают
так же, как для ключа из одной колонки.

`to_field=` можно указать и явно, но составной `to_field` принимается, только если **в точности**
совпадает с составным первичным ключом связанной модели, в объявленном порядке; произвольный составной
уникальный набор полей как цель не поддерживается (иначе `ConfigurationError`).

Фильтр через `ManyToManyField`, связанная модель которого имеет составной первичный ключ, тоже работает:
`=`, `__not`, `__in` и `__not_in` сравнивают весь первичный ключ как строку значений
(`(col1, col2) = (v1, v2)`), а не одну колонку.

## <a id="swappable-models"></a>Заменяемые модели

Пакет может поставлять модель по умолчанию, которую проект заменяет своей, — например, модель
пользователя пакета, вместо которой проект использует `accounts.User`. Пакет объявляет модель по
умолчанию с `Meta.swappable` и направляет свои связи не на конкретную модель, а на настройку через
`swappable()`:

```python
from hare import fields
from hare.models import Model, swappable


class BaseUser(Model):
    id = fields.IntField(primary_key=True)
    email = fields.CharField(max_length=255, unique=True)

    class Meta:
        abstract = True


class User(BaseUser):  # модель пакета по умолчанию
    class Meta:
        swappable = "USER_MODEL"


class Consent(Model):
    id = fields.IntField(primary_key=True)
    user = fields.ForeignKeyField(swappable("USER_MODEL"), related_name="consents", null=True)
```

Проект наследует базовую модель и указывает свою модель в разделе `swappable` настроек (см.
[`Hare.init()`](../connections/configuration.ru.md#the-config-dict)):

```python
class User(BaseUser):  # accounts/models.py
    phone = fields.CharField(max_length=20, default="")


await Hare.init(config={**HARE_CONFIG, "swappable": {"USER_MODEL": "accounts.User"}})
```

- `swappable("USER_MODEL")` принимают `ForeignKeyField`, `OneToOneField` и `ManyToManyField` (и его
  `through=` тоже). При `init()` он указывает на модель, выбранную настройкой, — заданную в конфигурации,
  а если её нет, то на модель, объявившую `Meta.swappable = "USER_MODEL"`. Поле хранит саму ссылку на
  настройку: `deconstruct()` возвращает её, поэтому файл миграции хранит настройку, а не модель, на
  которую она указывает.
- Модель по умолчанию, вместо которой настройка выбирает другую, считается **заменённой**:
  `Model._meta.swapped` хранит метку модели, используемой вместо неё (иначе `None`). Таблицы у неё нет —
  `Hare.generate_schemas()` и миграции её пропускают, проверка расхождений её не учитывает, — а каждый
  запрос или запись через неё (`User.objects.all()`, `User.objects.create(...)`) дают
  `ConfigurationError`: `"users.User" has been swapped for "accounts.User" by the USER_MODEL setting`. Её
  связи не регистрируют обратных ссылок, поэтому модель-замена может использовать те же `related_name`.
- Связь, которая называет заменённую модель напрямую (`"users.User"`), даёт `ConfigurationError` при
  `init()` — объявляйте её через `swappable("USER_MODEL")`.
- Автоматическая промежуточная таблица `ManyToManyField(swappable(...))` называется по полю
  (`consent_witnesses`), а не по связанной модели, поэтому не меняется вместе с настройкой.
- Выбирайте настройку в начале проекта: таблицы пакета при создании получают внешние ключи на ту модель,
  которую она называет. Как изменить её позже — см. [Заменяемые модели в
  миграциях](../migrations/migrations.ru.md#swappable-models).
