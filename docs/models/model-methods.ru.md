# Методы модели

`Model` (`hare.models.Model`) содержит то, что делает одна строка: сохранение, удаление, восстановление,
обновление из базы, копирование и сравнение. Запросы начинаются с
[`Model.objects`](../querying/managers.ru.md#model-objects) — это `QuerySet`, в котором есть `create()`,
`get()`, `filter()`, `bulk_create()` и всё остальное, что читает или пишет много строк.

## <a id="instance-methods"></a>Методы объекта

Каждый из этих методов работает со строкой одного объекта.

### <a id="save"></a>`save()`

```python
class Model:
    async def save(
        self,
        using: str | DatabaseClient | None = None,
        update_fields: Iterable[str] | None = None,
        *,
        force_create: bool = False,
        force_update: bool = False,
        changed_only: bool = False,
    ) -> None: ...
```
Создаёт строку, если у объекта нет первичного ключа или он ещё не сохранялся, иначе обновляет её.
`update_fields` ограничивает `UPDATE` этими колонками (имя прямой связи означает её колонку или колонки
ключа: `update_fields=["author"]` записывает `author_id`). Если у объекта уже есть первичный ключ,
`update_fields` всегда даёт `UPDATE` по первичному ключу — даже для объекта, который ни разу не загружали
и не сохраняли (`Model(id=known_id, name="x").save(update_fields=["name"])` — обновление без
предварительного чтения), — и даёт `IntegrityError`, если такой строки нет, а не создаёт её (для вставки
используйте `create()` или `save(force_create=True)`). `update_fields` игнорируется, только когда
первичного ключа ещё нет (его сгенерирует база) — тогда строка вставляется. Объект из
`.only()`/`.defer()`, у которого не загружено поле `auto_now`, всё равно можно сохранить (поле только
записывается, и свежее значение появляется в объекте), а объект без загруженного
`Meta.optimistic_lock_field` — нельзя. Ошибки: `IncompleteInstanceError` — у частично загруженного
(`.only()`/`.defer()`) объекта нет первичного ключа или сохраняемых полей; `IntegrityError` — невозможно
выполнить `force_create`/`force_update`; `StaleObjectError` — `Meta.optimistic_lock_field` обнаружил
одновременное изменение; `QueryError` — переданы и `force_create`, и `force_update`.

- `update_fields=[]` (любая пустая коллекция) ничего не делает: ничего не записывается, будь объект
  сохранён, не сохранён или загружен частично.
- `changed_only=True` записывает только поля, изменённые с момента загрузки или последнего сохранения
  объекта, — как `update_fields`, перечисляющий поля `get_dirty_fields()` (изменённая связь записывает
  колонку своего ключа); поля `auto_now` и `Meta.optimistic_lock_field` меняются как при любом
  обновлении. Если ничего не изменилось, команда не отправляется. Несохранённый объект вставляется
  целиком; с `force_update=True` ни разу не загруженный объект записывает все поля, кроме первичного
  ключа. Первичный ключ и вычисляемые базой поля не записываются никогда: изменённое поле первичного
  ключа у загруженного или сохранённого объекта даёт `QueryError`, ведь строка ищется по нему. Требует
  `Meta.track_dirty_fields` (без него — `QueryError`); вместе с `update_fields` или `force_create` даёт
  `QueryError`.
- `force_create=True` всегда вставляет текущий первичный ключ объекта, если он есть, — в том числе
  сгенерированный базой ключ загруженного или уже сохранённого объекта, — поэтому существующая строка
  даёт `IntegrityError`, а не вставляется копия под новым ключом.
- Первичный ключ, равный `None`, у которого есть `default`/`db_default` (например,
  `UUIDField(primary_key=True)`), получает это значение при вставке, поэтому копирование строки через
  `obj.pk = None; await obj.save()` работает для такого ключа так же, как для ключа, который генерирует
  база; `Model(id=None)`/`create(pk=None)` тоже применяют значение по умолчанию.
- Если записывать больше нечего (у модели только первичный ключ или все остальные поля ждут `db_default`
  либо генерируются), `save()` сохранённого объекта и `save(force_update=True)` всё равно проверяют, что
  строка существует, и дают `IntegrityError`, если её нет.

### <a id="delete"></a>`delete()`

```python
class Model:
    async def delete(self, using: str | DatabaseClient | None = None) -> None: ...
```
Настоящий `DELETE`, а если задано `Meta.soft_delete_field` — `UPDATE`, который ставит время удаления и
так же помечает связанные строки по каскаду. Каскад и проверки `PROTECT`/`RESTRICT` видят каждую
связанную строку, которая действительно ссылается на удаляемые, — у всех арендаторов, уже мягко удалённую
или скрытую фильтром своего `Meta.manager`. Та часть каскада, которую база не может выполнить сама
(мягкое удаление, связь с `db_constraint=False`), выполняется в Python, в той же транзакции, волнами:
каждая волна стоит одного запроса на каждую связь и модель, сколько бы строк она ни затронула. Если
связанная модель переопределяет `delete()`, этот метод вызывается для каждой её строки: связь, ведущая к
ней, даже та, которую поддерживает база, тогда каскадируется в Python. Строка, которую жёсткое удаление
удаляет по-настоящему (`hard_delete()` или каскад модели без мягкого удаления), а её модель задаёт
`Meta.soft_delete_field`, проходит через переопределённый `hard_delete()` своей модели, а не через
`delete()`, который лишь пометил бы её удалённой; без такого переопределения каскад идёт ниже неё к
переопределениям там. С `Meta.optimistic_lock_field`
строка удаляется только с той версией, с которой объект её прочитал: строка, изменённая с тех пор, даёт
`StaleObjectError` и остаётся на месте, как и строки, которые каскад удалил бы вместе с ней. Ошибки:
`QueryError` (объект ни разу не сохранялся), `IncompleteInstanceError` (первичный ключ или
`Meta.optimistic_lock_field` не загружены), `ProtectedError` (удаление запрещает связь с `PROTECT`),
`IntegrityError`, `StaleObjectError`.

### <a id="delete-preview"></a>`delete_preview()`

```python
class Model:
    async def delete_preview(self, *, using: str | DatabaseClient | None = None) -> DeletePreview: ...
```
Показывает, что сделал бы `delete()`, ничего не записывая, — например, для экрана подтверждения удаления.
Обходит то же дерево `CASCADE`, что и сам `delete()`, с тем же выбором мягкого удаления для каждой
модели, строками всех арендаторов, которые ссылаются на удаляемые (как `delete()`, а не только текущего
арендатора), уже мягко удалёнными связанными строками и связанными строками, которые скрывает фильтр
своего `Meta.manager` (как и сам `delete()`). `DeletePreview` (импортируется из `hare.models`) содержит:

| Атрибут | Что в нём |
|---|---|
| `deleted: dict[type[Model], int]` | строки, которые будут физически удалены, **включая сам объект** |
| `soft_deleted: dict[type[Model], int]` | строки, которые будут помечены удалёнными через `Meta.soft_delete_field` (уже помеченные не считаются) |
| `nulled: dict[type[Model], int]` | оставшиеся строки, у которых внешний ключ `SET_NULL` станет `NULL` |
| `set_default: dict[type[Model], int]` | оставшиеся строки, у которых внешний ключ `SET_DEFAULT` сбросится |
| `many_to_many_through: dict[str, int]` | строки, удаляемые из автоматических промежуточных таблиц «многие-ко-многим», по имени таблицы |
| `many_to_many_through_nulled: dict[str, int]` | строки автоматических промежуточных таблиц, у которых колонка ключа станет `NULL` (`SET_NULL` у «многие-ко-многим») |
| `protected_by: list[Model]` | строки, которые запрещают удаление через `PROTECT`, в том числе по цепочке; пусто, если ничто не защищает |
| `restricted_by: list[Model]` | строки, которые запрещают удаление через `RESTRICT`/`NO_ACTION` |
| `can_delete: bool` | `True`, если оба списка выше пусты |

Связь «многие-ко-многим» с `through=Model` — это обычный внешний ключ её промежуточной модели, поэтому её
строки попадают к этой модели в `deleted`/`nulled`. Считаются разные строки; строка, которую удаляет то
же удаление, не считается ещё и в `nulled`/`set_default`. Строка, до которой добрались с двух сторон
(ромб, оба конца строки «многие-ко-многим»), считается один раз. Уже мягко удалённый объект получает
пустой результат, потому что `delete()` для него ничего не делает, — и тогда, когда `.only()`/`.defer()`
не загрузили `soft_delete_field` (он тогда читается из базы). Запрещающие строки определяются так же, как
это делает база: строка с `RESTRICT`/`NO_ACTION`, которую та же команда `DELETE` тоже удаляет (ромб:
строка удаляется каскадом через одного родителя и ограничивает другого), удаление не запрещает там, где
база проверяет её в конце команды, — `NO_ACTION` в SQLite; `RESTRICT` SQLite проверяет сразу, поэтому там
такая строка всё ещё запрещает удаление. PostgreSQL проверяет оба на каждом шаге своего каскада, в порядке
своих ограничений, поэтому остановит ли такая строка удаление, решает сервер: предпросмотр считает её
запрещающей. На базе без внешних ключей (ClickHouse) `RESTRICT` проверяет сам hare, и такая строка удаление
не запрещает. Строка, мягко
удаляемая в том же каскаде, остаётся в таблице, поэтому её `PROTECT`/`RESTRICT` по-прежнему запрещает
удаление. Ошибки те же, что у `delete()`: `QueryError` (не совпадает арендатор, объект ни разу не
сохранялся) и `IncompleteInstanceError`. Результат — снимок на момент вызова: изменения строк, сделанные
параллельно между проверкой и настоящим `delete()`, не учитываются.

```python
preview = await author.delete_preview()
if not preview.can_delete:
    raise ValidationError(f"blocked by {preview.protected_by + preview.restricted_by}")
print(preview.deleted)  # {Author: 1, Book: 2, Chapter: 3}
```

### <a id="restore"></a>`restore()`

```python
class Model:
    async def restore(
        self,
        using: str | DatabaseClient | None = None,
        *,
        cascade: bool = False,
    ) -> None: ...
```
Отменяет мягкое удаление. По умолчанию возвращается только эта строка. `cascade=True` восстанавливает и
строки, которые мягкое удаление этой строки убрало вместе с ней: каждую строку, до которой ведёт
`on_delete=CASCADE` и у которой всё ещё стоит **время удаления этой строки**. Строка, мягко удалённая
отдельно — раньше или позже, — остаётся удалённой, как и строки ниже неё; значения, перезаписанные
`SET_NULL`/`SET_DEFAULT`, не возвращаются. Даёт `QueryError`, если у модели нет `Meta.soft_delete_field`
или объект ни разу не сохранялся. См.
[Мягкое удаление](../soft-delete-versions-tenants/soft-delete.ru.md).

### <a id="hard-delete"></a>`hard_delete()`

```python
class Model:
    async def hard_delete(self, using: str | DatabaseClient | None = None) -> None: ...
```
Настоящий `DELETE`, даже если задано `Meta.soft_delete_field`, — в том числе для уже мягко удалённой
строки. Связанные строки поступают по своему `on_delete`, как у модели без мягкого удаления; `PROTECT`
по-прежнему даёт `ProtectedError`. У модели без мягкого удаления то же самое, что `delete()`.

### <a id="refresh-from-db"></a>`refresh_from_db()`

```python
class Model:
    async def refresh_from_db(
        self,
        fields: Iterable[str] | None = None,
        using: str | DatabaseClient | None = None,
    ) -> None: ...
```
`refresh_from_db` заново загружает из базы все (или перечисленные) поля объекта и снова загружает каждую
связь с `lazy=`, которую обновление сбросило (прямой внешний ключ или «один-к-одному», колонку ключа
которых он перечитал, или «многие-ко-многим» при полном обновлении). Строка ищется без фильтра
`Meta.manager` (разделение по арендаторам всё равно действует), поэтому существующая строка, которую этот
фильтр скрывает, обновляется, а не даёт `DoesNotExist`. `fields=[]` (пустая коллекция) ничего не делает.
Связь называют её именем (`fields=["author"]`) или колонкой ключа; одна колонка ключа связи из нескольких
колонок (`document_version` у связи с `VersionedModel`) обновляет все колонки этого ключа, так что объект
никогда не держит ключ, собранный из двух.

Чтобы загрузить связи уже имеющихся объектов, используйте
[`prefetch_related_objects()`](relations.ru.md#prefetch-related-objects):
`await prefetch_related_objects([book], "author", "tags")`.

### <a id="update-from-dict"></a>`update_from_dict()`

```python
class Model:
    def update_from_dict(self: TModel, data: dict[str, Any]) -> TModel: ...
```
Присваивает значения из словаря, пропуская неизвестные ключи и приводя типы; `pk` задаёт первичный ключ
так же, как `Model(pk=...)`. Ошибки: `QueryError` для ключа обратной связи или «многие-ко-многим»,
`ValidationError` для значения, которое поле не принимает, `QueryError` для `None` в поле без `NULL`.

### <a id="clone"></a>`clone()`

```python
class Model:
    def clone(self: TModel, pk: Any = EMPTY) -> TModel: ...
```
Возвращает новую несохранённую копию (изменяемые значения полей копируются глубоко). Для составного
первичного ключа каждая часть определяется отдельно: части со своим `default=`/`db_default=` явное
значение не нужно, из `pk=(...)` берутся только части, у которых нет ни того, ни другого. Для первичного
ключа из одной колонки, который не `generated=True` и без `default`, тоже нужен явный `pk=`, иначе
`QueryError`. Явный `pk=` используется всегда, даже для ключа, генерируемого автоматически, а каждое поле
`auto_now_add` сбрасывается, чтобы новая строка получила своё значение при первом сохранении (задайте его
у копии до `save()`, чтобы сохранить старое); копия мягко удалённой строки живая — её
`Meta.soft_delete_field` очищается. Значения, которые база вычислила для строки оригинала
(`GeneratedField`), не копируются: копия читает свои после сохранения, а их чтение до этого даёт
`NoValuesFetched`.

### <a id="to-dict"></a>`to_dict()`

```python
class Model:
    def to_dict(self) -> dict[str, Any]: ...
```
Значения полей самой модели в виде обычного словаря с ключами — именами полей модели (а не колонок), то
же самое, что `dict(self)`. Сами поля связей в него не входят, но колонка ключа прямой связи входит
(`author_id`, а не `author`), поэтому метод никогда не обращается к незагруженной связи. Поле, которое
запрос с `.only()`/`.defer()` не загрузил, даёт `NoValuesFetched` с его именем.

### <a id="other-members"></a>Остальное

Ещё есть: `.pk` (чтение и запись; кортеж для составного ключа), `__str__` (`"Book object (1)"`) и
`__repr__` (`<Book: 1>`), `__eq__`, `__hash__` (`TypeError`, если первичный ключ ещё не задан),
`__iter__` (возвращает пары `(field_name, value)` — то же, что `to_dict()`), `__await__` (ожидание уже
загруженного объекта просто возвращает его самого).

## <a id="tracking-changes"></a>Отслеживание изменений

### <a id="get-dirty-fields"></a>`get_dirty_fields()`

```python
class Meta:
    track_dirty_fields = True
```

Включается явно (по умолчанию `False`): это стоит снимка значений при каждой загрузке, поэтому включайте
его только для моделей, которым это действительно нужно.

```python
class Model:
    def get_dirty_fields(self) -> dict[str, tuple[Any, Any]]: ...
```

```python
book = await Book.objects.get(pk=1)
book.title = "Dune Messiah"
book.get_dirty_fields()  # {"title": ("Dune", "Dune Messiah")}
```

Возвращает `{field: (old_value, new_value)}` для каждого поля самой модели, изменившегося с последней
загрузки из базы или последнего `save()`. Новый, ещё не сохранённый объект — и `clone()`, и объект,
вставка которого откатилась, — показывает каждое поле самой модели изменённым относительно `None`, поле
без значения (ключ, который генерирует база) — как `(None, None)`. Даёт `QueryError`, если не задан
`Meta.track_dirty_fields`.

Заглушка `db_default`, ещё не заменённая настоящим значением, и выражение для следующей записи
(`counter = F("counter") + 1`) сравниваются как один и тот же объект: относительно самих себя они не
изменены, а после `save()` выражение остаётся в объекте неизменённым, пока `refresh_from_db()` не
прочитает записанное значение. То же относится к `snapshot()`/`diff_against()`. Если транзакция, в
которой были `save()`/`delete()`/`restore()`, откатывается, точка отсчёта изменений возвращается к тому,
что было до записи.

Это то, чем стоит пользоваться вместо ручного приёма «прочитать свежую копию и сравнить `to_dict()`»
перед публикацией события: разница вычисляется за вас, по снимку в памяти, а не дополнительным `SELECT`.

Использует ту же логику копирования и сравнения, что `snapshot()` и `diff_against()` ниже.

### <a id="snapshot"></a>`snapshot()` / `diff_against()`

```python
class Model:
    def snapshot(self, fields: Iterable[str] | None = None) -> FieldSnapshot: ...

    def diff_against(self, snapshot: FieldSnapshot) -> dict[str, tuple[Any, Any]]: ...
```
Ручная пара «до и после» для журналов изменений, согласований или отмены действий — работает с **любой**
моделью, `Meta.track_dirty_fields` не нужен. `snapshot()` запоминает значения полей (по умолчанию всех
загруженных полей самой модели или только `fields=`) в неизменяемый `FieldSnapshot`
(`from hare.models import FieldSnapshot`) — словарь только для чтения с `.fields` и `.model_class`.
`diff_against()` возвращает `{field: (snapshot_value, current_value)}` для полей снимка, которые с тех
пор изменились.

```python
snapshot = book.snapshot()
book.details["cover"] = "hardback"      # правка JSON на месте
book.status = "published"
book.diff_against(snapshot)
# {"details": ({"cover": "paperback"}, {"cover": "hardback"}), "status": ("draft", "published")}

book.snapshot(fields=["status"])        # запоминаются и сравниваются только эти поля
```

- Изменяемые значения (`dict`, `list`, `set`, `bytearray`, другие объекты) при снимке копируются глубоко,
  поэтому правка на месте вроде `obj.config["url"] = ...` видна в разнице. Неизменяемые значения (`int`,
  `float`, `str`, `bool`, `bytes`, `None`, `datetime`/`date`/`time`/`timedelta`, `Decimal`, `UUID`,
  `Enum`) хранятся как есть, без копирования. `clone()` и `track_dirty_fields` следуют тому же правилу.
- Значение JSON, у которого поменялся только тип — `{"a": 1}` на `{"a": True}` или `{"a": 1.0}` на любой
  глубине, или `1` на `True` на верхнем уровне поля JSON, — считается изменённым, потому что база хранит
  его иначе. Остальные значения сравниваются через `==` (`Decimal("1.50")` и `Decimal("1.5")` равны).
  `get_dirty_fields()` сравнивает так же.
- Ничего не происходит само: загрузка и `save()` снимков не делают, если у модели нет
  `Meta.track_dirty_fields`, поэтому модели, которые не вызывают `snapshot()`, ничем не платят.
- `fields=` принимает любой итерируемый объект с именами полей самой модели (колонку внешнего ключа вроде
  `author_id`, а не связь `author`). Неизвестное имя даёт `FieldError`, а поле, которое запрос с
  `.only()`/`.defer()` не загрузил, — `IncompleteInstanceError`
  (при `fields=None` по умолчанию незагруженные поля просто пропускаются).
- `diff_against()` принимает и снимок другого объекта той же модели (чтобы сравнить две строки); снимок
  другой модели даёт `TypeError`.
- Снимок не зависит от объекта: `save()`/`refresh_from_db()` его не обновляют.
- `snapshot` и `diff_against` — зарезервированные имена: поле с таким именем объявить нельзя.

## <a id="optimistic-locking"></a>Оптимистичная блокировка: `optimistic_lock_field`

`Meta.optimistic_lock_field` называет `IntField` с версией строки. Запись экземпляра проверяет, что у
строки всё ещё та версия, которую экземпляр прочитал, и увеличивает её, поэтому копия, прочитанная до
другой записи, не может эту запись перезаписать:

```python
class Book(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=300)
    version = fields.IntField(default=1)

    class Meta:
        optimistic_lock_field = "version"


first = await Book.objects.get(id=1)
second = await Book.objects.get(id=1)
first.title = "Dune"
await first.save()            # версия 1 -> 2
second.title = "Dune Messiah"
await second.save()           # StaleObjectError: у строки версия 2
```

| Запись | Версия |
|---|---|
| `save()` сохранённого экземпляра | Строка обновляется только при версии, которая есть у экземпляра, затем версия увеличивается у строки и у экземпляра; при другой версии — `StaleObjectError`. |
| `delete()` | Строка удаляется, в том числе мягко, только при этой версии; при другой версии — `StaleObjectError`, но строка, мягко удалённая за это время, делает удаление пустым. |
| `bulk_update()` | Каждый объект проверяется по своей версии: свежие записываются с увеличенной версией, а для устаревших `StaleObjectError` бросается, когда выполнены все команды, — см. [`bulk_update()`](../querying/queryset-methods.ru.md#bulk-update). |
| `QuerySet.update()` | Увеличивается у каждой записанной строки без проверки: у запроса нет версии. |
| `bulk_create()`, обновляющий конфликтующую строку, `merge()`, обновляющий совпавшую строку | Увеличивается у обновлённой строки. |

Поле должно быть `IntField` модели без `null`, не входить в первичный ключ и не вычисляться базой — иначе
при объявлении класса будет `ConfigurationError`. hare задаёт его сам: `update()`, `bulk_update()` и
`update_or_create(defaults=...)` с этим полем дают `QueryError`. Экземпляр, прочитанный через
`.only()`/`.defer()` без этого поля, нельзя сохранить или удалить (`IncompleteInstanceError`). См.
[`StaleObjectError`](../errors/exceptions.ru.md#staleobjecterror).

## <a id="classmethods"></a>Методы класса

Эти методы работают с самой моделью.

### <a id="get-connection"></a>`get_connection()`

```python
class Model:
    @classmethod
    def get_connection(cls, *, for_write: bool = False) -> DatabaseClient: ...
```
Подключение, на котором запрос к модели выполнился бы сейчас: выбор настроенного
[маршрутизатора](../connections/multiple-databases.ru.md#routers) (`db_for_write` при `for_write=True`,
иначе `db_for_read`), а если его нет — подключение модели по умолчанию. Внутри открытой транзакции на
этом подключении — клиент транзакции. Даёт `ConfigurationError` без активного `HareContext` или для
модели, заменённой через свою настройку `swappable`. Модель, арендаторы которой требуют этого
(`Meta.tenant_schema`, построчная защита арендаторов), даёт `QueryError`, если не активен ровно один
арендатор или запрос идёт вне транзакции, задавшей арендаторов. `QuerySet.get_connection()` отвечает на
тот же вопрос для одного запроса с учётом его `using()`.

Закрепите запросы за этим подключением через `using=`/`.using()` и читайте его `features`, чтобы
пользоваться только тем, что база умеет, — например, блокируйте строку там, где база блокирует строки
(SQLite вместо этого выполняет запись по очереди и отклоняет `select_for_update()`):

```python
connection = Book.get_connection(for_write=True)
async with Transactions.atomic(connection.connection_alias) as transaction:
    queryset = Book.objects.filter(pk=book_id).using(transaction)
    if transaction.features.supports_select_for_update:
        queryset = queryset.select_for_update()
    book = await queryset.get()
    ...
```

### <a id="get-routed-connection"></a>`get_routed_connection()`

```python
class Model:
    @classmethod
    def get_routed_connection(cls, *, for_write: bool = False) -> DatabaseClient: ...
```
Подключение, которое маршрутизатор даёт модели, без проверки арендаторов — чтобы прочитать его имя и
`features` там, где запрос модели не выполняется: у модели `Meta.tenant_schema` вне области арендатора.

### <a id="construct"></a>`construct()`

```python
class Model:
    @classmethod
    def construct(cls: type[TModel], **kwargs: Any) -> TModel: ...
```
Создаёт «отдельный» объект **без** проверок значений, без обращений к базе и без проверки, что связанные
объекты сохранены, — для тестов и восстановления объектов из данных. Значения вложенных связей,
переданные как обычные значения или списки, оборачиваются так, чтобы читались как уже загруженные.
Несохранённый объект с заданным автоматически генерируемым ключом вставляется с этим ключом;
`get_dirty_fields()` у модели с `track_dirty_fields` показывает все поля до первого `save()`.

### <a id="model-getitem"></a>`Model[pk]`

```python
await Model[some_pk]  # -> Self или DoesNotExist
```
`Model[pk]` — это `Model.objects.get(pk=pk)`; ключ, который не может быть значением поля первичного ключа
(`Book["abc"]` при целочисленном ключе), тоже даёт `DoesNotExist`.

## <a id="model-meta"></a>`Model._meta`

`Model._meta` — объект `MetaInfo` (`hare.models`) — описывает таблицу и поля модели. Эти атрибуты
публичные и доступны только для чтения — задавайте их через опции `Meta`, никогда не присваивайте в
`_meta`:

| Атрибут | Что в нём |
|---|---|
| `full_name` | `"app.ModelName"` — метка, по которой модель называют связи и настройки `swappable`. |
| `app` | Метка приложения. |
| `db_table` | Имя таблицы — `Meta.table` или имя класса строчными буквами. |
| `schema` | Схема базы (`Meta.schema`) или `None`. |
| `default_connection` | Имя подключения, настроенного для приложения модели. |
| `primary_key_attribute` | Имя поля первичного ключа; кортеж имён для `CompositePrimaryKey`; `()` у модели с `Meta.primary_key = None`. |
| `primary_key_attribute_names` | `primary_key_attribute` всегда в виде кортежа — `("id",)`, `("id", "version")`, `()`. |
| `pk` | Поле первичного ключа; `None` для составного ключа или без ключа. |
| `pk_fields` | Поля составного первичного ключа по порядку; иначе `()`. |
| `has_primary_key` / `has_composite_primary_key` | Есть ли у модели первичный ключ / составной первичный ключ. |
| `fields_map` | `{name: Field}` всех полей: поля с данными, связи (прямые, обратные, «многие-ко-многим») и колонки ключей прямых связей (`author_id`). |
| `fields` | Имена из `fields_map`. |
| `fields_db_projection` | `{field name: column name}` для каждого поля, у которого есть колонка. |
| `db_fields` | Имена колонок таблицы в порядке объявления. |
| `foreign_key_fields` / `one_to_one_fields` / `many_to_many_fields` | Имена прямых `ForeignKeyField` / `OneToOneField` / `ManyToManyField`. |
| `backward_foreign_key_fields` / `backward_one_to_one_fields` | Имена обратных связей — `related_name`, которые дают этой модели внешние ключи и «один-к-одному» других моделей. |
| `fetch_fields` | Имена всех связей выше — то, что принимают `prefetch_related()`/`prefetch_related_objects()`. |
| `sensitive_fields` | Имена полей, объявленных с `sensitive=True` (см. [Чувствительные поля](encrypted-and-sensitive-fields.ru.md#sensitive-fields)). |
| `soft_delete_field` / `tenant_field` / `optimistic_lock_field` | Имена полей, которые для этого объявляет `Meta`, или `None`. |
| `managed` | `Meta.managed`. |
| `meta` | Класс `Meta` модели со слитыми в него `Meta` абстрактных предков — отсюда расширение читает свои опции (`Meta.outbox_wakeup`). |

Тип поля видно по его атрибутам — `relation_type`, `encrypted`, `enum_type` (см.
[Базовый класс `Field`](field-types.ru.md#the-base-field-class)):

```python
from hare.fields import RelationType

for name, field in Book._meta.fields_map.items():
    if field.relation_type is RelationType.MANY_TO_MANY:
        ...
```

`get_lookup_info()`, `get_lookups()` и `get_ordering_info()` описывают ключи фильтров и сортировки — см.
[Описание фильтров и сортировок](../querying/describing-filters.ru.md). Вместе с `fields_map` это и есть
описание модели: аргументы конструктора каждого поля даёт `field.deconstruct()` (`(path, args, kwargs)` —
то, что записывает файл миграции), его тип Python — `field.get_annotation()`.
