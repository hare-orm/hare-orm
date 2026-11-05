# Type checking queries

The mypy plugin `hare.contrib.mypy` checks queries against the project's models while mypy checks the
code: a filter key the model doesn't have, a lookup its field doesn't have, a value of the wrong type, a
misspelled name in `order_by()` or `only()`, a field the constructor doesn't know. Each of these would
otherwise be found only when the query runs. The plugin also gives `values()` and `values_list()` rows
real types instead of `dict[str, Any]` and `tuple[Any, ...]`.

The plugin reads lookup descriptions through `Model._meta.get_lookup_info()`
([Describing filters and orderings](describing-filters.md)) and shares field input and read types with
the stub generator. Registered lookups, transforms and fields participate in those descriptions too.
Static checks use these contracts without executing queries or validating actual field values;
their limits are listed under [What isn't checked](#not-checked).

## <a id="setup"></a>Setup

```bash
pip install "hare-orm[mypy]"
```

Turn the plugin on in mypy's configuration and tell it where the hare configuration is — the same
`[tool.hare] hare_orm` setting the `hare` command reads (or the `HARE_ORM` environment variable):

```toml
# pyproject.toml
[tool.mypy]
plugins = ["hare.contrib.mypy"]

[tool.hare]
hare_orm = "library.settings.HARE_CONFIG"
```

At the start of every mypy run the plugin imports the configuration and binds its models without a
database (`Hare.bind_models()`): their relations, swappable models and lookups. The directory of mypy's
configuration file is put on the Python path for the import. Each model's lookups are checked against the
dialect of its `default_connection`.

### <a id="mypy-imports"></a>Lookups registered outside the models

A lookup, transform or field class registered while the model modules are imported is seen as it is. One
registered elsewhere — in an application's startup code, say — needs its module named, so the plugin
imports it before binding the models:

```toml
[tool.hare]
hare_orm = "library.settings.HARE_CONFIG"
mypy_imports = ["library.lookups"]
```

`mypy_imports` is a list of module names.

### <a id="editor"></a>In the editor

The plugin runs wherever mypy runs: the command line, `dmypy`, and editors running mypy — in VS Code the
[Mypy Type Checker](https://marketplace.visualstudio.com/items?itemName=ms-python.mypy-type-checker)
extension, which reads the same `pyproject.toml`.

## <a id="checked"></a>What is checked

Model-aware validation errors reported by the plugin have the code `hare-query`, so one line is
silenced with `# type: ignore[hare-query]`. mypy also checks the types and signatures supplied by the
plugin; those diagnostics retain mypy's codes, such as `arg-type` and `assignment`.

### <a id="filters"></a>Filters

The keyword arguments of `filter()`, `exclude()`, `get()`, `get_or_create()` and `update_or_create()` —
on `Model.objects`, a related manager or a queryset of one's own class:

```python
Book.objects.filter(title__icontains="war", author__born__year__gte=1800)

Book.objects.filter(titel="War")
# error: Unknown filter param 'titel': Book has no field 'titel'  [hare-query]
Book.objects.filter(title__nope="War")
# error: Unknown filter param 'title__nope': Book.title has no lookup 'nope'  [hare-query]
Book.objects.filter(published__year="1869")
# error: Argument "published__year" to "filter" of "QuerySet" has incompatible type "str";
#        expected "int | Expression | Term | QuerySpecification[Any]"  [arg-type]
```

The value a key takes:

| Key | Value |
|---|---|
| a field (`title`), a lookup comparing its value (`title__gte`, `title__icontains`) | the field's type as the model declares it; an enum field (`CharEnumField(Status)`) takes a member or its value (`"open"`) |
| a typed JSON field (`JSONField[Address]`) | the declared type for equality (`address={...}`); its other lookups (`address__contains`) take any part of a document |
| a list lookup (`title__in`) | an iterable of that type |
| `__range` | a two-item tuple or list of it; a None bound leaves that side open |
| `__isnull` | `bool` |
| a date part (`published__year`), `__len` | `int` |
| `published__date`, `published__time` | `date`, `time` |
| a forward relation (`author`, `author__in`) | its key, or an instance of the related model |
| a relation to a composite key | a tuple of the key's types, or an instance of the related model |
| `pk` of a composite key | a tuple of the key's types; an iterable of tuples for `__in` |
| a generic foreign key (`subject`) | an instance of one of its models, or a dict `{"type": ..., "id": ...}` |
| `subject__type` | one of the branch names — `Literal["book", "author"]` |
| a JSON path (`data__owner__name`) | anything |
| an address field (`IPAddressField`, PostgreSQL's `InetField`, `CidrField`) | its text, or an `ipaddress` object |
| a range field (`period`, `period__overlap`, `period__contained_by`) | a `Range` of the bound type, or its `(lower, upper)` bounds (None for an open side) |
| `period__contains` | a value of the bound type, a `Range`, or its `(lower, upper)` bounds |
| a range's path (`period__startswith`, `period__endswith`, `period__isempty`, `period__lower_inc`, ...) | the bound type; `bool` for a flag |
| a geometry field's `dwithin` and distance lookups (`point__distance_lte`) | `(geometry, distance)` |
| a geometry field's `relate` | `(geometry, pattern)` |
| a geometry field's other spatial relations (`area__contains`) | a geometry |
| a registered lookup | its `value_shape` and `value_type` (the field's type when None) |
| a registered transform (`rating__as_text__startswith`) | the type of the field the transform reads |
| an annotation | its type (see below) |

Equality (`title=None`) also takes None when the value may be NULL: the field is nullable, or the key
crosses a nullable relation or one holding many rows. Every key also takes an expression (`F()`, a
function, an aggregate), a term or a subquery (a queryset).

A lookup that exists but doesn't run on the model's database is reported too:

```python
Author.objects.filter(rating__within_on_postgresql=(1, 2))
# error: Filter param 'rating__within_on_postgresql': Author.rating has no lookup
#        'within_on_postgresql' on the sqlite database  [hare-query]
```

So is a field the database has no column type for — an `ArrayField` of a model on SQLite:

```python
Book.objects.filter(keywords=["a"])
# error: Filter param 'keywords': Book.keywords has no column type on the sqlite database  [hare-query]
```

### <a id="values"></a>Rows of `values()` and `values_list()`

```python
rows = await Book.objects.values("id", "title", "author__name", "shelf__label")
reveal_type(rows[0])
# TypedDict({'id': int, 'title': str, 'author__name': str, 'shelf__label': str | None})

pairs = await Book.objects.values_list("id", "title")       # list[tuple[int, str]]
titles = await Book.objects.values_list("title", flat=True)  # list[str]
first = await Book.objects.values_list("title", flat=True).first()  # str | None
```

- `values()` without arguments — every stored field of the model (a relation by its column, `author_id`)
  and every annotation that isn't an `.alias()`.
- `values(name="title")` selects the path under the name; `values(total=Sum("price"))` an expression,
  typed as an annotation.
- A value crossing a nullable relation or one holding many rows may be None.
- A container field gives its whole value — `list[str]` for `ArrayField(TextField())`, `dict[str, int]`
  for a `MapField`, `tuple[int, str]` for a `TupleField`, a `Range` for a range field — though its
  equality filter takes something else (the elements of an array).
- A name that isn't a field path — or is a lookup, `title__icontains` — is reported.
- `annotate()` after `values()` or `values_list()` adds new selected names. Replacing an annotation
  updates every selected occurrence, including renamed dictionary outputs; `alias()` adds no columns.
  `flat=True` retains its single selected value, including when that value is a composite key tuple.
- `get()`, `first()`, `last()` and iteration give the row type.
- A `values_list(named=True)` row stays `Any`: its class is made at runtime.

### <a id="annotations"></a>Annotations

`annotate()` and `alias()` add their names to the queryset type — filter keys, orderings and `values()`
names may then name them:

```python
books = Book.objects.annotate(chapter_count=Count("chapters"), total=Sum("price"))
books.filter(chapter_count__gte=10).order_by("-total")
```

| Expression | Type |
|---|---|
| `Count`, `Length` | `int` |
| `Exists` | `bool` |
| `Sum`, `Min`, `Max` | the field's type, or None (no rows) |
| `F("path")` | the type of the path |
| `Value(x)` | the type of `x` |
| anything else | `Any` |

The names and types are kept in the queryset's third type parameter as a `TypedDict` —
`QuerySet[Book, Book, TypedDict({'chapter_count': int, ...})]`. The parameter is covariant, so an
annotated queryset is still a `QuerySet[Book]`. For a custom queryset subclass
(`class BookQuerySet(QuerySet[Book])`), the plugin stores annotation types in mypy's instance metadata.
The subclass and its custom methods remain available.

A function taking a queryset with annotations names them in the third parameter, or takes `Any` there —
then any name is accepted as a possible annotation:

```python
def popular(books: QuerySet[Book, Book, Any]) -> QuerySet[Book, Book, Any]:
    return books.filter(chapter_count__gte=10)
```

### <a id="names"></a>Names

The names given as string literals to:

| Method | A name is |
|---|---|
| `order_by()` | a field path or an annotation, with an optional `-`; `?` orders randomly |
| `only()` | a field path or an annotation — not one ending at a relation holding many rows |
| `defer()` | a direct field, forward relation or generic foreign key; no reverse or to-many relation |
| `select_related()` | a path of forward relations and reverse one-to-one relations, or a generic foreign key |
| `bulk_update(fields=...)` | a field of the model's rows — not a relation holding many rows |

### <a id="writes"></a>Writes

`update()` names follow the runtime assignment rules: no whole primary key, generated column,
optimistic-lock version, soft-delete marker or blind-index column. Individual columns of a composite
key remain assignable. Relation names are checked against all the columns they write. Constructor
and `create()` descriptions keep their separate input contract.

The keyword arguments of the model's constructor, `create()` and `update()` set fields: by the field's
name or a relation's column (`author_id`). The constructor and `create()` also accept `pk`; `update()`
follows the restrictions above. An enum field takes a member or its value, a relation
an instance of its model (None when it is nullable), a generic foreign key an instance of one of its
models or a dict `{"type": ..., "id": ...}`, and `update()` also an expression:

```python
Book(title="War and Peace", author=tolstoy)
await Book.objects.filter(id=1).update(price=F("price") * 2)

Book(titel="War and Peace")
# error: Book has no field 'titel' to set  [hare-query]
Book(chapters=[chapter])
# error: Book.chapters is a relation holding many rows - it can't be set  [hare-query]
```

### <a id="attributes"></a>Attributes of an instance

The attributes of an instance are typed from the model's fields, whatever the class declares:

- a field's value, None included when the field is nullable — `TextField(null=True)` is `str | None`;
- a container field's whole value — `list[str]` for `ArrayField(TextField())`; a JSON field without a
  declared type is `Any` (`JSONField(default=dict)` needs no annotation);
- a relation's key column — `author_id` is `int`, `int | None` for a nullable relation;
- a generic foreign key's object — an instance of one of its models; an assignment also takes a branch's
  row as a dict and None;
- `pk` — the key field's type (the related model's key for a key that is a relation), a tuple of the key
  fields' types for a composite key; an assignment also takes None, the key of an unsaved copy.

```python
book.pk                 # int
edition.pk              # tuple[int, int] - pk = CompositePrimaryKey("book", "number")
book.author_id          # int
comment.target          # Book | Author
book.pk = None
book.pk = "1"           # error: Incompatible types in assignment (expression has type "str",
                        #        variable has type "int | None")  [assignment]
```

### <a id="dialect-methods"></a>Methods of a dialect

A dialect's own `QuerySet` methods — ClickHouse's `final()`, `prewhere()`, `limit_by()`, ... (see
[Writing a dialect](../extending/writing-a-dialect/sql.md#queryset-methods)) — are known to mypy for the
dialects of the project's connections: each takes the parameters of its call and returns the queryset.
The plugin exposes their combined methods on the shared `QuerySet` type. In a project configured
with both ClickHouse and SQLite, mypy therefore accepts `final()` on either model; it does not
check the model's dialect for that call. A method is absent when none of the configured dialects
provides it:

```python
PageView.objects.final()                # ClickHouse connection: ok
PageView.objects.limit_by()             # error: Missing positional argument "limit" in call to "limit_by"
Book.objects.final()                    # also accepted with ClickHouse configured in the project
```

`hare stubs` selects methods for each model's dialect. Its generated SQLite queryset has no `final()`;
a generated method taking a condition accepts the model's filter keys.

## <a id="not-checked"></a>What isn't checked

- `Q(...)` and `F("...")` — they aren't tied to a model when they are made. A `Q` is checked when the
  query runs; an `F` given as a filter value is accepted for any key.
- Names that aren't literals: `filter(**conditions)`, `values(*names)`. A queryset whose annotations came
  from `annotate(**expressions)` accepts any name as an annotation.
- Raw SQL.
- After `first()`/`get()` and other single-row selectors, `QuerySetSingle` retains the result type,
  but the plugin no longer checks new field names or infers precise rows for subsequent
  `values()`/`values_list()` calls. Ordinary Python signatures are still checked.
- Reverse relations are attributes mypy sees only when the model declares them —
  `books: fields.ReverseRelation["Book"]` ([Relations](../models/relations.md)).

## <a id="cache"></a>When the models change

mypy keeps the results of a run for the next one. The plugin gives mypy a digest of everything it reads
from the models — fields, relations, nullability, registered lookups and transforms, dialects and their
`QuerySet` methods — and a change to their typing contracts makes mypy check the modules again.
This includes container element types, accepted input representations, composite key order and
generic relation branches. Replacing a registered lookup or transform under the same name also
invalidates the cache when its input or output types change. Runtime defaults are not serialized
into this digest; extension parameters record whether a default exists, not the default value.

## <a id="errors"></a>When the models can't be loaded

A configuration that can't be found or imported, or a model module that fails, is reported once in each
checked module, and the queries are then typed as without the plugin:

```text
error: hare: the models can't be loaded for type checking - ConfigurationError: Cannot import
       configuration module 'library.settings' ...  [hare-query]
```

## <a id="pyright"></a>pyright and Pylance: `hare stubs`

The mypy plugin and generated stubs share field input descriptions. These include supported input
conversions, such as UUID text and ISO date strings, separately from the values returned by reads.
The `pk` alias uses the same inputs as its column; composite keys describe each component separately.
Type checking does not validate the contents of a string or a field's numeric bounds.

pyright — and Pylance, VS Code's Python extension built on it — runs no plugins. It reads stubs instead:
`hare stubs` (`pip install hare-orm[pyright]`) writes one for each module of models into `typings/`, the
directory pyright looks in first:

```bash
hare stubs                       # typings/library/models.pyi, ...
hare stubs --relation-depth 3    # filter keys crossing up to 3 relations (0 to 5; 2 by default)
hare stubs --check               # exit 1 when a stub is missing or outdated - for CI
hare stubs --output stubs         # another directory than typings/
```

A stub starts with the whole module produced by mypy's stubgen. LibCST updates class declarations
structurally, preserving other scopes and comments. Both tools are installed by the `pyright` extra.
Pyright reads the stub instead of the module, so its other names stay.
Package modules keep their layout: models in `library/__init__.py` produce
`typings/library/__init__.pyi`, alongside stubs for child modules.
Each model's fields are typed (`name: Field[str]`, a relation
`author: Field[Author]`) and its queryset typed for its own keys and values:

```python
await Book.objects.filter(titel="x")
# error: No parameter named "titel"
await Book.objects.filter(published__year="2020")
# error: Argument of type "Literal['2020']" cannot be assigned to parameter "published__year"
#        of type "int | Expression | Term | QuerySpecification[Unknown]"
await Book.objects.create(title=1)
# error: Argument of type "Literal[1]" cannot be assigned to parameter "title" of type "str"
titles = await Book.objects.values_list("title", flat=True)  # list[str]
```

| Typed | Through |
|---|---|
| `filter()`, `exclude()`, `get()`, `get_or_create()`, `update_or_create()` | `<Model>Filters`: every key of the model's fields, the fields of the models its relations lead to (up to `--relation-depth`), with each lookup the connection's dialect runs, and the type of its value — the same as the mypy plugin's; a generic foreign key with `__type`, `__in`, `__isnull` and their negations. |
| `create()` | `<Model>Writes`: each field, a relation's object or key, a generic foreign key's object or its row as a dict, and `pk`. |
| `update()` | `<Model>Updates`: assignable fields with their input values or expressions (`F("price") * 2`, a function, a subquery). |
| `values_list(name, flat=True)` | The read type of a direct stored field selected by a literal name, including relation key columns. Paths through relations and expression outputs use the general return type. |
| A model's fields | `Field[<value>]` — the field on the class, its value on an instance; the fields of an abstract base the same. |
| `objects` and the model's other managers | `<Model>QuerySet`, built on the project's own queryset class of the default manager — its methods stay; a manager of another queryset class is that class. |
| `pk` of an instance | The key's type, a tuple for a composite key; an assignment also takes None. |

A declaration whose name the module holds already (a model named `BookFilters` beside `Book`) gets an
underscore after it: `BookFilters_`. Imported names, including builtins, are aliased when shadowed;
the alias also takes a free name. Runtime field types retain nested `Literal` values (including enum
members) and `None`; `Annotated` contributes its underlying type.
Run `hare stubs` again after changing the models; `--check` in CI finds missing or outdated stubs for
the currently configured model modules. Neither mode detects or removes leftover stubs of modules
that are no longer configured. The mypy plugin checks more than a stub can say: the rows of `values()` and
of `values_list()` with several names, annotations, names of `order_by()`/`only()`, the expressions in a
write. A field whose type is declared with a generic annotation (`JSONField[MyDict]`) gets its field
class's runtime value type in the stub.
