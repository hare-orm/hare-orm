# QuerySet methods

Every method of a `QuerySet` — what it returns, what it runs and what it raises — then the parts that
need more than a table row: `NULL` ordering, `Prefetch`, streaming, recursive queries, sampling,
returning the written rows, inserting from a query and merging.

## <a id="method-reference"></a>Method reference

Legend: **chain** — returns a new `QuerySet` (lazy, nothing runs yet); **await** — returns a
specialized awaitable object you must `await`; **terminal** — the `QuerySet`/`QuerySetSingle` itself
becomes awaitable.

A queryset not bound with `using()` chooses its connection each time it runs, so the
same queryset object can be awaited again after a transaction ends, inside a different one, or from
another task — each run uses the connection (or open transaction) current at that moment.

| Method | Type | What it does |
|---|---|---|
| <code>filter(&#42;args: Q &#124; Exists, &#42;&#42;kwargs)</code> | chain | Clones the queryset and ANDs in the given conditions. Supports `related__field=...`, positional `Q` objects and `Exists(...)` conditions. Like Django, a sliced queryset (`.limit()`/`.offset()`/`qs[a:b]`) can't be filtered: `filter()`/`exclude()`/`get(**kwargs)` on it raise `QueryError("Cannot filter a query once a slice has been taken.")` — filter first, then slice. A `.values()`/`.values_list()` query (or set operation) used as a filter value (`id__in=...`) must select one column — selecting more raises `QueryError("Cannot use multi-field values as a filter value...")` instead of a database error. |
| <code>exclude(&#42;args: Q &#124; Exists, &#42;&#42;kwargs)</code> | chain | Same, but drops the rows matching **all** of the call's conditions together, like Django: `exclude(a=1, b=2)` is `NOT (a = 1 AND b = 2)` — the same as `exclude(Q(a=1, b=2))`. Across a multi-valued relation hare differs from Django: the conditions of one `exclude()` call (or of one `~Q(...)`) refer to the **same** related row, the way they do in `filter()` — `exclude(books__name="a", books__rating=5)` drops an author only when one book matches both, and keeps an author with one book named "a" and another rated 5. In Django the conditions of one `exclude()` don't necessarily refer to the same item, so it drops that author too; to get that in hare, use separate calls — `.exclude(books__name="a").exclude(books__rating=5)` drops the authors with any book matching either condition. Separate `.exclude()` calls each drop their own rows. An `exclude()` across a to-many relation is a `NOT EXISTS` subquery, so it takes no `JOIN` of the query: a later `.filter(books__...)` still builds the `JOIN` that `values("books__...")`, `order_by("books__...")` and `Count("books")` read. An empty `Q()` is a no-op here and everywhere else — `~Q()` and `exclude(Q())` match every row. See [NULL semantics](filters.md#null-semantics) below for how a NULL column/value is handled. |
| <code>annotate(&#42;&#42;kwargs: Expression &#124; Term)</code> | chain | Adds computed fields/aggregates to the `SELECT`; registers lookup suffixes for the annotation, so you can later `.filter()`/`.exclude()` on it (landing in `HAVING` whenever the condition contains an aggregate — also an expression mixing one with a column, such as `Count("books") + F("id")`, or a field compared with an aggregate annotation, `budget__lt=F("total")` — and in `WHERE` otherwise). A name that is a field of the model (or `pk`) raises `FieldError`, as does such a name for an expression passed to `alias()`/`values()`/`values_list()` — every other reference to it would read the annotation instead of the field. A value that isn't an expression (e.g. a bare field name string — use `F("field")`) raises `TypeError`, in `alias()` too. Like Django, an `.alias()` nothing reads — no filter, ordering, grouping, other annotation or `values()` field — is left out of the query, so its to-many `JOIN` doesn't repeat rows. |
| `alias(**kwargs)` | chain | Like `annotate()` — usable in `.filter()`/`.order_by()` — but the expression isn't added to the `SELECT` unless `.values()`/`.values_list()` asks for it by name. `FieldError` for a name that is a field of the model or `pk`. |
| <code>order&#95;by(&#42;orderings: str &#124; Ordering)</code> | chain | `'-field'` for `DESC`; a path into a JSON field or annotation (`'-data__score'`) orders by the JSON value, in `jsonb` order on every backend; a [date part](values.md#date-part-paths) (`'-created__month'`) orders by that part; `F('field').asc()`/`.desc()` with `nulls_first=True`/`nulls_last=True` to fix where `NULL`s sort — see [NULL ordering](#null-ordering). Supports `related__field`; a forward FK/O2O name itself (`'tournament'`), also at the end of a related path (`'event__tournament'`), orders by its key column(s) (`tournament_id`, `event__tournament_id`), in `Meta.ordering` too; a to-many or reverse one-to-one relation name (`'events'`, `'-tags'`) orders by the related primary key (returning a row per related row), and a trailing `pk` (`'author__pk'`, `'tags__pk'`) by the primary key field(s), like Django — see [relations and `pk` in field paths](values.md#relation-field-paths). Can't follow `.after_cursor()`. Like Django, a sliced queryset (`qs[a:b]`, `.limit()`, `.offset()`, an index) can't be reordered — `QueryError("Cannot reorder a query once a slice has been taken.")`; order first, then slice (`order_by("name").limit(2)`). The same holds for a `union()` and a `.values()`/`.values_list()` set operation. `order_by()` with no arguments removes every ordering, `Meta.ordering` included. `'?'` orders the rows at random — by `Random()`, a new number for every row, alone or beside other orderings (`order_by('category', '?')`); the database reads every row to sort them, so on a big table pick rows another way. |
| `orderings -> tuple[tuple[str, Order], ...]` | property | The ordering `order_by()` gave the queryset, one `(path, Order)` per column the query orders by: `pk` stands for every primary key field, a forward relation for its key column(s) (`"tournament"` → `"tournament_id"`), `F("score").desc(nulls_last=True)` for `("score", Order.DESC_NULLS_LAST)`. Empty without `order_by()` — the query then orders by `Meta.ordering`, which is never included (read `Model._meta.ordering`) — and after `order_by()` with no arguments, which leaves the query unordered. `before_cursor()` leaves it as given; a clone keeps it, the next `order_by()` replaces it. `Order` is in `hare.sql`. |
| `after_cursor(*values)` | chain | Keyset/cursor pagination instead of `OFFSET` — requires `.order_by()` first; supports `related__field` in the ordering (joined in the same way as `order_by`), but not an annotation. On a `.distinct(<fields>)` queryset the boundary applies to the rows `DISTINCT ON` picks (through `pk IN (SELECT ...)`), as does `before_cursor()`'s. |
| `before_cursor(*values)` | chain | The previous page: rows strictly **before** the values. Runs in the exactly reversed ordering (so `.limit(n)` takes the `n` rows closest to the cursor) and returns rows in the original `.order_by()` order — for instances, `values()` and `values_list()` alike; `first()`/`get()` pick the row right before the cursor. Same errors as `after_cursor()`. Combined with `after_cursor()` (either order) it bounds a window; calling either again replaces its own boundary. Not supported by `iterator()`/`stream()`. |
| `cursor_values(instance) -> tuple` | sync | The cursor values of `instance` for the current `.order_by()` — pass them to `after_cursor()`/`before_cursor()`. A `related__field` ordering needs the relation loaded (`select_related()`), `QueryError` otherwise; a NULL relation gives `None`; an FK/O2O ordering (`'city__country'`) gives its key, not the related object; a to-many relation or an annotation raises `FieldError`. |
| `none()` | chain | An intentionally empty queryset — short-circuits before touching the DB (`count`/`exists`/`values`/`delete`/`update`/... all resolve without a query). |
| `limit(limit)` / `offset(offset)` | chain | `QueryError` if negative. `qs[start:stop]` is sugar for `.offset(start).limit(stop-start)` (step must be `1`/`None`, `start`/`stop` non-negative); a `stop` not past `start` (`qs[3:3]`, `qs[4:2]`) selects no rows, like a list slice. An offset without a limit renders `LIMIT -1 OFFSET n` on SQLite (which rejects a bare `OFFSET`), for querysets and unions alike. `qs[i]` is the one object at index `i` — `IndexError` when awaited if there is none; a negative index raises `QueryError`. A union takes slices and an index the same way. |
| `distinct(*fields)` | chain | No args: plain `DISTINCT`. With args: one row per combination of the fields — a field, a relation, an annotation or a [date part](values.md#date-part-paths) (`"created__date"`), the first in the ordering — `order_by` must start with the same fields in the same order; `count()`/`exists()` (also sliced) count the rows it picks; `aggregate()` raises `QueryError`. PostgreSQL runs `DISTINCT ON (fields)`; a database without it (SQLite) picks the same rows by `ROW_NUMBER() OVER (PARTITION BY <fields> ORDER BY <ordering>) = 1` in a `pk IN (SELECT ...)` subquery — the model needs a primary key there, with no ordering the row with the smallest primary key is kept, and an `OuterReference(...)` in its filters still reads the query the queryset is nested in. A `distinct(*fields)` branch of `union()` keeps its ordering. Like Django, `distinct()` (with or without fields) on a sliced queryset raises `QueryError("Cannot create distinct fields once a slice has been taken.")` — call it before slicing. |
| `union(*other_querysets, all=False)` | chain | Returns a queryset of the combined rows — the same `QuerySet` class, so everything below is ordinary queryset API. Chained `union()`/`union(all=True)`/`intersection()`/`difference()` calls combine each new branch with the whole result so far, in call order: `a.union(b).union(c, all=True)` is `(a UNION b) UNION ALL c` (the duplicates `c` brings stay), `a.union(b, all=True).union(c)` removes every duplicate, and `a.union(b).intersection(c)` is `(a UNION b) INTERSECT c` on every backend (a derived table keeps PostgreSQL' tighter `INTERSECT` precedence out of it). Each branch can carry its own `.annotate()`, as long as the annotation name doesn't collide with a real model field. An annotation is decoded through the field of the first branch that has one, like Django — a `Value(None)` branch has none and doesn't change it (a `date` stays a `date` on SQLite), and it stays undecoded only when another branch's value has no field at all (e.g. a `Value("text")`). `.order_by()` on the union refers to the selected columns by position, so it works with branches joining a relation or grouping by an aggregate on every backend. `select_related()`/`prefetch_related()` on an individual branch (before `.union()`/`.intersection()`/`.difference()`) still raises `QueryError` — the loaded relation can't survive being merged into the combined result. Call `.prefetch_related()` on the combined queryset itself instead: it batches the prefetch over the union's own materialized result list via `prefetch_related_objects()`, after execution — same accepted forms (`str`/`Prefetch(...)`) as `QuerySet.prefetch_related()`. A branch can itself be a set operation — `a.union(b.intersection(c))` combines `b INTERSECT c` as a whole. A branch — a nested set operation too — can be ordered and sliced on its own (`a.order_by("-rank")[:3].union(b)`): it is combined as a derived table of its own rows. Every branch runs on one connection — a branch whose model or router picks another connection than the first branch's raises `QueryError`. A union works as an `__in` value, like a queryset: `filter(pk__in=a.union(b))`, `filter(author__in=a.union(b))` compare with the primary key of its rows. Retrieval works as on a queryset: `count()`, `exists()` (the slice included), `first()`/`last()` (unordered: by the primary key; `last()` of a sliced union raises `QueryError`), `get(*args, does_not_exist_exception=..., multiple_objects_returned_exception=..., **kwargs)` (as on a queryset), `iterator(chunk_size=1000)` (pages by `OFFSET`, ordered by the `order_by()` and then every selected column — and, for a union of several models, the model — as a tie-breaker; the slice bounds the iteration; `prefetch_related()` runs per page) and `stream(chunk_size=1000)` (a cursor like [`QuerySet.stream()`](#stream) — inside a transaction, without `prefetch_related()`). Unlike Django, which rejects conditions there, `get()` accepts them: they filter every branch before combining — a row condition keeps the same rows before or after `UNION`/`INTERSECT`/`EXCEPT` — and raise `QueryError` on a sliced union, whose rows depend on the rows a condition drops. `aggregate()` runs over the combined rows. Methods changing which rows a queryset matches — `filter()`/`exclude()`/`annotate()`/`alias()`/`distinct()`/`group_by()`/`only()`/`defer()`/`select_related()`/`select_for_update()`/`with_cte()`/the cursors/`all_tenants()`/`include_deleted()`/`only_deleted()` — and the writes (`update()`/`delete()`/`hard_delete()`/`create()`/...) raise `QueryError` on the union itself, like Django — apply them to the branches. Model querysets combine with model querysets and `.values()`/`.values_list()` querysets with each other (`QueryError` for a mix — see [`values()` / `values_list()` queries](values.md)); `.values()`/`.values_list()` on a union of model querysets turn it into a union of values. |
| `with_cte(name, query)` | chain | Adds a named `WITH` (CTE) without changing the queryset's own `FROM`; `filter(id__in=CteRows(name, "id"))` reads the queryset's rows from it — see [Recursive queries and CTEs](#recursive). |
| `with_recursive(relation, *, max_depth=None)` | chain | The rows reachable from the queryset's rows through `relation` — a forward, reverse or many-to-many relation of the model to itself — step after step, these rows included, in one `WITH RECURSIVE` query: descendants, ancestors, a graph's connected rows. See [Recursive queries and CTEs](#recursive). |
| `sample(percent, *, method=TableSampleMethod.BERNOULLI, seed=None)` | chain | Reads a random sample of the model's table instead of all of it — `TABLESAMPLE`. See [A sample of the table](#sample). |
| `select_for_update(*, nowait=False, skip_locked=False, of=(), no_key=False, share=False, key_share=False)` | chain | `SELECT ... FOR UPDATE`. The lock strength is one of: `FOR UPDATE` by default, `no_key=True` — `FOR NO KEY UPDATE` (rows referencing the locked ones may be written; a plain `FOR UPDATE` where the database lacks it), `share=True` — `FOR SHARE` (other transactions may share the lock, none may update or delete the rows), `key_share=True` — `FOR KEY SHARE` (only deletes and key changes wait); more than one, or a flag that isn't a bool, raises `QueryError`; `FOR SHARE`/`FOR KEY SHARE` on a database without them (`features.supports_select_for_share`/`supports_select_for_key_share`) raise `UnSupportedError` when the query runs. The rows a `prefetch_related()` of the query reads are locked the same way. When the query runs on a database without `FOR UPDATE` (`features.supports_select_for_update` is false, e.g. SQLite), raises `UnSupportedError` — silently ignoring the call would let a caller believe rows are locked when they aren't. On a backend that does support it, also raises `QueryError` if actually executed outside a `Transactions.atomic()` block — the lock would otherwise be acquired and immediately released (autocommit), giving no real protection while looking like it does. `of` takes `"self"` (or the model's own table name) and forward relation paths as passed to `select_related()` (`"author"`, `"author__country"`), rendered as the aliases the query actually joins. A relation named in `of` is joined with `INNER JOIN` instead of `LEFT OUTER JOIN` (PostgreSQL can't lock the nullable side of an outer join), so every hop of its path must be a NOT NULL FK with `db_constraint=True` and no soft-delete/tenant/`Select(extra_condition=...)` condition — otherwise, or for a name that isn't such a path or isn't joined by the query, `QueryError`. Without `of`, a query with any JOIN locks only the model's own rows. A query with an aggregate annotation or a `GROUP BY` raises `QueryError` — a grouped row stands for several table rows, which SQL can't lock; lock the rows in a separate query. So do a `.distinct()`/`.distinct(<fields>)` query, one reading a window function (`Window(...)`) and a branch of `union()`/`intersection()`/`difference()` — a `.values()`/`.values_list()` one too — which PostgreSQL can't lock either. |
| `group_by(*fields)` | chain | Before or after `.values()`/`.values_list()`, which then return one row per group. Model instances are always grouped by their primary key, so on an instance queryset it doesn't change the rows — `await qs`, `count()`, `exists()`, `contains()`, `update()` and `delete()` all see one row per instance. `aggregate()` runs over the grouped rows (the rows `.values()` would return) and can read only the group-by fields and the annotations (`QueryError` otherwise): `.annotate(n=Count("id")).group_by("author_id").aggregate(most=Max("n"))` is the largest per-author count, like Django's `values().annotate().aggregate()`. A grouped `.values()`/`.values_list()` query ignores `Meta.ordering` (its columns aren't grouped) — unordered, it is ordered by the group key. |
| `values_list(*fields, flat=False, named=False, **expressions)` | chain | A queryset of tuples (flat values with `flat=True`, namedtuples with `named=True`) — see [`values()` / `values_list()` queries](values.md). Incompatible with `.only()`/`.defer()`. No args → every field + annotation. A name can be a path into a JSON field or annotation (`"data__owner__name"`), read as `F()` reads it, or a [date part](values.md#date-part-paths) (`"created__year"`). `named=True` returns each row as a namedtuple with one attribute per selected name (a name that isn't a valid identifier becomes `_<index>`); `flat` and `named` together, a non-string positional argument or a keyword argument that isn't an expression raise `QueryError`. A full queryset of tuples — see [`values()` / `values_list()` queries](values.md). |
| `values(*args, **kwargs)` | chain | A queryset of dicts — see [`values()` / `values_list()` queries](values.md). `kwargs` are `alias="field_name"` or `alias=<expression>` pairs; any other value raises `QueryError`. A name can be a path into a JSON field or annotation (`"data__owner__name"`), read as `F()` reads it, or a [date part](values.md#date-part-paths) (`"created__year"`). Incompatible with `.only()`/`.defer()`. A full queryset of dicts — see [`values()` / `values_list()` queries](values.md). |
| `delete()` | await | `DeleteQuery`, awaits to the row count. Same semantics as `Model.delete()`, applied in bulk: converts to an `UPDATE` instead of a real `DELETE` when `Meta.soft_delete_field` is set (single statement if nothing references this model, otherwise together with the same cascade `Model.delete()` runs); raises `ProtectedError` up front if any `on_delete=PROTECT` relation still references a matched row (checked once against the whole matched set, not per row); runs the cascade in Python first when a relation has `db_constraint=False` (no real DB-level `FOREIGN KEY`, so nothing enforces its `on_delete` for a plain bulk `DELETE`). A cascade run in Python works in batches, all in one transaction: every wave of reached rows costs one query per relation and model (more only past the backend's bind-parameter limit), not one per row, and every row it soft-deletes gets the same `deleted_at`. Only a model that overrides `delete()` still has each matched (or cascaded-to) row deleted through that override, one by one. On a sliced queryset (and with `.distinct(<fields>)`) it deletes exactly the rows `await qs` returns — through `pk IN (SELECT ...)` of the same ordered, sliced, `.distinct()`/grouped `SELECT`. A filter on an aggregate annotation (`HAVING`, also through `.alias()`/`.exclude()`) goes through the same subquery, so only the matching rows are deleted. |
| `hard_delete()` | await | Deletes every matched row for real, even when `Meta.soft_delete_field` is set — an already soft-deleted row too, when the queryset sees it (`Model.objects.only_deleted().hard_delete()` empties the trash). Related rows follow their `on_delete` exactly as `delete()` of a model without soft delete would; on a model without `Meta.soft_delete_field` it is the same as `delete()`. Awaits to the row count. |
| `restore(*, cascade=False)` | await | Reverses the soft delete of the queryset's rows — of those among them that are soft-deleted, whatever the visibility (`Model.objects.filter(...).restore()` reads the deleted rows); awaits to the number of rows restored. One `UPDATE` — the `auto_now` fields and `Meta.optimistic_lock_field` follow as in `update()`. `cascade=True` also restores the rows each one's soft delete removed with it (those still carrying its deletion time, as [`Model.restore(cascade=True)`](../models/model-methods.md)), in one transaction; a model overriding `restore()` gets it called for each row. `QueryError` for a model without `Meta.soft_delete_field`, a `values()` queryset, a `union()` or a `sample()`. Only the active tenant's rows unless `all_tenants()`. |
| `update(**kwargs)` | await | `UpdateQuery`, awaits to the row count. Rejects `Meta.soft_delete_field`/`Meta.optimistic_lock_field` in `kwargs` (`QueryError`) — instead, every matched row's `optimistic_lock_field` is bumped automatically (`SET version = version + 1`) whenever `kwargs` is non-empty, and every `auto_now` field is set to the same "now" regardless of whether it's named in `kwargs`, mirroring `save()`'s own handling of both. Unlike `save()`, there's no staleness check — `.update()` has no previously-fetched instance to compare a version against, so it never raises `StaleObjectError`. On a sliced queryset (and with `.distinct(<fields>)`, or a filter on an aggregate annotation) it updates exactly the rows `await qs` returns, like `delete()`; `bulk_update()` on such a queryset writes only the objects whose rows match it. A value that is an aggregate (`Sum("qty")`) or reads an aggregate annotation (`F("n")` after `annotate(n=Count(...))`) raises `FieldError`, like Django — an `UPDATE` has no groups to aggregate over; a `Subquery()` aggregating the related rows works. |
| `update(**kwargs).returning(*fields)` / `delete().returning(*fields)` / `hard_delete().returning(*fields)` | await | The write, awaiting to the rows it wrote — a dict of `fields` per row, or the model instances when none is named. See [Returning the written rows](#returning). |
| `count()` | await | Awaits to an `int` — the number of rows `await qs` returns, also with aggregate annotations, a `HAVING` filter, `.distinct()` or an ordering across a to-many relation (whose JOIN returns a row once per related row). A `.distinct()` queryset selecting an annotation over a to-many relation (`F("books__name")`) returns a row per distinct value, and so does `count()`. A `.group_by()` doesn't change it — see `group_by()`; with `.distinct(<fields>)` it counts the rows `DISTINCT ON` picks. |
| `aggregate(**kwargs)` | await | One dict keyed by each kwarg's name, over the whole queryset (after `.group_by()`, over the grouped rows — see `group_by()`). Like Django, a sliced queryset (`qs.order_by("-rating")[:10].aggregate(...)`) aggregates the rows of the slice, restricted to them by their primary key; an unordered slice holds the first rows by primary key, the rows `first()`/`last()` read. A `.distinct()` slice may filter over a to-many relation (`filter(tags__in=[1, 2]).distinct().order_by("id")[:2].aggregate(...)`): its rows are reduced to one per primary key before the metrics run, like an unsliced `.distinct()`. Any other slice whose rows can repeat a primary key (a filter, annotation or ordering over a to-many relation) and a model with a composite primary key raise `QueryError` — aggregate a sliced `.values()`/`.values_list()` query instead. |
| `exists()` | await | Awaits to a `bool` — whether `await qs` returns any row, the slice included. |
| `contains(obj)` | await | `ContainsQuery` — is this specific instance (by pk) in the queryset? `QueryError` if `obj` has no pk. |
| `all()` | chain | A no-op clone. |
| `raw(sql, parameters=())` | await | Executes the raw SQL as-is; `parameters` are substituted as real bind parameters at `%s` placeholders (SQL-injection safe — values are never interpolated into the query text). `.sql()` returns the original string unchanged, placeholders unresolved. |
| `first()` / `last()` | terminal | `LIMIT 1`; with no `order_by()` and no `Meta.ordering`, `first()` sorts by pk `ASC` (left unordered on a `.group_by()`/`.distinct(<fields>)` queryset); `last()` inverts the current ordering exactly, direction and explicit `NULL` placement both (or sorts by pk `DESC` if none is set — a model with no pk and no ordering has no defined last row, so it takes any row). On a slice, `first()`/`last()` pick from the slice's rows — an unordered slice holds the first rows by pk for both. A row taken from a slice (`qs[2:5].first()`, `qs[3]`, and `last()`/`latest()`/`earliest()`/`get()` of a slice) can't be filtered, reordered or made distinct (`QueryError`), like the slice itself; `first()` of an unsliced queryset can. On a `.distinct(<fields>)` queryset `last()`/`latest()`/`earliest()`/`get()` pick among the rows `DISTINCT ON` returns for the original ordering. |
| `latest(*orderings)` / `earliest(*orderings)` | terminal | Sort `DESC`/`ASC` by the given fields — the model's `Meta.get_latest_by` when none is given — then `first()`; a `NULL` never wins (`NULLS LAST` on every dialect — see [NULL ordering](#null-ordering)). `FieldError` with neither. |
| `reverse()` | chain | The ordering turned around exactly — direction and explicit `NULL` placement both — the one `order_by()` gave, else `Meta.ordering`; an unordered queryset stays unordered. `QueryError` on a slice or a `union()`. |
| `dates(field_name, trunc_type, order="ASC")` | chain | The distinct values of a `DateField`/`DatetimeField` truncated to `trunc_type` (`year`, `quarter`, `month`, `week` — from Monday — or `day`) as `datetime.date`s, a datetime's in the current zone, `NULL`s left out, in `order` (`ASC`/`DESC`); a `values_list(flat=True)` queryset, filtered and sliced as usual. |
| `datetimes(field_name, trunc_type, order="ASC", tzinfo=None)` | chain | The same for a `DatetimeField`, as aware `datetime`s, `trunc_type` also `hour`, `minute` or `second`, truncated in `tzinfo` (an IANA name or a `ZoneInfo`; the current zone by default). Another field, `trunc_type` or order raises `FieldError`. |
| `get(*args, does_not_exist_exception=..., multiple_objects_returned_exception=..., **kwargs)` | terminal | The one matching object. `get(**kwargs)` is `filter(**kwargs).get()`: on a `.distinct(<fields>)` queryset the conditions apply before `DISTINCT ON` picks a row per group, as `filter()` does; on a sliced queryset only the no-argument form works (it reads the slice's own rows). `does_not_exist_exception` — for no match — and `multiple_objects_returned_exception` — for more than one: left as they are, `DoesNotExist` / `MultipleObjectsReturned` is raised; an exception class (instantiated with no arguments) or an already-built instance is raised instead; `None` turns the check off — no match gives `None` (the result is typed `Model | None`), several matches give one of them, read with `LIMIT 1` without counting the rest (which one is not defined — `first()` takes a defined one). Anything else is a `TypeError`. |
| `create(**kwargs)` | `async def` | Creates a row and returns its object — `Model(**kwargs)` saved on the queryset's connection (`Model.objects.using("replica").create(...)`); the queryset's filters don't take part. `QueryError` for an `F()`/expression value, and per [`Meta.tenant_field`](../soft-delete-versions-tenants/multi-tenancy.md) when the tenant given differs from the active one. |
| `get_or_create(defaults=None, **kwargs)` | `async def` | The object matching `kwargs` among the queryset's rows, or a new one created from `kwargs` and `defaults` — `(object, created)`. The existence check reads on the write connection, so a lagging replica can't cause a duplicate; a row created concurrently between the check and the insert is fetched instead of raising — the insert's unique-constraint conflict tells it apart, so on a database without unique constraints (`Features.supports_unique_constraints`) two concurrent calls can both insert. `QueryError` when a row is created and `defaults` conflicts with `kwargs`. |
| `update_or_create(defaults=None, create_defaults=None, **kwargs)` | `async def` | Updates the object matching `kwargs` with `defaults`, or creates it with `create_defaults` (`defaults` when not given) — `(object, created)`. Runs in a transaction and locks the matched row (`SELECT ... FOR UPDATE`) where the database supports it. `QueryError` when `defaults` sets `Meta.optimistic_lock_field`, `FieldError` for an unknown field. |
| `bulk_create(objects, batch_size=None, *, ignore_conflicts=False, update_fields=None, on_conflict=None, on_conflict_constraint=None, conflict_where=None, returning=None, use_copy=False)` | await | Generated pks, `GeneratedField` columns, and any omitted `db_default` column are **not** populated back onto the objects unless `returning=True` (PostgreSQL only, via `RETURNING` — not supported on SQLite, whose multi-row `RETURNING` order isn't guaranteed to match input order). Left as `None` (the default), falls back to `Meta.returning` (itself `False` by default) — set that on a shared base to opt in without passing `returning=True` on every call; an explicit `True`/`False` here always overrides it, and a value only INHERITED from `Meta.returning` silently yields instead of raising when combined with `use_copy=True` or run on SQLite. `use_copy=True` loads through the PostgreSQL `COPY` protocol instead of a parameterized `INSERT` — faster for large batches, but mutually exclusive with an explicit `returning=True` and every conflict-handling kwarg (no `RETURNING`/`ON CONFLICT` support on `COPY`), and not supported on SQLite. On ClickHouse `bulk_create()` loads its rows in one binary insert with or without `use_copy=True` — see [Bulk loads](../dialects/clickhouse/differences.md#bulk-loads). `COPY` doesn't support an array/range field or a PostgreSQL-extension field type (`CitextField`, `PostGISField`, `TSVectorField`) on the model on either driver — raises `UnSupportedError` naming the offending field, checked uniformly on the Python side before either driver is reached; fall back to `use_copy=False` for a model with one of those. `update_fields`/`on_conflict` take field names: a `source_field` column is resolved, a forward FK/O2O relation name means its key column(s) (`owner` → `owner_id`), an unknown name raises `FieldError`; naming a generated field, `Meta.tenant_field`, `Meta.optimistic_lock_field` or `Meta.soft_delete_field` in `update_fields` raises `QueryError`. A conflicting row also gets its `auto_now` fields bumped (and `Meta.optimistic_lock_field` incremented); a conflicting soft-deleted row is updated but stays deleted. A field in `update_fields` whose value every object leaves to its `db_default` is still updated — to that database default. With `returning=True` an object with an explicitly set pk gets its `GeneratedField`/omitted `db_default` columns back too, an upsert on a model with `Meta.optimistic_lock_field` reads back the row's bumped version, and with `ignore_conflicts=True` several objects sharing one conflict key in the same call leave only the first of them saved (the one whose row was inserted). With `update_fields`, several objects sharing one conflict key in the same call are written in order on every driver, so the last one's values win; with `returning=True` each of them gets the row's pk. Every object must be an instance of the model itself (another model, a sibling sharing an abstract base, or a subclass with a table of its own raises `QueryError`), and one loaded with `.only()`/`.defer()` must have every column the insert writes (`IncompleteInstanceError`). A statement's own extra parameters (an upsert's tenant scope and version bump) are counted against the bind-parameter ceiling when the batch size is picked. A model whose every column comes from its database default is written in one `INSERT ... SELECT FROM generate_series(...)` on PostgreSQL (one `DEFAULT VALUES` statement per object on SQLite). If `bulk_create()` raises, the objects are left as they were before the call (pk, returned columns, saved state), even when earlier batches had already been written and then rolled back. With `use_copy=True` several `COPY` statements (objects with and without an explicit pk load separately, and so do `batch_size` batches) run in one transaction too, on both PostgreSQL drivers; inside `Transactions.atomic()` each `COPY` runs on the transaction's own connection and commits or rolls back with it, a savepoint's rollback included. `on_conflict_constraint` isn't supported on SQLite. `conflict_where` (PostgreSQL only) — a `Q` over the model's fields or `RawSQLTerm` of raw SQL — repeats a partial unique index's own `WHERE` predicate, so `ON CONFLICT (...) WHERE <conflict_where>` can target it; mutually exclusive with `on_conflict_constraint`, requires `on_conflict` to name that index's columns. |
| `insert_from(queryset, *, fields)` | await | Writes the rows a queryset selects into the model's table in one `INSERT ... SELECT`, awaiting to the number of rows written. See [Inserting the rows of a query](#insert-from). |
| `merge(source, *, on)` | await | One `MERGE` matching a list of rows or a queryset to the model's rows and updating, deleting or inserting them branch by branch. See [Merging rows](#merge). |
| `refresh_materialized_view(name, *, concurrently=False)` | await | Fills a materialized view of `Meta.materialized_views` with the rows of its query again, on the connection the model writes to. See [Materialized views](../models/schema-objects.md#materialized-views). |
| `get_next_sequence_value(name)` | await | The next number of a sequence of `Meta.sequences`. See [Sequences](../models/schema-objects.md#sequences). |
| `bulk_update(objects, fields, batch_size=None, *, returning=None)` | await | Doesn't support M2M/backward-relation fields or `F()`/expression values (`QueryError` — use `.update()` or `save()`); requires every object to have a pk. `returning=True` adds a `RETURNING` clause bringing back every `GeneratedField` column's fresh, DB-recomputed value onto each successfully-updated object — matched by primary key, not row position, so (unlike `bulk_create()`) this works on every dialect, not just PostgreSQL. No-op on a model with no `GeneratedField`. Same `Meta.returning` fallback as `bulk_create()` when left `None`. On a model with `Meta.optimistic_lock_field`, each object's in-memory version is bumped (and, with `track_dirty_fields`, its dirty snapshot reset) for every row that actually wrote, before raising `StaleObjectError` for any row in that batch that didn't — an object whose row the queryset's own `.filter(...)` excludes is simply not updated, not reported as stale — unlike plain `.update()`, this one does check staleness, since it already has each object's previously-loaded version to compare against. `fields` must be a non-empty list of field names (a bare string or an empty list raises `QueryError`); a repeated name is written once, an unknown one raises `FieldError`, and the primary key (`"pk"` or its field) raises `QueryError` — rows are matched by it. Every object must be an instance of the model itself (`QueryError` otherwise), and one loaded with `.only()`/`.defer()` must have its pk and every updated field (`IncompleteInstanceError`). The batch size leaves room for the queryset's own filter (a tenant scope included) and CTE parameters. When the objects are split into several statements only to stay under the bind-parameter ceiling (no `batch_size` given), they all run in one transaction — an error in any of them (e.g. `IntegrityError`) leaves no row updated; a stale version is not such an error and never rolls back the other rows, it's raised once every statement has run. With an explicit `batch_size`, each batch is its own statement, not wrapped in a transaction: an error in a later batch leaves the earlier batches committed, and a stale object in one batch doesn't stop the others from being written. |
| `only(*fields)` | chain | Whitelists fields → a partial model. `QueryError` if empty, or combined with `.defer()`. A `relation__field` entry also loads the primary key of every related model on the way, so a `NULL` foreign key yields `None` and an existing related row always yields an instance. |
| `defer(*fields)` | chain | Blacklists direct fields; repeated calls add up. No `related__field` support. Mutually exclusive with `.only()`. |
| <code>select&#95;related(&#42;args: str &#124; Select)</code> | chain | JOINs related objects; accepts a string or `Select(relation, extra_condition=Q(...))`. Every hop of a path must be a single-valued relation — a `ForeignKeyField`, `OneToOneField` or reverse `OneToOneField`; a reverse `ForeignKeyField` or a `ManyToManyField` (use `prefetch_related()`), a plain field or an unknown name raises `FieldError` right away. A `Select`'s condition is part of the relation's JOIN wherever the query crosses the relation: a filter, ordering or `.values()` across it reads only the related rows the condition matches — and so do `count()`, `exists()`, `aggregate()`, `update()` and `delete()` of the queryset, which match the same rows as the queryset itself. |
| <code>prefetch&#95;related(&#42;args: str &#124; Prefetch)</code> | chain | Separate queries (not a JOIN of the queried rows); accepts a string (including `"a__b"`) or `Prefetch(...)`. `FieldError` for an unknown/non-relation field. A many-to-many relation's rows come in one query, joined to its through table, when the through table and the related rows are on one connection, the relation has no `through=` model and a `Prefetch` queryset neither filters by that relation nor annotates, `.distinct()`s or prefetches further; otherwise the through table is read in a query of its own first. |
| `defer_related(*fields)` | chain | Turns off the automatic preload for relation fields with `lazy=RelationLoadStrategy.JOINED`/`.SELECT`, for this query only. |
| `include_deleted()` / `only_deleted()` | chain | Soft-deleted rows together with the live ones / the soft-deleted rows alone — instead of the default filter of [`Meta.soft_delete_field`](../soft-delete-versions-tenants/soft-delete.md). The last of the two calls wins; relations reached from the query see deleted rows too. `QueryError` for a model without `Meta.soft_delete_field`. |
| `all_tenants()` | chain | Every tenant's rows, instead of the default filter of [`Meta.tenant_field`](../soft-delete-versions-tenants/multi-tenancy.md#all-tenants) — also for the tenant-scoped models the query reaches through relations. Combines with `include_deleted()`/`only_deleted()` in either order. |
| `explain(output_format=None, **options)` | `async def` | Runs the statement `sql(explain=True)` returns (`EXPLAIN QUERY PLAN` on SQLite) and returns the plan rows. `output_format=`/extra `**options` are PostgreSQL-only — SQLite raises `UnSupportedError` for those, not for `.explain()` itself. |
| `using(using)` | chain | Binds the queryset to a specific connection — a connection alias (`"replica"`) or a client (a transaction's, `Connections.get(connection_alias)`); `None` unbinds it — see [Several databases](../connections/multiple-databases.md#using-db). |
| `get_connection(*, for_write=False)` | sync | The connection the queryset would run on if executed now: the one bound with `using()`, else the router's choice (asked under the tenant the queryset was built for), else the connection of the instance a related manager came from, else the model's default connection; inside an open transaction on it, the transaction's client. Binds nothing — see [`Model.get_connection()`](../models/model-methods.md#classmethods) for the locking pattern. |
| `iterator(chunk_size=1000)` | async generator | Without an `.order_by()`, pages follow `Meta.ordering`, else the primary key (every component of a composite one), like Django; an ordering that can tie (it has no pk, no non-nullable unique field and no non-nullable unique-together set) gets the pk appended as a tie-breaker, so no row is skipped or repeated between pages — rows sharing ordering values come out in pk order. An ordering column that `.only()`/`.defer()` leaves unloaded is still read for the page cursor, and stays unloaded on the yielded objects. Uses keyset/cursor pagination (safe under concurrent deletes) when every `order_by()` field is a plain column on this model; falls back to `LIMIT`/`OFFSET` (NOT safe under concurrent deletes — a deleted already-yielded row shifts later rows, skipping one) when the ordering includes a related-model lookup or an annotation, or an annotation is a window function (a keyset condition would change the rows it is computed over). Honors the queryset's own `.limit()`/`.offset()`/slice and `.after_cursor()`. A queryset that can return one primary key several times — a filter or annotation (`F()`, a function, `Case`, arithmetic) over a to-many relation, or a `.distinct()` one selecting such an annotation — pages with `OFFSET`, ordered by those annotations after the primary key, so no repeated row is dropped. A `.distinct(<fields>)` queryset pages over the rows `DISTINCT ON` picks. A page is fetched with one row more than `chunk_size`, which tells whether another page follows — a result ending on a page boundary needs no empty query after it. |
| `stream(chunk_size=1000)` | async generator | A genuine cursor — a server-side cursor/portal on PostgreSQL, a cursor read in batches on SQLite — inside a transaction. See below. |
| `sql(parameters_inline=False, *, explain=False, output_format=None, **explain_options)` | sync | Returns the SQL the query runs on its connection, with placeholders or with parameters inlined; `explain=True` returns its `EXPLAIN` statement instead. Several statements (a bulk write) are joined with `;`. |

`await qs` returns `list[Model]` (or `Model | None` once `.first()`/`.get()`/... narrowed it to a
single row); `async for x in qs` also works.

## <a id="null-ordering"></a>NULL ordering

SQLite and PostgreSQL disagree on where `NULL` sorts, and a plain ordering string leaves it to the
dialect: SQLite treats `NULL` as the smallest value (first for `ASC`, last for `DESC`), PostgreSQL as
the largest (last for `ASC`, first for `DESC`). With `Player(name, score)` holding `a:1, b:NULL, c:2,
d:3, e:NULL`, `order_by("score", "name")` returns `b, e, a, c, d` on SQLite and `a, c, d, b, e` on
PostgreSQL. That default is unchanged and applies to `order_by("field")`/`order_by("-field")`,
`Meta.ordering` strings, `first()`/`last()` and `Window(order_by=[...])` strings.

To fix the position on every dialect, pass an `Ordering` built from `F(...)` instead of a string:

```python
from hare.query.expressions import F

await Player.objects.all().order_by(F("score").desc(nulls_last=True), "name")   # 3, 2, 1, then the NULLs
await Player.objects.all().order_by(F("score").asc(nulls_first=True), "name")   # the NULLs, then 1, 2, 3
await Employee.objects.all().order_by(F("manager__name").asc(nulls_last=True))  # NULLs from a LEFT JOIN too
```

`F(name).asc()` / `F(name).desc()` take the keyword-only flags `nulls_first` / `nulls_last` (both
`True` raises `QueryError`; neither leaves the dialect default). An `Ordering` and plain strings mix
freely, and it is accepted everywhere an ordering is: `order_by()` (also on a `union()`),
`latest()`/`earliest()`, `Meta.ordering`, `Window(order_by=[...])`, `distinct(...)` + `order_by()`
(PostgreSQL), `values()`/`values_list()`, `stream()`/`iterator()`. Anything that is neither a string nor
an `Ordering` raises `QueryError`. The rendered SQL is `ORDER BY ... NULLS FIRST|LAST` (SQLite 3.30+).

- **`latest()` / `earliest()` are NULL-safe.** A plain field name gets `NULLS LAST` on every dialect,
  so a `NULL` never wins: `latest("score")` is the row with the greatest non-`NULL` value and
  `earliest("score")` the smallest. When every value is `NULL`, a `NULL` row is still
  returned (not `None`). A column that cannot hold `NULL` keeps the plain direction, so index-ordered
  scans still work; a `related__field` ordering and an annotation are treated as nullable. An explicit
  `Ordering` is honored: `earliest()` uses it as given, `latest()` reverses it like `last()` does.
- **`last()` reverses exactly.** The direction and any explicit placement are both inverted
  (`ASC NULLS LAST` becomes `DESC NULLS FIRST`), so `last()` is always the final row of the same
  ordering read forwards. Without an explicit placement the dialect default reverses along with the
  direction.
- **`after_cursor()` / `before_cursor()` / `iterator()`** follow the ordering's own placement: an
  explicit `nulls_first`/`nulls_last` decides which rows lie after (or before) a cursor (including a
  `NULL` boundary value), otherwise the dialect default does, so pages never drop or repeat a row
  either way. `before_cursor()` mirrors the placement when it reverses the ordering
  (`ASC NULLS FIRST` runs as `DESC NULLS LAST`).
- **Statement plans** are keyed on the placement, so two queries differing only in `NULL` position
  never share a plan.

## <a id="prefetch"></a>`Prefetch`

```python
class Prefetch:
    def __init__(self, relation: str, queryset: QuerySet, to_attribute: str | None = None) -> None
```

`queryset` can already have `.filter()`/`.only()`/`.order_by()` applied; it must be a model
`QuerySet` — a `.values()`/`.values_list()` query raises `QueryError`. `to_attribute` can't be the
name of a field, relation, key column (`author_id`) or other attribute of the model the prefetched
relation belongs to — that raises `QueryError` instead of overwriting it on every instance.
Nested prefetching is supported two ways:

```python
# dotted relation string inside Prefetch
await Team.objects.all().prefetch_related(Prefetch("events__reporter", queryset=User.objects.filter(is_active=True)))

# .prefetch_related() called directly on the queryset passed into Prefetch
await Team.objects.all().prefetch_related(
    Prefetch("events", queryset=Event.objects.filter(status="open").prefetch_related("reporter"))
)
```

As in Django, a path through a relation (`"events__reporter"`) loads the relation's own attribute
(`team.events`), even when a `Prefetch` of the same relation with `to_attribute=` is given too — that
one loads only its own attribute. A path goes into the `to_attribute` results when it starts with the
`to_attribute` name; the `Prefetch` giving it may come later in the same call or in an earlier
`prefetch_related()` call:

```python
teams = await Team.objects.all().prefetch_related(
    Prefetch("events", queryset=Event.objects.filter(status="open"), to_attribute="open_events"),
    "open_events__reporter",  # loads reporter on each event in team.open_events
    "events",  # team.events - every event of the team
)
```

A first name that is neither a relation of the model nor such a `to_attribute` raises `FieldError`.

### <a id="sliced-prefetch"></a>A sliced `Prefetch` queryset

A slice of the `Prefetch` queryset (`[:3]`, `[2:5]`, `.limit()`, `.offset()`) is taken of **each
parent's** rows — the three newest comments of every post, not three comments in all:

```python
posts = await Post.objects.prefetch_related(
    Prefetch("comments", queryset=Comment.objects.order_by("-id")[:3], to_attribute="latest_comments")
)
```

The rows are numbered within each parent by `ROW_NUMBER() OVER (PARTITION BY <the parent's key>
ORDER BY <the queryset's ordering>)` and the number is compared with the slice's bounds — one
query for a reverse foreign key; for a many-to-many relation, whose row may belong to several
parents and fall into each one's slice on its own, the numbered (parent, row) pairs are read
first and then the rows. The ordering is the queryset's `order_by()`, else the model's
`Meta.ordering`, else the primary key. Composite keys, `to_attribute` and nested prefetches of the
sliced rows work as without a slice. A forward relation and a reverse one-to-one have one row per
parent at most: a slice starting at it keeps it, one past it leaves `None`.

## <a id="stream"></a>`stream()` — a real server-side cursor

```python
async def stream(self, chunk_size: int = 1000) -> AsyncIterator[TModel]
```

Unlike `iterator()` (which re-runs a fresh `SELECT` per page via `LIMIT`/`OFFSET` or a keyset seek),
`stream()` opens exactly **one** server-side cursor/portal for the whole scan — a single consistent
snapshot for the entire iteration. A row already pulled into the cursor's own pipeline is immune to
a concurrent `DELETE`/`UPDATE` landing partway through, and unfetched rows are read against the
snapshot the cursor took when it opened, not against whatever the table looks like at the moment
each row is actually read.

```python
async with Transactions.atomic():
    async for widget in Widget.objects.filter(category="archived").stream():
        ...
```

Requires an active `Transactions.atomic()` block on both PostgreSQL drivers — enforced
uniformly even though only asyncpg's own `Connection.cursor()` strictly needs it (tied to its
transaction's lifetime by construction); rust_pg's portal has no such requirement on its own, but
`stream()` applies the same rule anyway so the contract doesn't vary by driver — and on SQLite,
where the cursor is read off the transaction's connection: `chunk_size` rows per `fetchmany()` hop to
aiosqlite's worker thread, the whole result never held at once, the cursor closed when the iteration
ends, is left or is cancelled. Raises `QueryError` outside a transaction, and `UnSupportedError` on a
database without `features.supports_streaming` (one without transactions). `chunk_size` is a
per-round-trip prefetch hint honored by asyncpg's `Connection.cursor(prefetch=...)` and SQLite's
batches; rust_pg's portal streams row-by-row off the wire already and ignores it. Other queries on the same transaction may run while the stream is
open — from the loop body, or from sibling tasks (`asyncio.gather()`) sharing the transaction; each
cursor fetch takes its turn on the transaction's connection.

On ClickHouse (`features.streams_without_transaction`) `stream()` runs outside a transaction too: the
server sends the rows as it computes them, in blocks the drivers read one at a time, and leaving the
loop — a `break`, an error, a cancellation — closes the stream and gives the connection back.

When the iteration ends early — an exception, a cancellation, or `aclose()` of the generator (a
plain `break` closes it once the generator is garbage-collected; wrap it in
`contextlib.aclosing()` to close it at the `break`) — the stream stops right away without reading
the remaining rows; on rust_pg its portal is dropped at once, so the transaction's next statement
doesn't wait behind the unread rows. The transaction's own `COMMIT`/`ROLLBACK` also
closes a stream still open on it; reading from it afterwards raises `TransactionManagementError`.
On rust_pg a `ROLLBACK` that gets no answer within 30 s closes the connection instead, which rolls
the transaction back server-side — the connection never goes back to the pool.

## <a id="recursive"></a>Recursive queries and CTEs

### <a id="with_recursive-walking-a-relation-of-a-model-to-itself"></a>`with_recursive()` — walking a relation of a model to itself

```python
def with_recursive(self, relation: str, *, max_depth: int | None = None) -> QuerySet[TModel, TModel]
```

`with_recursive()` takes the queryset's rows as the start and follows `relation` from them again and
again: the rows one step away, then the rows one step from those, and so on. The result is a queryset
of the model's rows reached — the start rows included — built as one `WITH RECURSIVE` query, with no
round trip per level.

```python
class Employee(Model):
    name = fields.CharField(max_length=50)
    manager = fields.ForeignKeyField("models.Employee", related_name="team_members", null=True)
    talks_to = fields.ManyToManyField("models.Employee", related_name="gets_talked_to")

ceo = await Employee.objects.get(name="CEO")
# The CEO and everyone below: the reverse relation, down the tree.
everyone = await Employee.objects.filter(pk=ceo.pk).with_recursive("team_members")
# A row and its managers up to the top: the forward relation, up the tree.
chain = await Employee.objects.filter(name="Ann").with_recursive("manager")
# Two levels down only - direct reports and theirs.
nearby = await Employee.objects.filter(pk=ceo.pk).with_recursive("team_members", max_depth=2)
# Everyone Ann reaches through a graph, many-to-many.
reached = await Employee.objects.filter(name="Ann").with_recursive("talks_to")
```

- **`relation`** — a forward (`ForeignKeyField`, `OneToOneField`), reverse or many-to-many relation
  whose related model is the model itself. Anything else — a plain field, a relation to another
  model, a path with `__` — raises `FieldError` when the query runs.
- **`max_depth`** — how many steps from the start rows: `0` is the start rows alone, `1` adds their
  direct neighbours, `None` (the default) walks until no new row turns up. An `int` from `0` to
  `100_000`; another value (a negative or larger number, `True`, `2.0`, a string) raises
  `QueryError` at the call.
- **Cycles end.** The walk joins its steps with `UNION`, so a row reached a second time — through a
  cycle in the data (`a.manager = b`, `b.manager = a`) or by two paths — adds no new row and the walk
  stops. With `max_depth` a row reached at two depths is kept once.
- **Start rows.** Any queryset: a filter, several rows, a slice, a queryset ordered or annotated (only
  its primary keys are read). An empty start gives an empty result.
- **Row scope.** The walk goes through the rows the model's default scope shows: a soft-deleted row
  (`Meta.soft_delete_field`) or another tenant's (`Meta.tenant_field`) is neither returned nor walked
  through, so the rows below a deleted row aren't reached from above it. `include_deleted()` /
  `all_tenants()` called before `with_recursive()` walk through every row, and the result keeps
  that visibility.
- **Composite primary keys** work as single-column keys — the walk compares every key column.
- **The result** is a plain queryset of the model on the same connection: filter it, order it, slice
  it, `count()` it, read `values()`, prefetch relations, or pass it as an `__in` value
  (`Employee.objects.exclude(pk__in=everyone_below)`). Its default order is the model's own — the
  walk's order isn't kept; the depth isn't a column of the rows.
- A combined queryset (`union()`/`intersection()`/`difference()`) raises `QueryError` — call
  `with_recursive()` on a branch.

The query has the rows' primary keys walked in a CTE and the model's rows read by them:

```sql
SELECT ... FROM "employee" WHERE "id" IN (
  WITH RECURSIVE "hare_recursive_rows"("id") AS (
    SELECT "hare_recursive_start"."id" FROM "employee" "hare_recursive_start"
    WHERE "hare_recursive_start"."id" IN (SELECT "id" FROM "employee" WHERE "id" = $1)
    UNION
    SELECT "hare_recursive_previous__team_members"."id" FROM "hare_recursive_rows"
    JOIN "employee" "hare_recursive_previous" ON "hare_recursive_rows"."id" = "hare_recursive_previous"."id"
    JOIN "employee" "hare_recursive_previous__team_members"
      ON "hare_recursive_previous"."id" = "hare_recursive_previous__team_members"."manager_id"
  )
  SELECT "id" FROM "hare_recursive_rows"
)
```

`with_recursive()` runs on PostgreSQL, SQLite and the columnar database alike. Its query keeps no
[plan](query-plan-cache.md): its steps join the related rows under their scope, values a plan can't
bind.

### <a id="cterows-reading-a-querysets-rows-from-a-cte"></a>`CteRows` — reading a queryset's rows from a CTE

`with_cte(name, query)` attaches a `WITH` to the query without changing its `FROM`. `CteRows(name,
*columns)` is the value an `__in` filter reads the CTE's rows with — `SELECT <columns> FROM <name>`:

```python
from hare.query.expressions import CteRows

# A queryset as the CTE's body.
large = IntFields.objects.filter(intnum__gte=40).values("id")
rows = await IntFields.objects.with_cte("large", large).filter(id__in=CteRows("large", "id"))

# A hand-built recursive CTE: the category and its ancestors.
connection = Category.get_connection()
query_class = connection.query_class
category, ancestors = Table("category"), Table("ancestors")
columns = (category.id, category.parent_id)
base = query_class.from_(category).select(*columns).where(category.id == leaf_id)
step = query_class.from_(category).join(ancestors).on(category.id == ancestors.parent_id).select(*columns)
union_all = base * step
union_all.base_query.wrap_set_operation_queries = False
path = await Category.objects.with_cte("ancestors", union_all).filter(id__in=CteRows("ancestors", "id"))

# A composite primary key: the CTE's columns in the key's order.
picked = Node.objects.filter(name="child").values("a", "b")
rows = await Node.objects.with_cte("picked", picked).filter(pk__in=CteRows("picked", "a", "b"))
```

`CteRows` needs the name and at least one column, all non-empty strings — else `QueryError`. A
composite `pk__in` compares as many columns as the key has, in the key's order. Unlike `RawSQL`,
`CteRows` quotes the names in the database's own way and keeps the query's [plan](query-plan-cache.md).

## <a id="sample"></a>A sample of the table

```python
def sample(
    self, percent: float, *, method: TableSampleMethod | str = TableSampleMethod.BERNOULLI, seed: int | None = None
) -> Self
```

`sample()` reads a random sample of the model's table instead of all of it — `TABLESAMPLE` in `FROM`.
The sample is taken from the table first; the queryset's filters, joins, grouping and ordering then
apply to its rows. It is the cheap way to estimate over a large table: a count, an average, a look at
a few rows.

```python
from hare.query.enums import TableSampleMethod

# About 1% of the rows - each row kept with a 1% chance.
rough = await Event.objects.sample(1).count() * 100
# The same rows every time, while the table doesn't change.
preview = await Event.objects.sample(5, seed=42).filter(status="open").order_by("id")[:20]
# Faster on a huge table: whole storage blocks are kept or skipped.
average = await Event.objects.sample(0.5, method=TableSampleMethod.SYSTEM).aggregate(avg=Avg("price"))
```

```sql
SELECT ... FROM "event" TABLESAMPLE BERNOULLI (5) REPEATABLE (42) WHERE "status" = $1 ORDER BY "id" LIMIT 20
```

- **`percent`** — the chance each row (or block) is kept, an `int` or `float` from `0` to `100`: `100`
  reads every row, `0` none. The number of rows read is random around `percent` of the table, not
  exactly it.
- **`method`** — `TableSampleMethod.BERNOULLI` (the default) reads the whole table and keeps each row
  on its own; `TableSampleMethod.SYSTEM` keeps or skips whole storage blocks — much faster on a large
  table, but the rows of a block come together, so the sample is less even. The name is accepted as a
  string in any case (`"system"`).
- **`seed`** — an `int` from `0` to `2_147_483_647` (`REPEATABLE`): the same seed reads the same rows
  while the table doesn't change; `None` (the default) takes a new sample each time.
- A wrong argument — a negative or larger percent, `True`, `"10"`, `nan`, another method, a negative
  seed, `1.0` as a seed — raises `QueryError` at the call.
- **Where it applies.** Model rows, `values()`/`values_list()`, `count()`, `exists()`, `aggregate()`,
  `group_by()`/`annotate()` groups, `select_related()` (the sample is of the model's table; the
  related rows are joined to it in full), and the queryset as a subquery value
  (`filter(pk__in=Event.objects.sample(10))`).
- **Writes.** `update()`, `delete()` and `hard_delete()` on a sample raise `QueryError` —
  `TABLESAMPLE` is a part of `SELECT` only. Write the sampled rows through their keys:
  `Event.objects.filter(pk__in=Event.objects.sample(10, seed=1)).delete()`.
- A `Meta.manager` whose `get_queryset()` returns a sample raises `ConfigurationError` when a relation
  needs its scope as a `JOIN` condition, like `with_cte()`. A combined queryset
  (`union()`/...) raises `QueryError` — sample a branch.
- PostgreSQL and ClickHouse (`features.supports_table_sample`): on another database the query raises
  `UnSupportedError` before any SQL is sent. Its query keeps no [plan](query-plan-cache.md): the
  percent and the seed are written into `FROM`.
- ClickHouse samples by the table's sample key — `SAMPLE <share>`, no method or seed: see
  [its sample](../dialects/clickhouse/query-modifiers.md#sample).

## <a id="returning"></a>Returning the written rows

`update(...)`, `delete()` and `hard_delete()` await to the number of rows they wrote. `.returning()`
on them awaits to the rows themselves instead — written once, read in the same statement:

```python
# The named fields of each updated row, as written - a dict per row.
rows = await Product.objects.filter(stock__lt=5).update(price=F("price") * 1.1).returning("id", "price")
# [{"id": 3, "price": Decimal("11.00")}, {"id": 8, "price": Decimal("5.50")}]

# The model instances - every field loaded, saved instances.
products = await Product.objects.filter(category=old).update(category=new).returning()

# The deleted rows.
gone = await Session.objects.filter(expires_at__lt=now).delete().returning("id", "user")
```

```sql
UPDATE "product" SET "price"="price"*$1 WHERE "stock"<$2 RETURNING "id","price"
```

- **Fields.** A concrete field of the model, a forward relation (`"user"` — its key, a tuple for a
  composite one) or `"pk"` (a tuple for a composite primary key); values decode as the field reads
  them. A path through a relation, a reverse or many-to-many relation, an unknown name or a name
  given twice raises `FieldError` at the `returning()` call.
- **No fields** returns the model instances: the write returns the primary keys, and the rows are read
  by them in the write's transaction — each instance has every field, as a query would load it.
- **An update** returns each row as written: the new values, the `auto_now` fields and the version
  `Meta.optimistic_lock_field` bumped. A value written from an expression and checked in Python
  (an integer or decimal on SQLite) is returned as stored. A filter through a relation, a slice or an
  ordering work as on `update()`.
- **A delete** returns each row as it was deleted. A plain `DELETE` the database carries out alone
  returns the named fields through its own `RETURNING`. A delete hare carries out in Python — an
  `on_delete` the database doesn't enforce, a model with a cascade cycle, the instances — reads the
  matched rows first and deletes them in one transaction. A soft delete (`Meta.soft_delete_field`)
  returns the rows after it: their deletion time set; a row already soft-deleted isn't deleted again
  nor returned (`include_deleted().delete()`). `hard_delete().returning()` deletes for real.
- No row matched — an empty list. `none()` and `limit(0)` send no statement.
- The order of the rows is the database's (`RETURNING`), else the order the delete matched them in.
- `returning()` needs `features.supports_returning` (PostgreSQL and SQLite 3.35+): on another
  database it raises `UnSupportedError` before any SQL is sent. The statement keeps its
  [plan](query-plan-cache.md), the returned fields part of its key.
- **The values before the update.** `update(...).returning(*fields, old=(...))` also returns the
  fields named in `old` as they were before the update, under the key `"old"` — the change of each row
  in the same statement (PostgreSQL 18, `RETURNING old.column`):

    ```python
    rows = await Account.objects.filter(id__in=ids).update(balance=F("balance") - fee).returning(
        "id", "balance", old=("balance",)
    )
    # [{"id": 7, "balance": Decimal("90.00"), "old": {"balance": Decimal("100.00")}}, ...]
    ```

    `old` takes the same names as the fields and needs named fields (the instances have no place for
    the old values); `"old"` itself can't be among the named fields then. A server without
    `features.supports_returning_old_new` (PostgreSQL before 18, SQLite) raises `UnSupportedError`
    before any SQL; `merge().returning()` takes `old=` the same way.

## <a id="insert-from"></a>Inserting the rows of a query

```python
def insert_from(self, queryset: QuerySet, *, fields: Sequence[str]) -> InsertFromQuery
```

`insert_from()` writes the rows a queryset selects into the model's table in one statement — the
rows never come to Python. It awaits to the number of rows written:

```python
written = await Archive.objects.insert_from(
    Event.objects.filter(finished=True).values("name", "tournament_id", archived=F("modified")),
    fields=["title", "tournament", "archived_at"],
)
```

```sql
INSERT INTO "archive" ("title","tournament_id","archived_at","created")
SELECT "name","tournament_id","modified",$1 FROM "event" WHERE "finished"=$2
```

- **`fields`** — the written fields, in the order the source selects its columns: a concrete field,
  or a forward relation to a single-column key (`"tournament"` writes its key column). A relation to
  a composite key is written through its key fields; an unknown, generated or many-to-many field, or
  a field given twice, raises `FieldError` at the call.
- **The source** — a `values()`/`values_list()` queryset selecting one column per field (expressions
  and annotations included, `values(total=F("price") * F("quantity"))`), a queryset of models (it
  selects its own fields of the same names), or a `union()` of such querysets. Filters, joins,
  ordering, slices and `distinct()` apply as in a read. A source selecting another number of columns
  raises `QueryError` before any SQL is sent. The source runs on the target's connection.
- **The fields not named** get their database default (`db_default`, a serial key). An
  `auto_now`/`auto_now_add` field gets the moment of the insert, one for every row. A Python
  `default=` (a callable, a constant) isn't computed — name the field, or give it a `db_default`.
- **Tenants.** `Meta.tenant_field` not named is written as the active tenant (`QueryError` without a
  scope of one tenant, as `create()`). Under an active scope the tenant can't be read from the
  source rows, nor a relation to a tenant-scoped model — the insert can't check them row by row — and
  that raises `QueryError`; on the target's `all_tenants()` the source rows are written as they are.
- The rows aren't read back: no instance is created, no `save()` is called, no signal of a model
  instance is sent; a `RowsChanged` observer gets the insert without the keys.
- A combined target queryset (`union()`/...) raises `QueryError`. The statement keeps no
  [plan](query-plan-cache.md).

## <a id="merge"></a>Merging rows

```python
def merge(self, source, *, on: str | Sequence[str] | Mapping[str, str]) -> MergeQuery
```

`merge()` matches the source rows to the model's rows and writes them in one `MERGE` statement —
branch by branch, in the order they are added: a matched row is updated, deleted or left alone, a
source row nothing matches is inserted or left out, and (PostgreSQL 17+) a row of the model no source
row matches is updated, deleted or left alone. It awaits to the number of rows written.

```python
from hare.query.expressions import F, Q

written = await (
    Stock.objects.merge(
        [{"sku": "A-1", "count": 5}, {"sku": "B-7", "count": 2}],
        on="sku",
    )
    .when_matched(delete=True, condition=Q(count__lte=F("merge_source__count")) & Q(merge_source__count__lt=0))
    .when_matched(update={"count": F("count") + F("merge_source__count")})
    .when_not_matched(insert={"sku": F("merge_source__sku"), "count": F("merge_source__count")})
)
```

```sql
MERGE INTO "stock" USING (SELECT column1 AS "sku", column2 AS "count"
  FROM (VALUES (CAST($1 AS VARCHAR), CAST($2 AS INTEGER)), (...)) AS "hare_merge_values") AS "hare_merge_source"
ON "stock"."sku"="hare_merge_source"."sku"
WHEN MATCHED AND ... THEN DELETE
WHEN MATCHED THEN UPDATE SET "count" = "stock"."count"+"hare_merge_source"."count", "version" = "stock"."version"+$3
WHEN NOT MATCHED THEN INSERT ("sku", "count") VALUES ("hare_merge_source"."sku", "hare_merge_source"."count")
```

- **The source** — a list of dicts with the same keys (each key a column; a key named after a field
  of the model is cast to its type, a related instance written as its key), a `values()` queryset
  (its names are the columns), a queryset of models (all its fields) or a `union()` of `values()`
  querysets. A `values_list()` queryset raises `QueryError` — its columns have no names. An empty
  list writes nothing and sends no statement.
- **`on`** — the matched fields: a field name, a list of them, or a dict of the model's field to the
  source column (`on={"sku": "code"}`). A field is a concrete field or a forward relation to a
  single-column key.
- **The source row** is read as `merge_source__<column>` — in a branch's values
  (`F("merge_source__count")`, expressions of it) and its condition (`Q(merge_source__count__gt=0)`,
  any lookup). `F("<field>")` and the plain keys of `Q` read the model's row. A column the source
  doesn't have raises `FieldError`; a model with a field named `merge_source` raises `QueryError`.
- **Branches** — `when_matched(update=... | delete=True | do_nothing=True, condition=Q)`,
  `when_not_matched(insert=... | do_nothing=True, condition=Q)`,
  `when_not_matched_by_source(update=... | delete=True | do_nothing=True, condition=Q)`: exactly one
  action each. A row takes the first branch whose condition holds; `do_nothing` keeps the later ones
  from taking it. The values are plain values, related instances, `F()` and expressions; a value
  is converted and checked by its field as in `update()`.
- **The model's rows read** are those of the target queryset: its filters
  (`Stock.objects.filter(warehouse=w).merge(...)`) and the default scope (soft-deleted rows, other
  tenants' rows) limit the rows a source row can match — a hidden row counts as missing, so its source
  row goes to `when_not_matched()`. `when_not_matched_by_source()` takes only those rows too. The
  target queryset can't annotate, slice, `distinct()`, `group_by()`, `sample()` or `with_cte()` — and a
  filter can't cross a relation (`QueryError`).
- **Written for you.** An update sets the `auto_now` fields to the moment of the statement and bumps
  `Meta.optimistic_lock_field`; an insert sets the `auto_now`/`auto_now_add` fields and, under a
  scope of one tenant, `Meta.tenant_field`. A Python `default=` isn't computed for an insert — name the
  field, or give it a `db_default`. Updating the primary key raises `QueryError`.
- **Tenants.** Under an active scope a tenant written from an expression, an update moving a row out
  of the scope, or a relation to a tenant-scoped model written from an expression raises `QueryError`;
  related instances written as plain values are checked against their model's scope. On the target's
  `all_tenants()` the values are written as given.
- **Deletes** run in the database alone: a model with `Meta.soft_delete_field`, an `on_delete` the
  database doesn't enforce or a `PROTECT` raises `QueryError` for a `delete=True` branch — use
  `delete()`.
- **`.returning(*fields)`** (PostgreSQL 17+) awaits to the written rows: a dict of the named fields
  plus `"merge_action"` — `"insert"`, `"update"` or `"delete"` (a deleted row as it was). At least one
  field is named. `old=("count",)` adds those fields as they were before the merge under `"old"` —
  None for an inserted row (PostgreSQL 18, [as on `update()`](#returning)).
- PostgreSQL 15+ (`features.supports_merge`); `when_not_matched_by_source()` and `returning()` need
  PostgreSQL 17 (`supports_merge_not_matched_by_source`, `supports_merge_returning`). On another
  database or an older server the merge raises `UnSupportedError` before any SQL is sent. A merge
  without a branch raises `QueryError`. The statement keeps no [plan](query-plan-cache.md).
