# Pydantic

Строит модель Pydantic прямо из модели hare-orm — для схем ответов API, без ручного написания
параллельного класса Pydantic для каждой модели.

```python
from hare.contrib.pydantic import pydantic_model_creator, pydantic_queryset_creator

AuthorSchema = pydantic_model_creator(Author)
AuthorListSchema = pydantic_queryset_creator(Author)
```

## <a id="pydantic-model-creator"></a>`pydantic_model_creator()`

```python
def pydantic_model_creator(
    model: type[Model],
    *,
    name: str | None = None,
    exclude: tuple[str, ...] | None = None,
    include: tuple[str, ...] | None = None,
    computed: tuple[str, ...] | None = None,
    optional: tuple[str, ...] | None = None,
    allow_cycles: bool | None = None,
    sort_alphabetically: bool | None = None,
    exclude_readonly: bool = False,
    meta_override: type | None = None,
    model_config: ConfigDict | None = None,
    validators: dict[str, Any] | None = None,
    module: str = PYDANTIC_MODELS_MODULE,
    exclude_sensitive: bool = False,
    relations_as_ids: bool = False,
) -> type[PydanticModel]
```

| Параметр | Что задаёт |
|---|---|
| `name` | Явное имя модели вместо автоматического. |
| `exclude` / `include` | Какие поля дополнительно убрать или оставить, поверх того, что уже задаёт `PydanticMeta`. Путь через связь (`"tournament.name"`) указывает на поле схемы связанной модели; в `include` он ещё и оставляет саму связь — только с перечисленными полями. |
| `computed` | Имена `@property` или методов модели, которые нужно показать как вычисляемые поля; путь через связь (`"team_members.name_length"`) добавляет такое поле в схему связанной модели. |
| `optional` | Поля, которые в схеме становятся необязательными, хотя в модели они не допускают `NULL`. |
| `allow_cycles` | Разрешить связям модели с самой собой и циклическим связям уходить в рекурсию (по умолчанию `False` — циклы обрываются). |
| `sort_alphabetically` | Упорядочить поля по алфавиту, а не в порядке объявления. |
| `exclude_readonly` | Построить схему для записи, из которой убраны все поля, которые заполняет база: первичный ключ, который присваивает база или ORM (генерируемый или с `default`/`db_default`), и всё с `generated=True` — в том числе `auto_now`/`auto_now_add` и `TSVectorField(stored=True)`, — а также все обратные связи и вычисляемые поля (ни то, ни другое нельзя передать при создании). Прямые связи (внешний ключ, «один-к-одному», «многие-ко-многим») остаются в схеме: их заполняет не база, вызывающий должен их передать (обязательно или нет — по тем же правилам `NULL` и значения по умолчанию, что и у любого поля). Естественный первичный ключ (`CharField(primary_key=True)` или части `CompositePrimaryKey` без значения по умолчанию) остаётся в схеме обязательным полем — вызывающий должен передать его, чтобы создать строку. Первичный ключ «один-к-одному» представлен своей связью (или полем `<имя>_id` при `relations_as_ids=True`). |
| `meta_override` | Класс в виде `PydanticMeta`, заменяющий собственный класс модели, без изменения самой модели: каждая заданная в нём опция, в том числе `model_config`, заменяет опцию модели. |
| `model_config` | `ConfigDict` Pydantic, сливаемый с настройками созданной модели. |
| `validators` | `{имя_проверки: field_validator(...)(func)}` — передаются в `__validators__` у `pydantic.create_model`. |
| `module` | `__module__` созданного класса — по умолчанию `"hare.contrib.pydantic"` (`PYDANTIC_MODELS_MODULE`). |
| `exclude_sensitive` | Убрать каждое поле с `sensitive=True` (`Model._meta.sensitive_fields`, см. [Чувствительные поля](../models/encrypted-and-sensitive-fields.ru.md#sensitive-fields)), включая вложенные схемы связей. Полезно для открытой схемы или схемы выгрузки. Схемы, построенные с ним и без него, сохраняются отдельно и никогда не смешиваются. |
| `relations_as_ids` | Показывать каждую прямую связь (внешний ключ, «один-к-одному») плоским полем `<имя>_id` вместо вложенной подмодели — см. [Связи как идентификаторы](#relations-as-ids). |

Созданные модели сохраняются и используются повторно для одинаковых сочетаний `(модель, параметры)`:
два вызова `pydantic_model_creator(Author)` возвращают один и тот же класс.

Собственные проверки поля в ORM — `Field(validators=[...])` (см. [Проверки значений](../models/validators.ru.md)) —
тоже выполняются в созданной схеме автоматически: `model_validate()` или создание объекта дают
`ValidationError` Pydantic, если любая из них отклонит значение, — в дополнение к (а не вместо) тому,
что вы передали для этого поля через параметр `validators=` выше. Поле из `optional=` принимает явный
`null` (означает «не передано») — проверки ORM для него не выполняются.

Типы значений: `BinaryField` передаётся в JSON как base64 (`model_dump_json()` его кодирует,
`model_validate_json()` раскодирует, в схеме `format: base64url`), поэтому любые байты доходят
неизменными; чтобы изменить это, передайте свои `ser_json_bytes`/`val_json_bytes` в `model_config`.
`JSONField` с объявленным `field_type` получает в схеме этот тип (без него — `Any`).
`IntEnumField`/`CharEnumField` — это `$ref` только на перечисление, без числового диапазона или
ограничения длины колонки.

Поле в созданной схеме **необязательно**, если выполняется хотя бы одно: оно перечислено в
`optional=`; в модели оно допускает `NULL`; у него есть значение по умолчанию на стороне Python
(`default=`); у него есть `db_default=`; это не первичный ключ и оно явно помечено `generated=True`
(`GeneratedField` или любое другое поле, которое заполняет база, — вызывающий не должен его
передавать, и в JSON Schema оно помечено `readOnly`); это `DatetimeField`/`TimeField` с
`auto_now=True`/`auto_now_add=True` (ORM заполняет его при сохранении, по той же причине, тоже
`readOnly`). Иначе поле обязательно. Первичный ключ — отдельный случай: целочисленный
автоинкрементный первичный ключ тоже `generated=True`, но остаётся в схеме обязательным, как любая
часть первичного ключа, — если только он не перечислен в `optional=`, что делает его необязательным и
допускающим `null`, как любое другое поле.

Модель с [`CompositePrimaryKey`](../models/field-types.ru.md#compositeprimarykey) обрабатывается так же,
как модель с ключом из одной колонки: каждая часть составного ключа становится отдельным полем
созданной схемы.

`Meta.tenant_field` тоже необязательно: `save()`/`create()` заполняют его текущим арендатором — когда
область состоит из одного значения; при [нескольких значениях](../soft-delete-versions-tenants/multi-tenancy.ru.md#tenancy-scope)
входные данные обязаны его передать, и оно проверяется по области. Это
относится и к внешнему ключу на арендатора (`tenant_field = "company_id"` или `"company"` для
`company = fields.ForeignKeyField(...)`) — и во вложенной подмодели, и в поле `company_id`.

Вложенная подмодель прямой связи — это `<Подмодель> | None` даже для внешнего ключа без `NULL`, если у
связанной модели есть `Meta.soft_delete_field`, `Meta.tenant_field` или фильтр своего `Meta.manager`:
чтение связи даёт `None` для цели, которую скрывает это ограничение, поэтому схема это принимает.

### <a id="relations-as-ids"></a>Связи как идентификаторы

По умолчанию прямая связь становится вложенной подмоделью связанной модели. API на JSON обычно
нужен плоский идентификатор — это делает `relations_as_ids=True`:

```python
class Book(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=200)
    author: fields.ForeignKeyRelation[Author] = fields.ForeignKeyField("models.Author", related_name="books")
    editor: fields.ForeignKeyNullableRelation[Author] = fields.ForeignKeyField("models.Author", null=True)
    tags: fields.ManyToManyRelation[Tag] = fields.ManyToManyField("models.Tag")


BookSchema = pydantic_model_creator(Book, relations_as_ids=True)
# поля: id, title, author_id (UUID, обязательное), editor_id (UUID | None, необязательное), tags (list[Tag])
BookCreate = pydantic_model_creator(Book, relations_as_ids=True, exclude_readonly=True, exclude=("tags",))
```

- **Имя поля** — собственный атрибут модели для колонки ключа, `<имя>_id` (`book.author_id`), в том
  числе когда у внешнего ключа своя колонка в базе через `source_field=`: меняется имя колонки, а не
  атрибута. Внешний ключ на модель с [`CompositePrimaryKey`](../models/field-types.ru.md#compositeprimarykey)
  получает по полю на каждую часть (`<имя>_<часть_ключа>`).
- **Тип** — тип первичного ключа связанной модели (`int`, `UUID`, `str` со своим `max_length`, …);
  **допустимость `null` и обязательность** определяются самим внешним ключом, как у обычного поля:
  внешний ключ с `null=True` — это `<тип> | None` и необязательное поле, внешний ключ без `NULL` и без
  значения по умолчанию — обязательное.
- `optional=`, `exclude_readonly=`, `exclude_sensitive=` действуют на поле идентификатора так же, как на
  любое другое. `exclude_readonly=True` его оставляет (вызывающий передаёт его при создании). Внешний
  ключ с `sensitive=True` теряет свой идентификатор при `exclude_sensitive=True`.
- `exclude=`/`include=`/`optional=` принимают **и** имя связи (`"author"`), **и** имя поля
  идентификатора (`"author_id"`) — оба выбирают одно и то же поле.
- «Один-к-одному» с `primary_key=True` становится просто своим полем идентификатора, которое и есть
  первичный ключ; `exclude=("owner",)` этот первичный ключ никогда не убирает — убирает только
  `exclude=("owner_id",)`.
- **Обратные связи** (обратный внешний ключ, «один-к-одному») не имеют колонки в этой строке, поэтому
  плоская схема их вообще не включает — `PydanticMeta.backward_relations` и аннотации не учитываются.
- **Связи «многие-ко-многим» остаются вложенными** подмоделями (`list[TagSchema]`) и по-прежнему
  загружаются заранее. Вложенные подмодели (схема цели «многие-ко-многим») тоже строятся с
  `relations_as_ids=True`.
- `from_hare_orm()`/`from_queryset()` читают идентификатор прямо из объекта — без запроса к связанному
  объекту. Заранее загружаются только связи «многие-ко-многим», оставшиеся в схеме.
- Схема, построенная с этим флагом, сохраняется отдельно от схемы без него; имя схемы без флага не
  меняется.

[`GenericForeignKeyField`](../models/relations.ru.md#genericforeignkeyfield) занимает в схеме место своих
веток: на выходе это объединение схем целей, различаемых полем `type` (имя ветки), на входе
(`exclude_readonly=True`) и с `relations_as_ids=True` — `{"type": "post", "id": 1}`, составной ключ —
полями ключа. `from_hare_orm()`/`from_queryset()` заранее загружают его ветки.

## <a id="pydanticmeta"></a>`PydanticMeta` — настройки модели по умолчанию

Объявите в самой модели hare-orm вложенный класс, чтобы задать настройки, которые подхватывает каждый
вызов `pydantic_model_creator()` для этой модели (явный аргумент функции всё равно важнее):

```python
class Author(Model):
    id = fields.UUIDField(primary_key=True)
    name = fields.CharField(max_length=120)
    books: fields.ReverseRelation["Book"]

    class PydanticMeta:
        exclude = ("some_internal_field",)
        max_recursion = 2
        backward_relations = False  # не включать `books`, если она явно не указана в аннотации
```

| Опция | По умолчанию | Что задаёт |
|---|---|---|
| `include` / `exclude` | `()` / `("Meta",)` | Какие поля оставить / убрать. |
| `computed` | `()` | Дополнительные вычисляемые поля. |
| `backward_relations` | `True` | Включать ли обратные связи (внешний ключ, «один-к-одному») даже без явной аннотации типа, которая их запрашивает. Не рекомендуется: так можно бесконтрольно загрузить очень много данных. |
| `max_recursion` | `3` | На сколько уровней вглубь идёт обход связей. |
| `allow_cycles` | `False` | Разрешить рекурсию через связь модели с самой собой. |
| `exclude_raw_fields` | `True` | Убирать колонку ключа внешнего ключа (например, `author_id`), если сам связанный объект включён. |
| `sort_alphabetically` | `False` | Порядок полей. |
| `model_config` | `None` | Дополнительный `ConfigDict` Pydantic. |

## <a id="computed-fields"></a>Вычисляемые поля

```python
class Author(Model):
    first_name = fields.CharField(max_length=60)
    last_name = fields.CharField(max_length=60)

    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


AuthorSchema = pydantic_model_creator(Author, computed=("full_name",))
```

Подходит обычный метод, `@property` или `functools.cached_property`. Аннотация возвращаемого типа
функции становится типом поля в схеме. Вычисляемое поле, которое обращается к незагруженной связи,
даёт `NoValuesFetched` с подсказкой: либо укажите связь в аннотации, чтобы она загружалась заранее,
либо сами сначала загрузите её через `prefetch_related_objects()`.

## <a id="building-instances"></a>Создание объектов схемы: `PydanticModel` / `PydanticListModel`

Каждая созданная схема наследует `PydanticModel` (или `PydanticListModel` для варианта со списком),
которые добавляют к обычному `model_validate()` конструкторы, знающие про ORM:

```python
async def from_hare_orm(cls, obj: Model) -> Self          # один объект, асинхронно — сам загружает связи
async def from_queryset_single(cls, queryset: QuerySetSingle) -> Self
async def from_queryset(cls, queryset: QuerySet) -> list[Self]
```

```python
author = await Author.objects.get(pk=author_id)
schema = await AuthorSchema.from_hare_orm(author)

schemas = await AuthorSchema.from_queryset(Author.objects.filter(name__icontains="le guin"))
list_schema = await AuthorListSchema.from_queryset(Author.objects.all())
```

Все три заранее загружают связи, на которые ссылается сама схема, поэтому вызывать
`select_related()`/`prefetch_related()` ради её аннотаций не нужно. Простой
`AuthorSchema.model_validate(author)` тоже работает (это даёт `from_attributes=True`), но даёт
`NoValuesFetched` с именем поля, если схема ссылается на внешний ключ, «один-к-одному»,
«многие-ко-многим» или обратную связь, которые не загружены в `author`, — даже внешний ключ с `NULL`,
значение которого действительно `None`, требует явной загрузки: иначе нельзя отличить «точно пусто» от
«ещё не проверяли». Используйте один из трёх методов выше, а не загружайте связи вручную.

## <a id="modeldescription"></a>`ModelDescription` — поля модели по типам

```python
from hare.contrib.pydantic.descriptions import ModelDescription

description = ModelDescription.from_model(Book)
```

То, из чего строится схема: поля модели, сгруппированные по типам, каждая группа — список объектов
`Field` в порядке объявления: `pk_fields` (по одному на каждую часть составного ключа), `data_fields`
(все остальные поля с обычным значением, включая колонки ключей прямых связей вроде `author_id`),
`foreign_key_fields`, `backward_foreign_key_fields`, `one_to_one_fields`, `backward_one_to_one_fields` и `many_to_many_fields`. Связи модели должны
быть настроены (`Hare.init()` или `Hare.bind_models()`).

## <a id="pydantic-queryset-creator"></a>`pydantic_queryset_creator()`

```python
def pydantic_queryset_creator(
    model: type[Model],
    *,
    name: str | None = None,
    exclude: tuple[str, ...] | None = None,
    include: tuple[str, ...] | None = None,
    computed: tuple[str, ...] | None = None,
    optional: tuple[str, ...] | None = None,
    allow_cycles: bool | None = None,
    sort_alphabetically: bool | None = None,
    exclude_readonly: bool = False,
    meta_override: type | None = None,
    model_config: ConfigDict | None = None,
    validators: dict[str, Any] | None = None,
    module: str = PYDANTIC_MODELS_MODULE,
    exclude_sensitive: bool = False,
    relations_as_ids: bool = False,
) -> type[PydanticListModel]
```

Оборачивает схему одного объекта в схему списка на основе `RootModel` (по умолчанию с именем
`<Model>_list`) — удобно как аннотация возвращаемого типа для точки API «список всех X». `name=` задаёт
имя только схемы списка; схема элемента сохраняет своё имя по умолчанию. Все остальные аргументы
строят схему элемента, как в `pydantic_model_creator()`.
