# Связи

## `ForeignKeyField` {: #foreignkeyfield }

```python
def ForeignKeyField(
    to: type[TModel] | str | SwappableModelReference,
    related_name: str | None | Literal[False] = None,
    on_delete: OnDelete = CASCADE,
    db_constraint: bool = True,
    null: bool = False,
    **kwargs: Any,
) -> ForeignKeyRelation[TModel] | ForeignKeyNullableRelation[TModel]
```

`to_field`, `db_index` и `lazy` передаются именованными аргументами, вместе с
[аргументами базового `Field`](field-types.ru.md#the-base-field-class).

```python
class Book(Model):
    author = fields.ForeignKeyField("models.Author", related_name="books", on_delete=fields.CASCADE)
```

| Аргумент | Что задаёт |
|---|---|
| `to` | Связанная модель: класс, строка `"app.Model"` или `swappable("НАСТРОЙКА")` (см. [Заменяемые модели](#swappable-models)). |
| `related_name` | Имя обратной ссылки на связанной модели. `False` — обратной ссылки нет (нет и обратного фильтра, и имени для `prefetch_related()`), но `on_delete` по-прежнему действует на ссылающиеся строки, как и с именем; `None` — имя создаётся автоматически. Имя, уже занятое атрибутом `Model` (`save`, `filter`, `pk` и т. п.) или атрибутом связанной модели, даёт `ConfigurationError`. |
| `on_delete` | Что делать при удалении связанной строки — см. ниже. |
| `to_field` | Поле связанной модели, на которое ссылается ключ (по умолчанию — её первичный ключ). На связанную строку, у которой значение `to_field` равно `NULL`, не ссылается ни одна строка: её обратная ссылка, `prefetch_related()` и каскадное удаление никогда не находят строки с внешним ключом `NULL`, а создание строки через такую обратную ссылку (`.create()`) даёт `QueryError`, а не вставляет строку без связи. Если первичный ключ связанной модели — `OneToOneField(primary_key=True)`, внешний ключ ссылается на собственную колонку этого поля (например, `user_id`). |
| `db_constraint` | Создавать ли в базе настоящее ограничение `FOREIGN KEY` (по умолчанию `True`). Для связи с моделью, приложение которой работает через другое подключение, нужно `False`: ограничение не может связывать две разные базы, иначе `Hare.init()` даёт `ConfigurationError`. |
| `db_index` | Индексировать ли колонку (колонки) ключа (у `ForeignKeyField` по умолчанию `True`) — см. [Индекс по колонке ключа](#index-on-the-key-column). |
| `lazy` | `RelationLoadStrategy.JOINED` (как будто всегда вызван `select_related`) или `.SELECT` (как будто всегда вызван `prefetch_related`); по умолчанию `None` — ничего заранее не загружается. Любая стратегия загружает один уровень: запрос предзагрузки не применяет `lazy=RelationLoadStrategy.SELECT` самой связанной модели (поэтому связь модели с самой собой останавливается после одного шага, даже если в данных есть цикл) — более глубокие уровни указывайте явно, например `prefetch_related("mentor__mentor")`. `refresh_from_db()` снова загружает обновлённую связь с любой из стратегий. |

### `on_delete` {: #on-delete }

```python
class OnDelete(StrEnum):
    CASCADE = "CASCADE"
    RESTRICT = "RESTRICT"
    SET_NULL = "SET_NULL"     # требует null=True
    SET_DEFAULT = "SET_DEFAULT"  # требует db_default (или default при db_constraint=False) — см. ниже
    NO_ACTION = "NO_ACTION"
    PROTECT = "PROTECT"       # проверяется в Python; в базе — отложенное ограничение NO ACTION как страховка
```

У `PROTECT` нет своего ключевого слова в SQL: hare-orm проверяет его перед отправкой `DELETE`, а в
базе ограничение записывается как `ON DELETE NO ACTION DEFERRABLE INITIALLY IMMEDIATE`. Обычный
`delete()` или `QuerySet.delete()`, каскад которого доходит до связи с `PROTECT`, откладывает эти
ограничения только на время своего `DELETE` (в PostgreSQL — `SET CONSTRAINTS <имена> DEFERRED`, а
сразу после — `IMMEDIATE`, в той же транзакции, где идёт удаление). Поэтому защищающая строка,
которую тот же каскад удаляет вместе с защищаемой, удалению не мешает, а защищающая строка вне
каскада по-прежнему его останавливает. В остальной части транзакции проверки остаются
немедленными: после записи не остаётся отложенных событий триггеров, поэтому `ALTER TABLE` или
`TRUNCATE` позже в той же транзакции (например, в миграции, выполняемой целиком в транзакции)
работают, а ваши собственные отложенные ограничения сохраняют свой порядок проверки. SQLite
проверяет ограничение в конце всей команды, и откладывать ничего не нужно. Исключение — удаление,
которое выполняет каскад в Python (несколько команд): оно идёт с `PRAGMA defer_foreign_keys` и
проверяет затронутые таблицы, прежде чем выключить его.

Сокращения `CASCADE`, `RESTRICT`, `SET_NULL`, `SET_DEFAULT`, `NO_ACTION`, `PROTECT` можно
импортировать прямо из `hare.fields`.

#### `SET_DEFAULT` требует `db_default` {: #set-default-needs-db-default }

С настоящим ограничением внешнего ключа (`db_constraint=True`, так по умолчанию) команды создания
таблицы содержат `ON DELETE SET DEFAULT`, и при удалении связанной строки колонку сбрасывает **сама
база**. Она сбрасывает её в значение `DEFAULT` колонки в SQL, а единственное, что hare-orm
записывает в `DEFAULT` колонки, — это `db_default`. Значение `default=` на стороне Python hare-orm
подставляет перед `INSERT`, и в команды создания таблицы оно не попадает. Без `db_default` база
молча сбросила бы колонку в `NULL` (или остановила бы всё удаление, если колонка `NOT NULL`), а не в
`default=`, тогда как каскад в Python применил бы `default=`. Чтобы оба пути вели себя одинаково,
поле без `db_default` отклоняется ещё при объявлении (`ConfigurationError`):

```python
class Post(Model):
    # Строка с id=1 — заглушка «удалённый пользователь», на которую переходят записи без автора.
    author = fields.ForeignKeyField("models.User", on_delete=fields.SET_DEFAULT, db_default=1)
```

Указать здесь `default=` рядом с `db_default=` можно, и предупреждения
`RedundantDbDefaultWarning` не будет: база действительно читает `db_default` при удалении.

При `db_constraint=False` ограничения в базе нет, и колонку сбрасывает только каскад hare-orm в
Python. Он сначала берёт `default=`, а если его нет — `db_default` (как выражение SQL), поэтому
достаточно любого из двух. Тот же каскад в Python выполняется и для связанной модели с
`Meta.soft_delete_field`. У внешнего ключа на составной ключ одно и то же значение `db_default`
копируется во все его колонки и не может быть разным для разных частей, поэтому разные значения
для частей требуют `db_constraint=False` и `default=` в виде кортежа.

Поля, объявленные внутри файла миграции, не проверяются: они описывают схему, которая уже
применена. Чтобы задать такой колонке `DEFAULT`, добавьте в модель `db_default` и создайте новую
миграцию — она задаст `DEFAULT` колонке существующей таблицы.

`PROTECT` даёт `hare.exceptions.ProtectedError` (с `.protected_objects: list[Model]`), если вы
пытаетесь удалить строку, на которую ещё что-то ссылается. Проверка и удаление атомарны — отдельно
проверять существование ссылок перед удалением не нужно:

```python
class Event(Model):
    tournament = fields.ForeignKeyField("models.Tournament", null=True, on_delete=fields.PROTECT)

try:
    await tournament.delete()
except ProtectedError as exc:
    raise ValidationError(f"На этот турнир ещё ссылается событий: {len(exc.protected_objects)}")
```

`QuerySet.filter(...).delete()` (массовое удаление) выполняет ту же проверку `PROTECT` — один раз
для всего найденного набора, а не для каждой строки, — прежде чем что-либо удалить.

Проверка идёт и по цепочкам `CASCADE`: удаление строки даёт `ProtectedError`, если каскад удалил бы
строку, которую ещё защищает связь с `PROTECT`, сколько бы шагов `CASCADE` до неё ни было (и для
моделей с `Meta.soft_delete_field` тоже). В `protected_objects` лежат защищающие строки; защищающая
строка, которую тот же каскад тоже удаляет, не считается. Модель, в цепочке `CASCADE` которой нигде
нет `PROTECT`, за эту проверку ничем не платит.

Чтобы заранее узнать, что удаление затронет каскадом, обнулит или что его остановит, — ничего не
удаляя, — используйте [`instance.delete_preview()`](model-methods.ru.md).

### Индекс по колонке ключа {: #index-on-the-key-column }

`ForeignKeyField` по умолчанию создаёт индекс по своей колонке ключа, как в Django: фильтр по
связи, соединения таблиц, `prefetch_related()` и каждое каскадное удаление — и собственный
`ON DELETE` базы, и каскад hare-orm в Python — ищут строки по этой колонке, и без индекса каждый
такой поиск просматривает всю таблицу.

```python
class Membership(Model):
    user = fields.ForeignKeyField("models.User", related_name="memberships")  # покрыт индексом UniqueConstraint
    group = fields.ForeignKeyField("models.Group", related_name="memberships")  # свой индекс
    invited_by = fields.ForeignKeyField("models.User", related_name="invites", db_index=False)  # без индекса

    class Meta:
        constraints = [UniqueConstraint(fields=("user", "group"))]
```

- `db_index=False` отключает индекс.
- Отдельный индекс не создаётся, если индекс, уже объявленный в модели, начинается с колонок ключа
  (в любом порядке): обычная запись `Meta.indexes` (`Index(fields=...)`, уникальный или нет),
  безусловное `UniqueConstraint` или составной первичный
  ключ. Частичный индекс, индекс по выражению и индекс не вида «дерево» (btree) не считаются —
  кроме безымянного индекса ровно по колонкам ключа: он получает то же сгенерированное имя, что и
  собственный индекс связи, поэтому индекс связи не создаётся. Задайте одному из них `name=`, чтобы
  получить оба.
- Связь с [составным первичным ключом](#targeting-a-composite-primary-key) получает один индекс по
  всем своим колонкам ключа.
- С `db_constraint=False` индекс остаётся: каскад hare-orm ищет строки по этой колонке.
- `OneToOneField` отдельного индекса не получает: его ограничение `UNIQUE` уже индексирует колонку.
- Индекс называется так же, как собственный индекс любого поля: `idx_<таблица>_<колонка>_<хэш>`.

## `OneToOneField` {: #onetoonefield }

Аргументы те же, что у `ForeignKeyField`; внутри поле всегда получает `unique=True`. Первичным
ключом модели (`primary_key=True`) может быть только `OneToOneField` —
`ForeignKeyField(..., primary_key=True)` даёт `ConfigurationError`. Так же и
`ForeignKeyField(..., unique=True)` даёт `ConfigurationError`: связь, в которой на каждый связанный
объект приходится не больше одной строки, — это `OneToOneField`. Его ограничение `UNIQUE` уже
индексирует колонку, поэтому `db_index` по умолчанию выключен.

```python
def OneToOneField(
    to: type[TModel] | str | SwappableModelReference,
    related_name: str | None | Literal[False] = None,
    on_delete: OnDelete = CASCADE,
    db_constraint: bool = True,
    null: bool = False,
    **kwargs: Any,
) -> OneToOneRelation[TModel] | OneToOneNullableRelation[TModel]
```

## `ManyToManyField` {: #manytomanyfield }

```python
def ManyToManyField(
    to: type[TModel] | str | SwappableModelReference,
    through: str | type[Model] | SwappableModelReference | None = None,
    forward_key: str | None = None,
    backward_key: str = "",
    related_name: str = "",
    on_delete: OnDelete = CASCADE,
    db_constraint: bool = True,
    unique: bool = True,
    **kwargs: Any,
) -> ManyToManyRelation[TModel]
```

`db_index` и `lazy` передаются именованными аргументами.

```python
class Post(Model):
    tags = fields.ManyToManyField("models.Tag", related_name="posts")
```

| Аргумент | Что задаёт |
|---|---|
| `through` | Имя промежуточной таблицы (по умолчанию создаётся автоматически) или настоящий класс `Model` — для промежуточной таблицы со своими дополнительными колонками (см. ниже). Если автоматическое имя уже занято промежуточной таблицей другого поля «многие-ко-многим» или собственной таблицей какой-то модели (например, два `ManyToManyField` между одной и той же парой моделей), будет `ConfigurationError` — задайте `through=` явно у одного из них. |
| `forward_key` / `backward_key` | Имена колонок промежуточной таблицы (по умолчанию создаются автоматически). |
| `unique` | Создаёт индекс `UNIQUE` по `(backward_key, forward_key)`; `False` разрешает повторяющиеся строки для одной и той же пары. |
| `db_index` | Индексирует колонки ключей автоматической промежуточной таблицы, с которых не начинается её индекс `UNIQUE` (по умолчанию `True`): колонки `forward_key`, а при `unique=False` — ещё и колонки `backward_key`. `db_index=False` отключает это. Промежуточная таблица-`Model` индексирует свои `ForeignKeyField` [обычным образом](#index-on-the-key-column). |
| `on_delete` | Допустимы `CASCADE`/`RESTRICT`/`NO_ACTION`/`PROTECT`/`SET_NULL`. `SET_DEFAULT` недопустим: колонки промежуточной таблицы всегда `NOT NULL` и без значения по умолчанию, сбрасывать не во что, — он даёт `ConfigurationError`. Если `through` — это `Model`, `on_delete` поля переносится на собственные внешние ключи промежуточной модели (если у них оставлен `CASCADE`), а `SET_DEFAULT` принимается, только если оба этих внешних ключа выполняют [правило `SET_DEFAULT`](#set-default-needs-db-default). |
| `lazy` | Только `.SELECT` или `None` — без `.JOINED`: соединение через промежуточную таблицу размножило бы строки. Полный `refresh_from_db()` загружает связь снова. |

### Промежуточная таблица-модель {: #a-model-as-through }

Передайте настоящую модель вместо имени таблицы, если самой связи нужны дополнительные колонки
(время `added_at`, роль участника `role` и т. п.):

```python
class Membership(Model):
    team = fields.ForeignKeyField("models.Team")
    user = fields.ForeignKeyField("models.User")
    role = fields.CharField(max_length=32, default="member")

    class Meta:
        constraints = [UniqueConstraint(fields=("team", "user"))]


class Team(Model):
    members = fields.ManyToManyField("models.User", through=Membership, related_name="teams")
```

Промежуточная модель — обычная модель: к ней можно обращаться напрямую
(`Membership.objects.filter(role="admin")`), как к любой другой таблице, в дополнение к обычным
`.add()`/`.remove()`/`.clear()` у самой связи. hare-orm управляет её таблицей и миграциями так же,
как автоматической промежуточной таблицей. Оба собственных внешних ключа промежуточной модели
должны ссылаться на первичный ключ своей модели — `to_field=`, указывающий на другую колонку, даёт
`ConfigurationError` при `Hare.init()`.

Дополнительные поля промежуточной модели в новых строках задаёт `add(..., through_defaults=...)`:

```python
await team.members.add(user, through_defaults={"role": "admin"})
```

Они применяются только к строкам, которые этот вызов действительно вставляет, — уже существующая
пара остаётся как есть. `through_defaults` не имеет смысла (даёт `QueryError`) без
настоящего `through=Model`, для имени поля, которого нет в промежуточной модели, и для её
собственных внешних ключей, из которых состоит сама связь.

Если у промежуточной модели есть [`Meta.tenant_field`](../soft-delete-versions-tenants/multi-tenancy.ru.md), `add()` заполняет
его значением области промежуточной модели из `Tenancy.scope()`, если его не задаёт
`through_defaults`, — при области из нескольких значений задать его обязан `through_defaults`;
арендатор в `through_defaults` вне этой области даёт `QueryError`.

Операции со связью:

```python
async def add(
    self, *instances: Model | Any, through_defaults: dict[str, Any] | None = None,
    using: str | DatabaseClient | None = None,
) -> None
async def remove(self, *instances: Model | Any, using: str | DatabaseClient | None = None) -> None
async def clear(self, using: str | DatabaseClient | None = None) -> None
async def set(
    self, *instances: Model | Any, through_defaults: dict[str, Any] | None = None,
    clear: bool = False, using: str | DatabaseClient | None = None,
) -> None
async def create(
    self, using: str | DatabaseClient | None = None,
    through_defaults: dict[str, Any] | None = None, **kwargs: Any,
) -> Model
async def get_or_create(
    self, defaults: dict[str, Any] | None = None, through_defaults: dict[str, Any] | None = None,
    using: str | DatabaseClient | None = None, **kwargs: Any,
) -> tuple[Model, bool]
async def update_or_create(...)  # параметры get_or_create() и ещё create_defaults=
```

```python
tag = await post.tags.create(name="new")   # создаёт Tag и добавляет его, в одной транзакции
await post.tags.add(tag_a, tag_b)
await post.tags.remove(tag_a)
await post.tags.set(tag_b, tag_c)   # связь с tag_a удаляется, с tag_b остаётся как есть, tag_c добавляется
await post.tags.set([tag_b, 7])     # один итерируемый объект — объекты или значения первичного ключа
await post.tags.set(Tag.objects.filter(name__startswith="py"))
await post.tags.clear()
tag, created = await post.tags.get_or_create(name="python")
```

Как в Django, `add()`, `remove()` и `set()` принимают связанные объекты или значения их первичного
ключа (кортеж для составного ключа). `add()` читает строки по этим значениям через запрос связанной
модели по умолчанию и даёт `IntegrityError` для значения, которому не соответствует ни одна строка;
`remove()` по значению отвязывает только строки, которые этот запрос показывает. `set()` принимает
также один итерируемый или ожидаемый (`await`) объект — список, множество, запрос,
`values_list(..., flat=True)` ключей, другую связь. `get_or_create()`/`update_or_create()` ищут
только среди уже связанных строк; новый объект создаётся и добавляется в той же транзакции
(существующая строка, которая не входит в связь, создаётся заново, как в Django, — тогда
уникальный ключ даст `IntegrityError`).

`set()` в одной транзакции делает `instances` составом связи: удаляет связи, которых нет в
`instances` (помечая их промежуточные строки удалёнными, если у промежуточной модели есть
`Meta.soft_delete_field`), и добавляет новые — с `through_defaults`, как `add()`. Промежуточные
строки оставшихся связей не меняются, вместе с дополнительными полями. `clear=True` сначала очищает
всю связь и заново добавляет все `instances`. `set()` без аргументов очищает связь, как `clear()`.

`clear()` и `set()` удаляют только связи со строками, которые показывает запрос связанной модели по
умолчанию: связь с мягко удалённой строкой, со строкой другого арендатора (`Meta.tenant_field`) или
со строкой, которую скрывает свой `Meta.manager`, остаётся. `clear(all_tenants=True)` удаляет связи
со строками всех арендаторов, без выбора текущего арендатора (см.
[Разделение данных по арендаторам](../soft-delete-versions-tenants/multi-tenancy.ru.md)). `remove()` называет строки явно,
поэтому отвязывает и мягко удалённую строку, и скрытую `Meta.manager` (строка другого арендатора
всё равно отклоняется).

`add()` проверяет по базе, что владелец связи не помечен удалённым и что каждую добавляемую строку
показывает запрос связанной модели по умолчанию: строка, которую тем временем мягко удалили (объект
в памяти устарел) или которую скрывает её `Meta.manager`, даёт `IntegrityError`, и ничего не
связывается.

## Обратные ссылки {: #reverse-access }

`ForeignKeyField`/`OneToOneField` сам создаёт обратную ссылку на связанной модели с типом
`BackwardFKRelation[TModel]`/`BackwardOneToOneRelation[TModel]`; `ManyToManyField` даёт
`ManyToManyRelation[TModel]` с обеих сторон.

Ссылка «ко многим», прочитанная у объекта, — `author.books`, `book.tags` — это **`RelatedQuerySet`**
(`ReverseRelation` для обратного внешнего ключа, `ManyToManyRelation` для «многие-ко-многим»):
QuerySet менеджера связанной модели, отфильтрованный по строкам этого объекта. Это именно QuerySet —
отдельного «менеджера связи» с копией части методов QuerySet нет:

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
  менеджера; записи самой связи (`add()`, `remove()`, `set()`, `clear()`, `create()`, ...) остаются
  у связи.
- Если у менеджера связанной модели свой подкласс `QuerySet` (`objects = Manager(BookQuerySet)`), у
  связи есть и его методы.
- Запрос выполняется на подключении, с которого объект загружен или на которое сохранён, если
  маршрутизатор или `.using()` не говорят иначе.
- Связь ещё и **хранит свои строки, когда они загружены** — через `prefetch_related()`,
  [`prefetch_related_objects()`](#prefetch-related-objects), `lazy=` или перебором через
  `async for`. Тогда сама связь перебирается, измеряется и индексируется как список:
  `for book in author.books`, `book in author.books`, `len(author.books)`, `bool(author.books)`,
  `author.books[0]`. Для незагруженной связи это даёт `NoValuesFetched`. Запись через связь
  сбрасывает загруженное, поэтому, чтобы увидеть изменение, загрузите связь снова (или переберите
  её через `async for`).
- Связь, прочитанную у ни разу не сохранённого объекта, запросить нельзя (`QueryError`): ключа, на
  который могли бы ссылаться связанные строки, ещё нет.

Обратная связь внешнего ключа записывает данные так же, как связанный менеджер Django:

```python
await author.books.create(title="New")                  # внешний ключ указывает на author
book, created = await author.books.get_or_create(title="Old", defaults={"year": 1999})
await author.books.update_or_create(title="Old", defaults={"year": 2000})
await author.books.add(book_a, book_b)                   # один UPDATE; bulk=False сохраняет каждый
await author.books.remove(book_a)                        # ставит внешний ключ в NULL
await author.books.clear()                               # все книги автора
await author.books.set([book_b, book_c])                 # аргументы, один итерируемый объект или запрос
```

`add()` при `bulk=True` (по умолчанию) требует сохранённые объекты и даёт `IntegrityError` для
строки, которую не показывает запрос связанной модели по умолчанию; `bulk=False` сохраняет каждый
объект отдельно (выполняются обработчики `save()`, несохранённый объект создаётся). `remove()` и
`clear()` есть только у внешнего ключа, допускающего `NULL` (иначе `QueryError`); `remove()`
строки, которая указывает на другой объект, даёт `DoesNotExist`. `set()` удаляет связи со строками,
которых нет в новом составе, и добавляет новые — для внешнего ключа без `NULL` он только добавляет,
как в Django; `clear=True` сначала очищает. Значение в `kwargs`, которое направляет внешний ключ на
другую строку, а не на родительскую, даёт `QueryError` в `create()`, `get_or_create()` и
`update_or_create()`.

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

## Прямые ссылки {: #forward-access }

`book.author` возвращает загруженного `Author`, если связь загружена (`select_related()`,
`prefetch_related()`, `prefetch_related_objects()`, `lazy=` или присваивание), а иначе — запрос, который можно ждать
через `await`: `await book.author` работает в обоих случаях, если связанная строка существует. Связь
без связанной строки (внешний ключ `NULL`, обратный `OneToOneField`, на который никто не ссылается,
или строка, не прошедшая `Select(extra_condition=...)`) читается по-разному в зависимости от того,
загружена ли она. Незагруженная, она возвращает ложный объект `NoneAwaitable` (импортируется из
`hare.models`), а `await book.author` возвращает `None`. Загруженная, она равна обычному `None` —
проверяйте её через `book.author is None` (или смотрите `book.author_id`) и не используйте `await`.

Фильтр или присваивание по объекту читают поле (поля), на которое ссылается связь (`to_field`, по
умолчанию первичный ключ); объект, загруженный через `.only()`/`.defer()` без них, даёт
`QueryError` — как и его обратные ссылки и их `prefetch_related_objects()`.

## Загрузка связей {: #loading-relations }

Каждый способ загрузить связь делает одно дело:

| Как | Когда | Что выполняется |
|---|---|---|
| [`select_related("author")`](../querying/queryset-methods.ru.md) | Вместе с запросом, для связей «к одному» (внешний ключ, «один-к-одному», обратный «один-к-одному»). | `JOIN` в том же запросе. |
| [`prefetch_related("books", Prefetch(...))`](../querying/queryset-methods.ru.md#prefetch) | Вместе с запросом, для любой связи. | Ещё один запрос на связь для всех строк — для связи «многие ко многим» соединённый со связующей таблицей, или два, если связующая таблица читается первой (см. [`prefetch_related()`](../querying/queryset-methods.ru.md)). |
| `lazy=RelationLoadStrategy.JOINED` / `.SELECT` у поля | Всегда, для каждого запроса модели, — объявленное поведение по умолчанию; `defer_related("author")` выключает его для одного запроса. | Тот же `JOIN` / дополнительный запрос. |
| `await book.author`, `await author.books` | По требованию, для одного объекта. | Один запрос для этого объекта. |
| `prefetch_related_objects(instances, ...)` | Позже, для уже имеющихся объектов. | Один запрос на связь для всех объектов. |

### `prefetch_related_objects()` {: #prefetch-related-objects }

```python
from hare import Prefetch, prefetch_related_objects

await prefetch_related_objects(
    instances: Iterable[Model], *lookups: str | Prefetch, using: str | DatabaseClient | None = None
) -> None
```

Загружает связи уже имеющихся объектов — ровно то, что `prefetch_related()` делает для строк
запроса, с теми же аргументами: путь связи (`"posts__comments"`) или `Prefetch(...)` со своим
QuerySet или `to_attr`. Один запрос на связь загружает её для всех объектов, сколько бы их ни было;
один объект передаётся списком из одного элемента.

```python
users = await User.objects.filter(is_active=True)
...
await prefetch_related_objects(users, "emails", Prefetch("posts", Post.objects.filter(published=True)))
for user in users:
    print(len(user.emails), [post.title for post in user.posts])

await prefetch_related_objects([book], "author", "tags")   # один объект
```

- `instances` — объекты одной модели; пустая коллекция ничего не делает.
- Запросы выполняются на подключении, с которого пришёл первый объект, если `using=` не называет
  другое.
- Аргумент, который не называет связь модели, даёт `QueryError`.

## Связь с составным первичным ключом {: #targeting-a-composite-primary-key }

`ForeignKeyField`/`OneToOneField` может ссылаться на модель, первичный ключ которой —
[`CompositePrimaryKey`](field-types.ru.md#compositeprimarykey):

```python
class ArticleVersion(Model):
    id = fields.UUIDField()
    version = fields.IntField()
    pk = CompositePrimaryKey("id", "version")


class Tag(Model):
    # ссылается на пару (id, version) настоящим составным FOREIGN KEY на уровне таблицы
    article_version = fields.ForeignKeyField("models.ArticleVersion", related_name="tags")
```

hare-orm создаёт по одной колонке на каждую часть первичного ключа связанной модели и настоящее
ограничение `FOREIGN KEY (col1, col2) REFERENCES table (col1, col2)` — а не две независимые колонки
без ограничения. Соединения таблиц, каскады (`CASCADE`/`SET_NULL`/`SET_DEFAULT`/`PROTECT`) и
`prefetch_related()` работают так же, как для ключа из одной колонки.

`to_field=` можно указать и явно, но составной `to_field` принимается, только если **в точности**
совпадает с составным первичным ключом связанной модели, в объявленном порядке; произвольный
составной уникальный набор полей как цель не поддерживается (иначе `ConfigurationError`).

Фильтр через `ManyToManyField`, связанная модель которого имеет составной первичный ключ, тоже
работает: `=`, `__not`, `__in` и `__not_in` сравнивают весь первичный ключ как строку значений
(`(col1, col2) = (v1, v2)`), а не одну колонку.

## Заменяемые модели {: #swappable-models }

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


await Hare.init(config={..., "swappable": {"USER_MODEL": "accounts.User"}})
```

- `swappable("USER_MODEL")` принимают `ForeignKeyField`, `OneToOneField` и `ManyToManyField` (и его
  `through=` тоже). При `init()` он указывает на модель, выбранную настройкой, — заданную в
  конфигурации, а если её нет, то на модель, объявившую `Meta.swappable = "USER_MODEL"`. Поле хранит
  саму ссылку на настройку: `deconstruct()` возвращает её, поэтому файл миграции хранит
  настройку, а не модель, на которую она указывает.
- Модель по умолчанию, вместо которой настройка выбирает другую, считается **заменённой**:
  `Model._meta.swapped` хранит метку модели, используемой вместо неё (иначе `None`). Таблицы у неё
  нет — `Hare.generate_schemas()` и миграции её пропускают, проверка расхождений её не учитывает, —
  а каждый запрос или запись через неё (`User.objects.all()`, `User.objects.create(...)`) дают `ConfigurationError`:
  `"hare_ui.User" has been swapped for "accounts.User" by the USER_MODEL setting`. Её связи не
  регистрируют обратных ссылок, поэтому модель-замена может использовать те же `related_name`.
- Связь, которая называет заменённую модель напрямую (`"hare_ui.User"`), даёт `ConfigurationError`
  при `init()` — объявляйте её через `swappable("USER_MODEL")`.
- Автоматическая промежуточная таблица `ManyToManyField(swappable(...))` называется по полю
  (`consent_witnesses`), а не по связанной модели, поэтому не меняется вместе с настройкой.
- Выбирайте настройку в начале проекта: таблицы пакета при создании получают внешние ключи на ту
  модель, которую она называет. Как изменить её позже — см.
  [Заменяемые модели в миграциях](../migrations/migrations.ru.md#swappable-models).
