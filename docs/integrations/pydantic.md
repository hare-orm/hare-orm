# Pydantic

Generates a Pydantic model straight from a hare-orm model — for API response schemas, without
hand-writing a parallel Pydantic class for every model.

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

| Parameter | Meaning |
|---|---|
| `name` | Explicit model name, instead of an auto-generated one. |
| `exclude` / `include` | Extra fields to drop/keep, on top of whatever `PydanticMeta` already says. A path through a relation (`"tournament.name"`) reaches a field of the related schema; in `include` it also keeps the relation itself, with only the listed fields. |
| `computed` | Names of `@property`/methods on the model to expose as computed fields; a path through a relation (`"team_members.name_length"`) adds one to the related schema. |
| `optional` | Fields to make optional in the schema even though they aren't nullable on the model. |
| `allow_cycles` | Allow self-referential/cyclic relations to recurse (default `False` — cycles are cut off). |
| `sort_alphabetically` | Sort fields alphabetically instead of declaration order. |
| `exclude_readonly` | Build a write-schema variant that drops every DB-assigned field (a pk the database or ORM assigns — generated, or with a `default`/`db_default` — and anything with `generated=True` — includes `auto_now`/`auto_now_add` and a `TSVectorField(stored=True)`), plus every backward relation and computed field (neither can ever be supplied on create). A forward FK/O2O/M2M stays in the schema — it isn't DB-assigned, the caller still has to supply it (required or optional, following the same nullable/default rules as any other field). A natural primary key (a `CharField(primary_key=True)` or `CompositePrimaryKey` components with no default) stays in the schema as a required field — the caller has to supply it to create the row. An O2O primary key is represented by its relation (or its `<name>_id` field with `relations_as_ids=True`). |
| `meta_override` | A `PydanticMeta`-shaped class to override the model's own, without editing the model — every option it sets, `model_config` included, replaces the model's. |
| `model_config` | A Pydantic `ConfigDict` merged into the generated model's config. |
| `validators` | `{validator_name: field_validator(...)(func)}`, passed through to `pydantic.create_model`'s `__validators__`. |
| `module` | The `__module__` of the generated class — `"hare.contrib.pydantic"` (`PYDANTIC_MODELS_MODULE`) by default. |
| `exclude_sensitive` | Drop every `sensitive=True` field (`Model._meta.sensitive_fields`, see [Sensitive fields](../models/encrypted-and-sensitive-fields.md#sensitive-fields)) — nested relation schemas included. Useful for a public/export schema. Schemas built with and without it are cached separately and never mixed. |
| `relations_as_ids` | Render every forward FK/O2O as its flat `<name>_id` field instead of a nested submodel — see [Relations as ids](#relations-as-ids). |

Generated models are cached and reused for identical `(model, options)` combinations — calling
`pydantic_model_creator(Author)` twice returns the same class.

A field's own ORM-level `Field(validators=[...])` (see [Validators](../models/validators.md)) also
runs automatically in the generated schema — `model_validate()`/construction raises Pydantic's
`ValidationError` if any of them reject the value, in addition to (not instead of) whatever you
pass via the `validators=` parameter above for that same field. A field listed in `optional=`
accepts an explicit `null` (meaning "not provided") — its ORM validators don't run on it.

Value types: a `BinaryField` is carried in JSON as base64 (`model_dump_json()` encodes it,
`model_validate_json()` decodes it, the schema says `format: base64url`), so arbitrary bytes
round-trip; pass your own `ser_json_bytes`/`val_json_bytes` in `model_config` to change that. A
`JSONField` with a declared `field_type` gets that type in the schema (`Any` without one). An
`IntEnumField`/`CharEnumField` is a `$ref` to the enum alone, without the column's numeric range or
length constraints.

A field is **optional** in the generated schema if any of: it's listed in `optional=`, it's
nullable on the model, it has a Python-side `default=`, it has a `db_default=`, it's a non-PK
field explicitly marked `generated=True` (a `GeneratedField`, or any other field the DB fills in —
the caller shouldn't have to provide it, and it's also marked `readOnly` in the JSON Schema), or
it's a `DatetimeField`/`TimeField` with `auto_now=True`/`auto_now_add=True` (the ORM fills it in on
save, same reasoning, also marked `readOnly`) — otherwise it's required. A PK is a separate case:
an auto-increment integer PK is `generated=True` too, but stays required in the schema like every
other PK component, regardless of this rule — unless it's listed in `optional=`, which makes it
optional and nullable like any other field.

A model with a [`CompositePrimaryKey`](../models/field-types.md#compositeprimarykey) works the same way
as one with a single-column pk — every component of the composite key becomes its own field in the
generated schema.

`Meta.tenant_field` is optional too: `save()`/`create()` fill it from the active tenant scope — when
that scope is one value; under [several values](../soft-delete-versions-tenants/multi-tenancy.md#tenancy-scope) the input has to
carry it, and it is checked against the scope. That
covers a tenant FK (`tenant_field = "company_id"` or `"company"` for `company =
fields.ForeignKeyField(...)`) as a nested submodel as well as its `company_id` id field.

A nested forward FK/O2O submodel is `<Submodel> | None` even for a non-null FK when the related
model has `Meta.soft_delete_field`, `Meta.tenant_field` or a custom `Meta.manager` filter — reading
the relation gives `None` for a target that scope hides, so the schema accepts it.

### <a id="relations-as-ids"></a>Relations as ids

By default a forward FK/O2O becomes a nested submodel of the related model. A JSON API usually wants
the flat id instead — `relations_as_ids=True` does that:

```python
class Book(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=200)
    author: fields.ForeignKeyRelation[Author] = fields.ForeignKeyField("models.Author", related_name="books")
    editor: fields.ForeignKeyNullableRelation[Author] = fields.ForeignKeyField("models.Author", null=True)
    tags: fields.ManyToManyRelation[Tag] = fields.ManyToManyField("models.Tag")


BookSchema = pydantic_model_creator(Book, relations_as_ids=True)
# fields: id, title, author_id (UUID, required), editor_id (UUID | None, optional), tags (list[Tag])
BookCreate = pydantic_model_creator(Book, relations_as_ids=True, exclude_readonly=True, exclude=("tags",))
```

- **Field name** is the model's own shadow attribute — `<name>_id` (`book.author_id`), also when the
  FK sets a custom DB column via `source_field=`: the column name changes, the attribute doesn't.
  An FK to a model with a [`CompositePrimaryKey`](../models/field-types.md#compositeprimarykey) gets one
  field per component (`<name>_<pk_component>`).
- **Type** is the target model's pk type (`int`, `UUID`, `str` with its `max_length`, …);
  **nullability and required-ness** follow the FK itself exactly like a plain field: a `null=True`
  FK is `<type> | None` and optional, a non-null FK without a default is required.
- `optional=`, `exclude_readonly=`, `exclude_sensitive=` apply to the id field like to any other
  field. `exclude_readonly=True` keeps it (the caller supplies it on create). A `sensitive=True` FK
  drops its id under `exclude_sensitive=True`.
- `exclude=`/`include=`/`optional=` accept **either** the relation name (`"author"`) or the id field
  name (`"author_id"`) — both select the same field.
- An O2O with `primary_key=True` becomes just its id field, which is the pk; `exclude=("owner",)`
  never removes that pk — only `exclude=("owner_id",)` does.
- **Reverse relations** (reverse FK/O2O) have no column on this row, so a flat schema leaves them
  out entirely — `PydanticMeta.backward_relations` and annotations are ignored.
- **M2M relations stay nested** submodels (`list[TagSchema]`), and still get prefetched.
  Nested submodels (an M2M target's schema) are built with `relations_as_ids=True` too.
- `from_hare_orm()`/`from_queryset()` read the id straight off the instance — no query for the related
  object. Only M2M relations still in the schema are prefetched.
- A schema built with the flag is cached separately from the one built without it; the name of a
  schema built without it doesn't change.

A [`GenericForeignKeyField`](../models/relations.md#genericforeignkeyfield) takes the place of its
branches in a schema: on output a union of its targets' schemas told apart by `type` (the branch
name), on input (`exclude_readonly=True`) and with `relations_as_ids=True`
`{"type": "post", "id": 1}` — a composite key by its key fields. `from_hare_orm()`/`from_queryset()`
prefetch its branches.

## <a id="pydanticmeta"></a>`PydanticMeta` — per-model defaults

Declare a nested class on the hare-orm model itself to set defaults every `pydantic_model_creator()`
call for that model picks up (an explicit function argument still wins):

```python
class Author(Model):
    id = fields.UUIDField(primary_key=True)
    name = fields.CharField(max_length=120)
    books: fields.ReverseRelation["Book"]

    class PydanticMeta:
        exclude = ("some_internal_field",)
        max_recursion = 2
        backward_relations = False  # don't pull in `books` unless explicitly annotated
```

| Option | Default | Meaning |
|---|---|---|
| `include` / `exclude` | `()` / `("Meta",)` | Field allow/deny list. |
| `computed` | `()` | Extra computed fields. |
| `backward_relations` | `True` | Whether reverse FK/O2O relations are included even without an explicit type annotation requesting them. Not recommended: it can pull in a large amount of data unchecked. |
| `max_recursion` | `3` | How many levels deep relation traversal goes before stopping. |
| `allow_cycles` | `False` | Allow self-referential recursion. |
| `exclude_raw_fields` | `True` | Drop the FK shadow column (e.g. `author_id`) once the related object itself is included. |
| `sort_alphabetically` | `False` | Field ordering. |
| `model_config` | `None` | Extra Pydantic `ConfigDict`. |

## <a id="computed-fields"></a>Computed fields

```python
class Author(Model):
    first_name = fields.CharField(max_length=60)
    last_name = fields.CharField(max_length=60)

    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


AuthorSchema = pydantic_model_creator(Author, computed=("full_name",))
```

A plain method, a `@property` or a `functools.cached_property` works. The function's return type
annotation becomes the field's type in the schema. A computed field
that touches an unfetched relation raises `NoValuesFetched` with a message telling you to either
annotate the relation so it gets auto-prefetched, or load it yourself first with `prefetch_related_objects()`.

## <a id="building-instances"></a>Building instances: `PydanticModel` / `PydanticListModel`

Every generated schema inherits from `PydanticModel` (or `PydanticListModel` for the queryset
variant), which adds ORM-aware constructors on top of the usual `model_validate()`:

```python
async def from_hare_orm(cls, obj: Model) -> Self          # one instance, async — auto-prefetches relations
async def from_queryset_single(cls, queryset: QuerySetSingle) -> Self
async def from_queryset(cls, queryset: QuerySet) -> list[Self]
```

```python
author = await Author.objects.get(pk=author_id)
schema = await AuthorSchema.from_hare_orm(author)

schemas = await AuthorSchema.from_queryset(Author.objects.filter(name__icontains="le guin"))
list_schema = await AuthorListSchema.from_queryset(Author.objects.all())
```

All three prefetch whatever relations the schema itself references, so you don't have to call
`select_related()`/`prefetch_related()` yourself just to satisfy the schema's own annotations. Plain
`AuthorSchema.model_validate(author)` also works (that's what `from_attributes=True` gives you for
free), but it raises `NoValuesFetched` naming the field if the schema references an FK/O2O/M2M/
backward-relation field you haven't already loaded on `author` — even a nullable FK whose value is
genuinely `None` still needs an explicit fetch, since there's no other way to tell "confirmed empty"
apart from "never checked". Use one of the three methods above instead of prefetching by hand.

## <a id="modeldescription"></a>`ModelDescription` — a model's fields by type

```python
from hare.contrib.pydantic.descriptions import ModelDescription

description = ModelDescription.from_model(Book)
```

What the creator builds a schema from: a model's fields grouped by type, each group a list of
`Field` objects in declaration order — `pk_fields` (one per component of a composite key),
`data_fields` (every other field holding a plain value, the key columns of forward relations such as
`author_id` included), `foreign_key_fields`, `backward_foreign_key_fields`, `one_to_one_fields`, `backward_one_to_one_fields` and
`many_to_many_fields`. The model's relations must be initialised (`Hare.init()` or `Hare.bind_models()`).

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

Wraps the single-item schema in a `RootModel`-based list schema (named `<Model>_list` by default) —
useful as the return-type annotation for a "list all X" endpoint. `name=` names the list schema
only; the item schema keeps its default name. Every other argument builds the item schema, as
in `pydantic_model_creator()`.
