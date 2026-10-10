# Type checking queries

The mypy plugin `hare.contrib.mypy` checks queries against the project's models while mypy checks
the code: a filter key the model doesn't have, a lookup its field doesn't have, a value of the
wrong type, a misspelled name in `order_by()` or `only()`, a field the constructor doesn't know.
Each of these would otherwise be found only when the query runs. The plugin also gives
`values()` and `values_list()` rows real types instead of `dict[str, Any]` and `tuple[Any, ...]`.

The plugin reads the models the way hare does at runtime — through `Model._meta.get_lookup_info()`
([Describing filters and orderings](describing-filters.md)) — so what it accepts is what a query
accepts, its error texts are the ones the query would raise, and lookups, transforms and fields a
project registers itself are checked like the built-in ones.

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
hare_orm = "myproject.settings.HARE_CONFIG"
```

At the start of every mypy run the plugin imports the configuration and binds its models without a
database (`Hare.bind_models()`): their relations, swappable models and lookups. The directory of
mypy's configuration file is put on the Python path for the import. Each model's lookups are
checked against the dialect of its `default_connection`.

### <a id="mypy-imports"></a>Lookups registered outside the models

A lookup, transform or field class registered while the model modules are imported is seen as it
is. One registered elsewhere — in an application's startup code, say — needs its module named, so
the plugin imports it before binding the models:

```toml
[tool.hare]
hare_orm = "myproject.settings.HARE_CONFIG"
mypy_imports = ["myproject.lookups"]
```

`mypy_imports` is a list of module names.

### <a id="editor"></a>In the editor

The plugin runs wherever mypy runs: the command line, `dmypy`, and editors running mypy — in VS Code
the [Mypy Type Checker](https://marketplace.visualstudio.com/items?itemName=ms-python.mypy-type-checker)
extension, which reads the same `pyproject.toml`.

## <a id="checked"></a>What is checked

Every error the plugin reports has the code `hare-query`, so one line is silenced with
`# type: ignore[hare-query]`.

### <a id="filters"></a>Filters

