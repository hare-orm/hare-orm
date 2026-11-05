# Query plan cache

hare caches the *structure* of a query — its **shape** — so that calling the same `.filter()`/
`.annotate()`/`.order_by()` chain again with different values skips re-walking the filter tree,
re-resolving every lookup, and rebuilding the underlying `hare.sql` query object from scratch. This is a
fully internal optimization: there's no `Meta` option and no `Hare.init()` kwarg for it — the only knob
is an environment variable capping its size (see [Scope and lifetime](#scope-and-lifetime) below). This
page exists to explain what it does and doesn't cover, so a query's actual behavior is never a mystery.

## <a id="shape-vs-value"></a>The core idea: shape vs. value

Two calls have the **same shape** when they build the identical filter/annotation/join/ordering
*structure* — same fields, same operators, same relations joined — differing only in the literal values
plugged into that structure:

```python
await Book.objects.filter(title__icontains="dispossessed")
await Book.objects.filter(title__icontains="left hand")   # same shape, different value
```

The first call builds the full `hare.sql` query object (WHERE tree, JOINs, SELECT list, decode plan),
renders it once, and stores the result as the shape's **plan**, keyed by everything that makes up the
*shape*: the SQL text, which parameter each value of the query goes into, and what reading the result
needs. The second call recognizes the same shape and runs the plan's SQL text with its own values bound —
never re-walking `Q`/`F`/annotation resolution, re-touching the join graph, or rendering SQL.

A query of a shape with a plan is built in full when it can't run on the plan: one shown with
`.sql()`/`explain()` or built into another query that keeps no plan of its own, and one with a value that
would change the SQL text (a value converting to another type, a `LIMIT` present on one side only). The
plan stays for the next query. A `None` filter value and the boolean of `__isnull` are part of the shape
— `IS NULL`/`IS NOT NULL` queries keep plans of their own.

**Pagination is the headline real-world case**: the `LIMIT`/`OFFSET` values are never part of the shape
key and are bound fresh on every call (only whether the query has a `LIMIT` and an `OFFSET` at all is
part of it), so paging through the same filtered/ordered list — easily the single most common query
pattern in an application — hits the cache on every page:

```python
base = Book.objects.filter(in_print=True).order_by("-published_at")
await base.limit(20).offset(0)     # builds and caches the shape
await base.limit(20).offset(20)    # same shape, cache hit
await base.limit(20).offset(40)    # same shape, cache hit
```

Keyset boundaries on JSON fields also bind fresh values into the saved plan, using the same JSON
ordering as `order_by()`. This applies to `after_cursor()` and `before_cursor()`, including their
`values()`, `count()` and `exists()` queries.

The cached plan retains SQL text, immutable parameter slots and result metadata. It does not
retain the first query's SQL tree or its replaceable input values. Repeated occurrences share the
same fresh converted value, and sensitive parameter positions stay hidden on every execution.

## <a id="shape-key"></a>What's part of the shape (the cache key)

The model class, the dialect, the connection alias and declared native result types, the query builder
the model is bound to, which columns/relations are selected
(`select_related`/`.only()`/`.defer()`/`.values()`), ordering,
`distinct()`, the WHERE tree's *structure* (join type, negation, which field/lookup at which position —
never the compared value), annotation *names and expression structure* (never literal annotation values)
and whether each one is an `.alias()`, the `select_for_update()` flags, how many
`.after_cursor()`/`.before_cursor()` boundary values are given and which of them are `None` (a `None`
boundary is an `IS NULL`/`IS NOT NULL` test with no value), whether the rows are sliced (a `LIMIT`, an
`OFFSET` — present or not, never their values), and the zone the query renders into its SQL — the active
timezone name under `use_timezone`, the machine's own zone (as `tzlocal` names it) without it. See
[Correctness notes](#correctness-notes) below for why.

`Model.objects.get(**filters)` runs on the same entry as the queryset it stands for, whatever its
exception parameters, found by the filter keys and the types of their values — see
[`get()`](queryset-methods.md#get). A model with `Meta.soft_delete_field` or `Meta.tenant_field` takes
this path too (the default scope's filters are part of the key, the tenant bound is the one active when
the query runs), and so do `get()` after `select_for_update()`, `include_deleted()`, `only_deleted()`,
`all_tenants()` and `using()` — the lock and the rows seen are part of the key. A `get()` of plain values
is a call of its own; one with a `None`, an `__isnull` boolean or an `__in` list takes the `filter()`
call it stands for. Like every queryset, it chooses its connection when it is awaited.

A queryset made from `Model.objects` by simple calls alone — `filter()`/`exclude()` with `Q(...)` and
`Exists(...)` conditions among them (`filter(Q(pages=3) | Q(title="x"))`, the tree's joins and negations
part of the key) and values that are plain, expressions (`F("other") + 1`) or queries
(`id__in=Subquery(...)`, a queryset), `annotate()`/`alias()` of expressions, `distinct()`, `order_by()`
of field names, `limit()`/`offset()`, `first()`/`last()`, `get()`, `only()`, `select_related()`,
`values()`/`values_list()` of field names, `group_by()`, `prefetch_related()`, and
`count()`/`exists()`/`aggregate()` at the end — runs on an entry found by the calls themselves: its
filters aren't even built. Both a cache hit and a fresh compilation run through the same statement
executor. The plan supplies SQL, parameters and row layout; the executor applies the query's result
shape, single-row policy and relation loading. Each call's values — a filter's, an expression's literals — are
found
among the entry's values by where they come from, so the entry's own order of values (the annotations'
before the filters') doesn't have to be the order of the calls. A `prefetch_related()` call keys the
relations it prefetches — a forward relation's key column is selected for its prefetch even past `only()`
— and its prefetch queries run as they do for any queryset. A relation read off an instance
(`author.books.filter(...)`) is a call chain too, its owner's key bound as a value. Any other call (a
`hare.sql` term built by hand as a value) leaves the queryset to the usual description above. The entry
is the same one — a call chain finds the entry its first query built, and an entry dropped from the cache
isn't found by the calls either.

A forward relation compared with a key value (`author=3`, `author__gt=3`), an instance (`author=obj`) or
a list of keys or instances (`author__in=[...]`) keeps its entry — an instance stands for its key and is
of another type than a key value, so it has an entry of its own. A relation to a composite key compares
each key column, the instance or the key tuple bound column by column. A generic foreign key
(`target=obj`, `target__in=[...]`, `target__type="book"`, `target__type__in=[...]`,
`target__isnull=True`) compares the branch the value names — the branch's name is part of the key. A
filter on a to-many relation's own name (`tags=obj`, `tags__in=[...]`, `books=obj`) and reading a
many-to-many relation (`obj.tags.all()`) keep their entry, an instance standing for its primary key; so
do a composite primary key's `pk__in=[(a, b), ...]` (by the number of rows), `__iexact`, PostgreSQL's
`__search`, and filters on a JSON path (`data__a=`, `data__a__gt=`, `data__a__in=`).

A one-sided or fully-open `__range` (`(None, 10)`, `(None, None)`) keeps an entry for each open side, its
remaining bound a value. `.contains(obj)` keeps an entry like `exists()`, the instance's key bound per query.
A lookup that derives SQL structure from a JSON document or a container is cached only when every
input has a declared parameter conversion; otherwise it uses normal query preparation.

A filter whose value is a query — `field__in=Other.objects.filter(...).values_list("id", flat=True)`, a
bare queryset (compared by its primary key), `Subquery(...)` — keeps its entry with the subquery in it:
the key holds the subquery's own shape, and the subquery's values, its `LIMIT`/`OFFSET` included, are
bound with the enclosing query's. That holds for a filter on a window function and a union as the
subquery too. A subquery that keeps no entry of its own
shape (see [the table below](#uncached-shapes)) is built each time, and so is the query it is the value
of.

A query with CTEs (`.with_cte(name, query)`) — a model query, `.values()`/`.values_list()`, `count()`,
`exists()`, `aggregate()` — keeps its entry with them the same way: the key holds each CTE body's shape
and the body's values are bound with the query's own. A `RawSQL` filter value (the documented way to read
a CTE, `filter(id__in=RawSQL('SELECT "id" FROM "cte"'))`) is part of the shape by its SQL text; its
`parameters` are values. `update()` and `delete()` with CTEs keep their entry the same way.

A set operation — `union()`, `intersection()`, `difference()` of querysets or of `.values()`/
`.values_list()` queries, and `count()`/`exists()` over one — keeps an entry of the whole combined
statement: the key holds each branch's shape (a nested set operation's included), the operations, the
ordering and whether it is sliced; every branch's values and the slice are bound per query. A set
operation with a branch that keeps no entry of its shape is built each time.

A query calling a dialect's QuerySet methods — ClickHouse's `.final()` or `.limit_by(3, "author_id")` —
keeps an entry with the calls written into its SQL text: the key holds each method's name and arguments,
so `.limit_by(3, "author_id")` and `.limit_by(5, "author_id")` are two shapes. A call with an argument
that can't be part of a key (a list, a dict) builds the query each time. A query run on its entry still
checks that its connection's dialect implements every called method.

`count()`, `exists()` and `aggregate()` of the rows a `.values()`/`.values_list()` query or a set
operation returns — sliced (`[2:5].count()`), `.distinct()` — keep an entry of the whole statement over
those rows: the key holds the rows query's shape (and each `aggregate()` metric's structure), its values
and the `OFFSET` `exists()` skips are bound per query. An `aggregate()` metric holding a value of its own
(`Sum("price", _filter=Q(...))`, `Coalesce(Max("price"), 0)`) over such rows has its values bound after
the rows query's.

`aggregate()` keeps an entry whatever builds it: prior `.annotate()`/`.alias()` keys the metrics or
filters read, a grouped derived table for an aggregate annotation
(`.annotate(n=Count(...)) .aggregate(Max("n"))`) or `.distinct()`, a `.group_by()`, a keyset boundary.
Which annotations each step of the build resolves follows from the shape and is kept with the entry, so a
later query lists its values in that order without resolving anything. Over a `.group_by()`, a metric
holding a value of its own has it bound the same way.

An annotation reading another one by name — `F("bumped") * 2`, `Sum("bumped")`, a filter
`bumped__gt=...`, a window's `partition_by=`/`order_by=` or the field of `Lag`/`Lead` — keeps its entry:
resolving the name resolves that annotation again, and its values are bound at every place it is
resolved.

A window function keeps its entry whatever it computes — `RowNumber()`, `CumeDist()`, `PercentRank()`,
`NthValue(...)`, `NTile(n)`, `Lag`/`Lead` with a default or without one, an aggregate over the window
(`Window(Sum("price"))`, over an expression or with a `_filter=` condition). An expression whose SQL
takes no value — `Pi()`, `Now()`, PostgreSQL's `TransactionNow()` and `RandomUUID()` — keeps its entry
too. A `None` argument — `Case` without `default`, `When(then=None)`, `Lag` without a default — is part
of the shape: `NULL` in the SQL text, no value.

A filter on an `Exists(...)` condition, and a filter whose value is an expression — `F("other")`,
`F("other") + 1`, `OuterReference("id")` in a correlated `Exists(...)`/`Subquery(...)` — keeps its entry:
the expression's own literals are bound per query. So does a bare `.annotate(x=Value(...))`, an
`.annotate(x=Subquery(...))`, and an ordering, grouping or `DISTINCT ON` of an annotation that isn't
selected (`.annotate(dist=L2Distance("embedding", vector)).order_by("dist")[:10].values_list("id")`): the
annotation's expression rendered there again has its values bound too.

An `Exists(...)` over a `.values()` query, a slice, an `OFFSET` or a queryset with its own
`.with_cte(...)` keeps its entry: the query built into the condition is part of the key, its values — its
CTEs' and its `OFFSET` included — bound with the enclosing query's. `OuterReference(...)` across a
relation (`OuterReference("author__name")`) keeps its entry too — the enclosing query's JOIN of the
relation records its default scope like any other — and so does `OuterReference(...)` naming an
annotation of the enclosing query, which resolves the annotation again with its values bound where the
reference stands. A many-to-many relation compared with the outer row (`~Q(tags=OuterReference("id"))`,
`tags__not=OuterReference("pk")` of a relation to a composite key) keeps its entry as well.

`with_recursive()` keeps its entry: the rows the walk starts from are a subquery bound per query,
`max_depth` is a value, and each JOIN of the walk records its default scope.

A `.values()`/`.values_list()` query filtered on a window function, wrapped as a derived table, keeps its
entry.

A `FilteredRelation(...)`, and a `select_related(Select(relation, extra_condition=...))` whose relation
is crossed elsewhere as well (`.only()`/`.defer()` naming its fields, a filter, `.order_by()`,
`.values()`/`.values_list()`, `group_by()`, an annotation), keep their entry: the condition is folded
into every JOIN of the relation — one condition folded into several JOINs (the filter's, the ordering's,
a many-to-many relation's link and its row) binds its values into each, and one folded into no JOIN (a
`count()` of a relation it doesn't cross) binds them nowhere. The condition's values are the query's own,
so the same holds for a query built into another one — a subquery, `Exists(...)`, a CTE body, a set
operation's branch — even one joining a `FilteredRelation(...)` under the name the enclosing query uses.

The vector distances, full-text search and PostgreSQL's expressions keep their entry like the built-in
ones: `L2Distance`/`CosineDistance`/`InnerProduct` (the query vector bound, converted the way the dialect
stores the compared `VectorField`), `SearchVector`, `SearchQuery` (a `Lexeme` query included),
`SearchRank` and `SearchHeadline` (the search text bound, on every dialect), `Distance`/`DWithin`
(the point and the radius bound) and the trigram functions.

`count()` and `exists()` past a keyset boundary (`.after_cursor()`/`.before_cursor()`), `exists()` past
an `OFFSET`, and a filter on a set operation's rows (`pk__in=a.union(b)`) keep their entry: the boundary
values, the `OFFSET` and the union's own values are bound per query.

`stream()` runs on the entry's SQL text the same way as awaiting the query.

`update()` and `delete()` keep their whole statement the same way, whatever picks the rows they write —
filters across relations, a filter on an aggregate annotation
(`pk IN (SELECT ... GROUP BY ... HAVING ...)`), `.after_cursor()`: the key holds the shape of what picks
the rows and adds each assigned field with the type of its value, or the structure of an assigned
expression (`F("price") + 1` and `F("price") * 1` are two shapes).

A `hare.sql` term built by hand (`Field("score") * ValueWrapper(factor)`) keeps its entry wherever it
stands — annotated, as a function's argument, assigned by `update()`: its SQL text with every value a
parameter is part of the key, and the values it binds are bound per query.

`Model.objects.raw(sql, parameters)` keeps a statement of its own for each SQL text, parameter types and
connection: the text is rendered once with the connection's placeholders, and a later call binds its
`parameters` into it. Which result columns are fields of the model and which become attributes of each
instance is kept for each list of column names. A parameter the text holds as a literal (`"*"`) builds
the statement each time.

A filter across a relation keeps its entry whatever its lookup — `author__name="x"`,
`author__name__in=[...]`, `author__name__startswith="a"`, `author__books__rating__range=(1, 2)`, a
negated one (`.exclude(author__name="x")`) — and its value is converted for the related model.

A filter on an annotation — `.annotate(n=Count("books")).filter(n__gte=2)`,
`.annotate(doubled=F("price") * 2).filter(doubled__gt=10)`,
`.annotate(has_x=Exists(...)).filter(has_x=True)` — keeps its entry: the filter's value and the
annotation's own values are bound per query.

## <a id="plan-descriptions"></a>How a query describes its plan

Every part of a query a plan can be kept for — a filter (`Q`), an expression (`F`, `Value`, arithmetic, a
function or aggregate, `Case`/`When`, `Window` and its window function, `Exists`, `RawSQL`), a query
built into another one (a subquery, a CTE body, a set operation's branch) — describes itself
(`hare.query.plans.description.Plannable.get_plan_description()`). The description (`PlanDescription`)
has two halves: the **structure** — everything that changes the SQL text the part builds, which becomes
part of the plan key — and the **values** the part binds as parameters. A part describes its own parts
through their descriptions, so the key of a whole query is the structure of its description.

Descriptions aren't written by hand. An expression class declares how each of its attributes meets the
plan (`plan_parts`), and a query class how each of its settings meets the key (`plan_slots`: its model,
connection and visibility, its filters, ordering, slice, CTEs, ...); the description of each class is
generated from its declaration once, when the class is made. A pytest run with `--verify-plans` checks
that every attribute of an expression it describes is declared, besides comparing each query run on a
plan with the query built in full.

Each value comes from somewhere — the attribute of an object holding it: a filter's value from its
condition's key, a literal from its expression's attribute, the slice from its query. The first query of
a key records, while it is built, every place its SQL text binds a value together with where the value
comes from, and its plan binds a later query's value into each of them — however many times the build
resolved it: a condition read by a filter, an ordering and `.values()` across one relation, a nested
queryset built into the query twice. An object the build makes again for each copy of a query (a copy of
a query built into another one, the default scope's condition, a filter call kept unbuilt) stands for the
object it was made from, and a condition the build derives from a value (a filter across a relation, a
key compared column by column) keeps that value's origin.

The key is the full structure, compared by equality — not a hash of it — so two queries share a plan only
when everything that shapes their SQL text is equal. A part that keeps no plan describes itself as
`None`, and so does every query holding it: such a query is built in full each time.

A class of a part neither describing itself nor declaring `plannable = False` is rejected when it is
defined — see [Writing a custom
expression](../extending/custom-functions-and-expressions.md#writing-a-custom-expression).

## <a id="values"></a>What's a value (never part of the key, free to vary)

Every filter's literal value (its Python *type* is part of the key, though — an `int` and a
`float`/`Decimal` for the same filter build different SQL, so they never share an entry), the
`LIMIT`/`OFFSET` values, an `__in`/`__not_in` list's own contents (whether it holds a `None` is
structural — a `None` ORs in an `IS NULL` test, and an empty list is a constant condition; a list shorter
than the dialect binds as one parameter — 20 values on PostgreSQL and SQLite — is keyed by the number of
its values other than `None`, a longer one by nothing more: one entry serves every length), a
fully-bounded `__range`'s two bounds, `.after_cursor()`'s boundary values, annotation literals (`Value()`
nested inside an expression, `Case`/`When` literal `then=`/`default=`, `RawSQL` parameters,
`Coalesce`/`Concat`/`NTile`/`Lag`/`Lead` default arguments, `Aggregate(_filter=Q(...))`'s own filter
values), `select_related(Select(relation, extra_condition=Q(...)))`'s and `FilteredRelation(...)`'s condition
values, `with_recursive()`'s `max_depth`, the values of a `hare.sql` term built by hand, `.raw()`
parameters, an `aggregate()` metric's own values, the values an `update()` assigns (converted the way a
write converts them — a `DecimalField` value is rounded to its decimal places) and the moment it sets
`auto_now` fields to.

## <a id="uncached-shapes"></a>What still falls back to the uncached path

A handful of shapes are deliberately excluded — either because caching them would risk a silently wrong
result, or because no rebinding hook exists yet. Each of these still works correctly, just without the
fast path:

| Shape | Why it's excluded |
|---|---|
| `Lateral(...)` and `JsonTable(...)` | The subquery or table function joined under the name has no description — its values sit in the JOIN. |
| `.sample(...)` | The percent and the seed are written into `FROM`, not bound. |
| A dialect's QuerySet method called with a list or dict argument | The argument can't be part of a key. |
| A `register_lookup()`-registered lookup whose operator **transforms** the value | Only safe when the criterion embeds the encoder's own output directly — see below. |
| A `hare.sql` term built by hand that renders on its own dialect only, or binds a value no `ValueWrapper` holds | Its description is its SQL text rendered by the neutral dialect, with every value a `ValueWrapper` binds. |

None of these need any code change on your side — each one just runs without the cache.

A JOIN to a model with a default scope — `Meta.tenant_field`, `Meta.soft_delete_field` or a custom
`Manager(scope=...)` provider on the related model or a many-to-many `through=` model — takes the fast
path: the plan records the scope's values apart from the query's own, and a query running on it binds the
scope of its own context (the active tenant, what a custom manager filters by now). A scope whose filter
has another shape than when the plan was recorded — another number of tenants under `Tenancy.scope(...)`,
a custom manager filtering differently — builds the query in full.

## <a id="scope-and-lifetime"></a>Scope and lifetime

Plans are bounded per model. A cache hit refreshes LRU order; a full bucket evicts
its least recently used entry. The environment variable
`HARE_STATEMENT_PLAN_CACHE_MAX_SIZE_PER_MODEL` selects the configured plan limit.
A model's lifetime storage is attached to its metadata. The cache registry tracks
buckets weakly, so a cached plan referring back to its model does not keep that
model alive after its application context is released.

`Cache` and `ModelCache` use the same `CacheBucket` implementation:

- `cache.get_model_bucket(model)` explicitly selects model storage. SQL buckets
  are invalidated when the model's query builder changes.
- `cache.get_bucket(holder)` selects storage attached to another class or object.
- `cache.new_shared_bucket()` creates storage retained by its callers.
- `cache.get_value(holder)` and `set_value(holder, value)` store one derived value.
- The mapping interface on `Cache` addresses global entries. Keys are opaque:
  putting a model inside a tuple does not select that model's lifetime.
- `ModelCache` provides a model-to-value mapping backed by these same buckets.

Every bucket enforces its size bound, including writes through `update()` and
`setdefault()`. Clearing preserves its identity, so retained references see the
invalidation. Generation belongs to the bucket; callers do not compare generations
or maintain parallel metadata dictionaries. Holder buckets use `max_size`, or
the default holder bound when no size is supplied.

Individual bucket reads, writes and invalidations are synchronized across threads, including
LRU maintenance. Concurrent callers share the same bucket for a holder. A computation between
reading and writing the cache can still run more than once; cache access does not serialize
query execution or make a sequence of mapping operations atomic.

`holds_sql=False` marks data independent of a model's SQL builder.
`depends_on_other_models=True` clears all entries when any model changes.
`dropped_with=source` clears dependent storage when that cache is invalidated.
`Caches.forget_model_caches()` and `forget_all_caches()` invalidate registered
storage; consumers need no separate clearing lists. `len(cache)` counts global
mapping entries, and `cache.entry_count` also counts live holder entries.

## <a id="correctness-notes"></a>Correctness notes

This cache stores real query structure keyed on filter *keys*, not values — so its own design principle
is that reusing the wrong criterion structure, or a stale/shared value, for a different call would be a
silent data-correctness bug, not just a missed optimization. Two rules follow from that:

- **A `register_lookup()` operator must not transform its value.** A custom lookup that embeds
  `field.to_db_value(value, model)` (or `value_encoder(value)`) directly into the criterion is safe to
  cache — a hit binds a fresh value through the same path. A lookup whose operator computes something
  *else* from the value (e.g. reversing a string before comparing) can't be — a shape hit would silently
  keep applying the *first* call's transformed value. hare checks this by identity before trusting a
  shape's values can be bound, so an unsafe custom lookup is automatically excluded rather than silently
  miscached. Binding never rebuilds a criterion to recover its values.
  A prepared slot records its SQL positions and rendering constraints; the conversion receives the
  current input. Conversion errors propagate directly rather than requesting another query build.
- **Anything built into the query's structure, rather than stored as a value a hit binds, must be part of
  the shape key.** The clearest example: `field__year`/`__month`/etc. build the active timezone's name
  into their criterion (`EXTRACT(... AT TIME ZONE $1)`) — it reaches the database as a bind parameter,
  but a cache hit doesn't bind it anew — so the timezone name is part of the shape key, and switching
  timezones at runtime (`Timezone.override()`, another `init()`) can't silently keep extracting in a
  stale cached zone. Without `use_timezone` a datetime expression converts from the machine's own zone
  instead, and that zone's name is part of the key the same way.

## <a id="performance"></a>Performance

There's no single official "N% faster" benchmark for this cache as a whole — its rationale is structural:
skip re-walking and re-resolving the entire filter/annotation/join tree, rebuilding `hare.sql` term
objects and rendering SQL on every repeated-shape call, binding only the values. The cost this
specifically targets is real — e.g. eagerly building one bind-value wrapper per element of a large list
was measured at roughly 97% of that list's own construction cost, which is why a plan binds an entire
list-valued argument as one parameter rather than rebuilding it element by element — but that number
describes one internal design choice, not an end-to-end speedup claim for the cache as a whole.