The keyword arguments of `filter()`, `exclude()`, `get()`, `get_or_create()` and
`update_or_create()` — on `Model.objects`, a related manager or a queryset of one's own class:

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
| a relation to a composite key, `pk` of a composite key | a tuple of the key's types, or an instance |
| a generic foreign key (`subject`) | an instance of one of its models |
| `subject__type` | one of the branch names — `Literal["post", "photo"]` |
| a JSON path (`data__owner__name`) | anything |
| a registered lookup | its `value_shape` and `value_type` (the field's type when None) |
| a registered transform (`rating__as_text__startswith`) | the type of the field the transform reads |
| an annotation | its type (see below) |

Equality (`title=None`) also takes None when the value may be NULL: the field is nullable, or the key
crosses a nullable relation or one holding many rows. Every key also takes an expression (`F()`, a
function, an aggregate), a term or a subquery (a queryset).

A lookup that exists but doesn't run on the model's database is reported too:

```python
Writer.objects.filter(rating__within_on_postgresql=(1, 2))
# error: Filter param 'rating__within_on_postgresql': Writer.rating has no lookup
#        'within_on_postgresql' on the sqlite database  [hare-query]
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

- `values()` without arguments — every stored field of the model (a relation by its column,
  `author_id`) and every annotation that isn't an `.alias()`.
- `values(name="title")` selects the path under the name; `values(total=Sum("price"))` an
  expression, typed as an annotation.
- A value crossing a nullable relation or one holding many rows may be None.
- A name that isn't a field path — or is a lookup, `title__icontains` — is reported.
- `annotate()` after `values()` adds its names to the row.
- `get()`, `first()`, `last()` and iteration give the row type.
- A `values_list(named=True)` row stays `Any`: its class is made at runtime.

### <a id="annotations"></a>Annotations

`annotate()` and `alias()` add their names to the queryset type — filter keys, orderings and
`values()` names may then name them:

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
annotated queryset is still a `QuerySet[Book]`. A queryset class of one's own that doesn't pass the
parameter on (`class BookQuerySet(QuerySet[Book])`) keeps the annotations out of sight instead,
along with its own methods.

A function taking a queryset with annotations names them in the third parameter, or takes `Any`
there — then any name is accepted as a possible annotation:

```python
def popular(books: QuerySet[Book, Book, Any]) -> QuerySet[Book, Book, Any]:
    return books.filter(chapter_count__gte=10)
```

### <a id="names"></a>Names

The names given as string literals to:

| Method | A name is |
|---|---|
| `order_by()` | a field path or an annotation, with an optional `-`; `?` orders randomly |
| `only()` | a field path or an annotation |
| `defer()` | a direct field of the model — not a relation |
| `select_related()` | a path of forward relations and reverse one-to-one relations, or a generic foreign key |
| `bulk_update(fields=...)` | a field of the model's rows — not a relation holding many rows |

### <a id="writes"></a>Writes

The keyword arguments of the model's constructor, `create()` and `update()` set fields: by the
field's name, a relation's column (`author_id`), or `pk`. An enum field takes a member or its value, a relation an instance of its model
(None when it is nullable), a generic foreign key an instance of one of its models, and `update()`
also an expression:

```python
Book(title="War and Peace", author=tolstoy)
await Book.objects.filter(id=1).update(price=F("price") * 2)

Book(titel="War and Peace")
# error: Book has no field 'titel' to set  [hare-query]
Book(chapters=[chapter])
# error: Book.chapters is a relation holding many rows - it can't be set  [hare-query]
```

### <a id="dialect-methods"></a>Methods of a dialect

A dialect's own `QuerySet` methods — ClickHouse's `final()`, `prewhere()`, `limit_by()`, ... (see
[Writing a dialect](../extending/writing-a-dialect.md#queryset-methods)) — are known to mypy for the
dialects of the project's connections: each takes the parameters of its call and returns the
queryset. A method only another database's dialect has is an unknown attribute:

```python
PageView.objects.final()                # ClickHouse connection: ok
PageView.objects.limit_by()             # error: Missing positional argument "limit" in call to "limit_by"
Book.objects.final()                    # SQLite connection: error: "QuerySet[...]" has no attribute "final"
```

`hare stubs` declares them on each model's queryset the same way, a method taking a condition with the
filter keys of the model.

## <a id="not-checked"></a>What isn't checked

- `Q(...)` and `F("...")` — they aren't tied to a model when they are made. A `Q` is checked when the
  query runs; an `F` given as a filter value is accepted for any key.
- Names that aren't literals: `filter(**conditions)`, `values(*names)`. A queryset whose
  annotations came from `annotate(**expressions)` accepts any name as an annotation.
- Raw SQL.
- Reverse relations are attributes mypy sees only when the model declares them —
  `books: fields.ReverseRelation["Book"]` ([Relations](../models/relations.md)).

## <a id="cache"></a>When the models change

mypy keeps the results of a run for the next one. The plugin gives mypy a digest of everything it
reads from the models — fields, relations, nullability, registered lookups and transforms,
dialects and their `QuerySet` methods — and a change to any of them makes mypy check the modules
again.

## <a id="errors"></a>When the models can't be loaded

A configuration that can't be found or imported, or a model module that fails, is reported once in
each checked module, and the queries are then typed as without the plugin:

```text
error: hare: the models can't be loaded for type checking - ConfigurationError: Cannot import
       configuration module 'myproject.settings' ...  [hare-query]
```

## <a id="pyright"></a>pyright and Pylance: `hare stubs`

pyright — and Pylance, VS Code's Python extension built on it — runs no plugins. It reads stubs
instead: `hare stubs` (`pip install hare-orm[pyright]`) writes one for each module of models into
`typings/`, the directory pyright looks in first:

```bash
hare stubs                       # typings/myapp/models.pyi, ...
hare stubs --relation-depth 3    # filter keys crossing up to 3 relations (0 to 5; 2 by default)
hare stubs --check               # exit 1 when a stub is missing or outdated - for CI
hare stubs --output stubs         # another directory than typings/
```

A stub is the whole module as mypy's stubgen writes it — pyright reads the stub instead of the
module, so its other names stay — with each model's fields typed (`name: Field[str]`, a relation
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
| `filter()`, `exclude()`, `get()`, `get_or_create()`, `update_or_create()` | `<Model>Filters`: every key of the model's fields, the fields of the models its relations lead to (up to `--relation-depth`), with each lookup the connection's dialect runs, and the type of its value — the same as the mypy plugin's. |
| `create()`, `update()` | `<Model>Writes`: each field, a relation's object or key. |
| `values_list(name, flat=True)` | The type of the field selected. |
| A model's fields | `Field[<value>]` — the field on the class, its value on an instance. |

Run `hare stubs` again after changing the models; `--check` in CI catches a stub left behind. The
mypy plugin checks more than a stub can say: the rows of `values()` and of `values_list()` with
several names, annotations, names of `order_by()`/`only()`, the expressions in a write. A model
declared generic in the class (`JSONField[MyDict]`) gets the value type of its field class at run
time in the stub.
