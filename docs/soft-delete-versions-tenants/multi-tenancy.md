# Multi-tenancy

Auto-scopes every default-manager query on a model to the tenants currently active for it —
mirrors [soft delete](soft-delete.md)'s own `Meta.soft_delete_field` mechanism
exactly, just filtering on a tenant column instead of a deletion flag.
A tenant's tables can instead live in a schema of its own — see [Schema per tenant](schema-per-tenant.md).

```python
class Order(Model):
    company_id = fields.IntField(db_index=True)
    ...
    class Meta:
        tenant_field = "company_id"
```

```python
from hare.models.tenancy.tenancy import Tenancy

with Tenancy.scope(company.id):
    await Order.objects.filter(status="pending")   # WHERE company_id = <company.id> AND status = 'pending'
    await Order.objects.using(...).create()                # company_id filled from the scope - see below
```

`tenant_field` can also name a forward `ForeignKeyField`/`OneToOneField` — `tenant_field =
"company"` for `company = fields.ForeignKeyField("models.Company")` means its key column,
`company_id`, and `Order._meta.tenant_field` reads `"company_id"`.

## <a id="tenancy-scope"></a>`Tenancy.scope(tenant)`

```python
from hare.models.tenancy.tenancy import Tenancy

with Tenancy.scope(company.id):                          # one value, for every model
    ...

with Tenancy.scope(Tenancy.any_of("msk", "kzn")):        # several values, for every model
    ...

with Tenancy.scope(Tenancy.ALL):                         # every tenant
    ...

with Tenancy.scope({Order: Tenancy.any_of("msk", "kzn"), SalesSummary: 2, Faq: Tenancy.ALL}):
    ...                                                  # each model's own tenants
```

| `tenant` | Rows a query sees | A row written without a tenant | A row written with a tenant |
|---|---|---|---|
| A value | `WHERE <tenant_field> = value` | gets the value | must be that value |
| `Tenancy.any_of(*values)` | `WHERE <tenant_field> IN (values)` | `QueryError` — name it | must be one of the values |
| `Tenancy.ALL` | every tenant's, no filter | `QueryError` — name it | any value |
| A dict by model | each model by its own entry — one of the three above | by the model's entry | by the model's entry |

A value, `Tenancy.any_of(...)` and `Tenancy.ALL` given directly apply to every model alike:
`Meta.tenant_field` only names *which* column a given model uses (`company_id` on one model,
`org_id` on another), and the scope is compared with whatever column each model names. That fits
models split by the same tenants. Models split by different fields with different sets of values
(orders by `store` — `"msk"`/`"kzn"`, sales summaries by `store_id` — `1`/`2`, FAQ entries by
`group`) take a dict.

`Tenancy.any_of()` needs at least one value and takes no `None` — `QueryError` (a `ValueError`)
otherwise: no value at all neither means every tenant nor no rows. A value repeated is kept once,
and `Tenancy.any_of(value)` with one value is the same scope as `value` itself. A list, a tuple or
a set given as the scope (or as a model's entry) raises `QueryError` — several values are always
spelled `Tenancy.any_of(*values)`.

### <a id="scope-by-model"></a>A scope by model

The keys of the dict are model classes with `Meta.tenant_field`; anything else, and `None` as an
entry, raises `QueryError` when the scope is entered. A model takes:

1. the entry of its own class;
2. else the entry of its nearest base class with the same `Meta.tenant_field` — one entry for an
   abstract base covers every model built on it, and a model's own entry overrides it;
3. else it has **no scope**: it behaves exactly as outside any `Tenancy.scope()` — a query raises
   `QueryError` (see [below](#no-active-scope)), a write trusts the tenant it names. Give a model
   `Tenancy.ALL` to leave it unlimited on purpose.

A [swapped model](../models/relations.md#swappable-models) is looked up by the class that replaces
it, like any other model — name that class, or a base class both share.

Every part of one query reads its own model's entry: the query's own filter, a JOIN to a related
model, the second query of `prefetch_related()`, a many-to-many's through table and target.

```python
with Tenancy.scope({Order: Tenancy.any_of("msk", "kzn"), Customer: "north"}):
    # orders of two stores, joined to the customers of the north only
    await Order.objects.filter(customer__name__startswith="A")
```

`Tenancy.get_scope(model)` gives a model's scope in the current task: the value, the
`Tenancy.any_of(...)` object (its `values` is the tuple), `Tenancy.ALL`, or `None` when no scope
is active or the active one doesn't name the model. Use it instead of reading
`Tenancy.current.get()`, which holds the whole scope — for a dict, an object whose
`get_for_model(model)` answers the same question. `Tenancy.get_values(scope)` turns a model's
scope into the tuple of values its rows are limited to — one value, the `any_of` values — or
`None` when they aren't limited (no scope, or `Tenancy.ALL`).

### <a id="scope-lifetime"></a>Lifetime

Nests correctly: a nested `Tenancy.scope(...)` **replaces** the outer scope for the queries it
wraps — the two are not merged, so inside a nested dict a model the dict leaves out has no scope —
and the outer block's scope is back in effect once the inner block exits. Each `asyncio` task has
its own scope.

A queryset runs for the scope active when it is **awaited**, and everything it runs then is
scoped by that one scope: its own filter, every JOIN it makes, `.values()`/`.values_list()`/
`.count()` built from it, its use as a subquery (`field__in=queryset`, `Subquery(...)`,
`Exists(...)`) and the second query of `.prefetch_related()`.

A scope value is compared with a row's tenant after the tenant field's own conversion, so for a
`UUIDField` tenant `Tenancy.scope(uuid_value)` and `Tenancy.scope(str(uuid_value))` are the same
tenant on every backend.

`Tenancy.set(tenant)`/`Tenancy.reset(token)` are the lower-level primitives
`Tenancy.scope()` itself is built on, for a case where a single `with` block can't span the
scope's intended lifetime (e.g. across an `await` boundary spanning multiple callback
invocations) — the same shape as the `QueryTags.set()`/`QueryTags.reset()` pair of
[SQL comment tags](../observability/query-tags.md).

## <a id="no-active-scope"></a>No active scope → `QueryError`

Querying a tenant-scoped model that has no scope — no `Tenancy.scope()` is active, or the active
one is a dict that doesn't name the model — raises `QueryError` immediately — deliberately,
instead of silently filtering by `tenant_field = None` (which would just match zero rows and look
like "no data" rather than "you forgot to scope this call"). If you hit this, wrap the call in
`Tenancy.scope(...)`, or add the model to the dict.

## <a id="all-tenants"></a>`Model.objects.all_tenants()` — the escape hatch

```python
await Order.objects.all_tenants().filter(status="pending")   # every tenant, no scoping
```

Returns a `QuerySet` that bypasses the automatic filter entirely — for admin tooling, background
jobs, and migrations that deliberately need to see across every tenant. Tenant-scoped models the
query reaches through relations (a relation filter, `Count("docs")`, `values("docs__title")`, ...)
span every tenant too. On a model with no `Meta.tenant_field` of its own that is all it does — the
way to run a relation query from a shared model with no tenant active:

```python
await Tag.objects.all_tenants().filter(docs__isnull=True)          # tags no tenant links to
await Tag.objects.all_tenants().annotate(uses=Count("docs"))       # links of every tenant
await tag.docs.clear(all_tenants=True)                     # every tenant's links of this tag
```

Mirrors
[`Model.objects.include_deleted()`](soft-delete.md)'s own bypass shape: it constructs a
bare, unfiltered queryset directly rather than "un-filtering" one that already has the tenant
condition applied.

`.all_tenants().delete()` is a deliberate cross-tenant delete: it deletes every matched row
whatever the active scope, both when it runs as one statement and when it deletes row by row
(a model other models point at, whose cascade runs per row).

## <a id="write-path-guards"></a>Write-path guards

Every write is checked against the scope of the model it writes (`Tenancy.get_scope(model)`):
"the scope's value" below is the one value of that scope, and "inside the scope" means that value,
one of the `Tenancy.any_of(...)` values, or anything under `Tenancy.ALL`.

- **`create()` and `save()` of a new row fill `tenant_field` with the scope's value**, and raise
  `QueryError` when the row names a tenant outside the scope. Under `Tenancy.any_of(...)` and
  `Tenancy.ALL` there is no one value to fill in: the row has to name its tenant, `QueryError`
  otherwise. So do `get_or_create()` and `update_or_create()` when they create — pass the tenant
  in `defaults`/`create_defaults`. When `tenant_field` is a `ForeignKeyField`'s own column
  (`tenant_field = "company_id"` for `company = fields.ForeignKeyField(...)`), passing the relation
  (`create(company=company)`) counts as giving the tenant: accepted with no scope, checked against
  the scope inside one.
- **`save()` of an existing row** writes only a row whose tenant — the one on the instance and the
  one stored in the database — is inside the scope: the `UPDATE` carries the scope in its `WHERE`,
  so an instance built by hand around another tenant's primary key writes nothing. Changing the
  tenant of an instance and saving it moves the row, to a tenant inside the scope only. `delete()`
  and `restore()` of an instance are checked the same way.
- **A forward FK/O2O can't point at a row its own model's scope doesn't show.** `create()`,
  `save()`, `.update(...)`, `bulk_create()` and `bulk_update()` read the tenant of every
  tenant-scoped row a written relation names (one query per relation and statement) and raise
  `QueryError` when it is outside the scope **of the related model** — from a tenant-scoped model
  and from a shared one alike, as M2M `add()` rejects such a row. With one value for every model
  that is "the related row is the active tenant's". A related model with no scope or with
  `Tenancy.ALL` isn't checked, nor are `.all_tenants().update(...)` and writes with no scope at all
  (seed data, trusted like an explicit `tenant_field` there). A key naming no row is left to the
  database.
- **M2M `add()` fills a through model's own `tenant_field`** with the value of the through model's
  scope, and needs it in `through_defaults` under a scope of several values (see
  [A `Model` as `through`](../models/relations.md#a-model-as-through)).
- **`.update(<tenant_field>=value)` moves rows inside the scope only.** `value` has to be a plain
  value inside the model's scope — under one value that is the value the rows already have, under
  `Tenancy.any_of("msk", "kzn")` an order moves between the two stores. A value outside the scope,
  `None`, an expression, and any such update with no scope or on `.all_tenants()` raise
  `QueryError`. `.update(company=...)` for a tenant relation follows the same rule as
  `.update(company_id=...)`.
- **`bulk_update()` checks every object's tenant explicitly.** `bulk_update()` matches rows purely
  by their own PK (like `Model.save()`) and deliberately does **not** go through the same
  auto-filtering path `.filter()`/`.all()` do — so before issuing anything, it verifies the model
  has a scope and that every object being updated belongs to a tenant inside it, raising
  `QueryError` naming the mismatched PKs otherwise. Without this explicit check, a caller
  that somehow obtained another tenant's primary keys could silently overwrite that tenant's rows.
- **`bulk_create()` requires a scope.** With none for the model it raises `QueryError`, even when
  every instance already carries a `tenant_field` value (unlike `create()`, which trusts an
  explicit tenant outside a scope). Inside a scope, an instance with no `tenant_field` value yet
  gets the scope's value — `QueryError` under several values or `Tenancy.ALL` — and an instance
  carrying a tenant outside the scope makes the whole call raise `QueryError` naming the
  mismatched PKs.
- **`bulk_create(update_fields=...)` only overwrites rows inside the scope.** `ON CONFLICT`
  matches the table's physical unique constraint regardless of tenant, so the `DO UPDATE` carries
  `WHERE <tenant column> = <value>` (`IN (<values>)` for several): a conflicting row of a tenant
  outside the scope is left untouched. As with `ignore_conflicts=True`, the objects are then not
  marked saved (the call can't tell which of them were written) unless `returning=True` with
  `on_conflict=[...]` matches the written rows back to them; an object whose row was skipped gets
  nothing from `RETURNING`. Not applied under `Tenancy.ALL` and `.all_tenants()`.
- **`QuerySet.delete()` and `.update()` reach the rows inside the scope**, like any query. A cascade
  from a deleted row follows its foreign keys to related rows of every tenant, and
  `restore(cascade=True)` brings back what that delete took with it — see
  [soft delete](soft-delete.md).

## <a id="relations"></a>Relations to a tenant-scoped model

A forward FK/O2O from any model to a tenant-scoped one sees only a target inside the related
model's own scope, like a soft-deleted target: `note.doc`, `select_related()`, `values("doc__title")` give `None` for a
target of another tenant, and `doc__isnull=True` (`doc=None`, `doc__not_isnull=False`) matches such
a row, `doc__isnull=False` doesn't. A null check on the target's own columns agrees: a hidden target
has no visible columns, so `doc__title__isnull=True` (`doc__title=None`) matches the same rows as
`doc__isnull=True`, in `filter()` and `exclude()` alike. The key column (`doc_id__isnull`,
`doc_id=...`) keeps comparing the stored value. A relation read or a relation-level null check of this type needs an active
scope (or `.all_tenants()` on the filtered queryset, which drops the tenant part of the check) — just
like `values("doc__title")`. The same applies to a target a custom `Meta.manager` filters out.

A many-to-many relation to a tenant-scoped model is scoped the same way, from either side:
`Tag.objects.filter(docs=doc_pk)`, `docs__in`, `docs__not`, `docs__not_in`, their `exclude()` forms and
`docs__isnull` count only links to rows of the active tenant (and not soft-deleted, and not hidden by
a custom `Meta.manager`), exactly like `docs__title=...` does. So does every path through the
relation — `docs__id__isnull`, `docs__title=...`, `values("docs__id")`, `order_by("docs__title")`,
`F("docs__title")`, `Count("docs")`: a link to a hidden row adds no row and no `NULL` of its own.
`tag.docs.clear()` and `tag.docs.set(...)` remove only such links too — a link from the same tag to
another tenant's row stays; `add()`/`remove()` reject a related object of another tenant outright, and
`add()` also rejects a row the database shows as soft-deleted or hidden by the related model's
`Meta.manager` (a stale or hand-built instance included).

> [!WARNING]
> **Whether a shared row is in use depends on the active tenant**
>
> For a model with no `Meta.tenant_field` (a tag shared by every tenant), "has links" means "has
> links to rows the active tenant sees". Inside `Tenancy.scope(1)`,
> `Tag.objects.filter(docs__isnull=True).delete()` also deletes a tag that only tenant 2's documents use.
> Clean up shared rows with no tenant active, through `Tag.objects.all_tenants()` (see above), so every
> tenant's links count.

## <a id="routers"></a>Routers

A [router](../connections/multiple-databases.md#routers) choosing a database by tenant (sharding) runs with the
scope its query was built for, so the connection it picks holds the tenants the query filters by.
`Tenancy.get_scope(model)` inside `db_for_read(model)`/`db_for_write(model)` gives that model's
part of it: one value; a `Tenancy.any_of(...)` object, whose values the router has to map to one
database or refuse; `Tenancy.ALL`; or `None`. `Tenancy.current.get()` still reads the whole
scope — the value itself for a scope of one value.

```python
class StoreRouter:
    def db_for_read(self, model):
        stores = Tenancy.get_values(Tenancy.get_scope(model))
        if stores is None:                                # no scope, or every tenant
            return None                                   # the default connection
        shards = {SHARD_BY_STORE[store] for store in stores}
        if len(shards) > 1:
            raise RuntimeError(f"{model.__name__}: stores {stores} are on different shards")
        return shards.pop()

    db_for_write = db_for_read
```

## <a id="pydantic"></a>Pydantic input models

[`pydantic_model_creator()`](../integrations/pydantic.md) makes `Meta.tenant_field` optional in an input model:
left out, `create()` fills it with the scope's value. Under a scope of several values (or
`Tenancy.ALL`) the request has to carry it — it is accepted and checked against the scope like any
`create()` value, and leaving it out raises `QueryError`.

## <a id="soft-delete"></a>Interaction with `Meta.soft_delete_field`

A model can set both options at once — `Manager.get_queryset()` adds both filters independently,
so a model with both configured gets both applied on every default-manager query. The two escape hatches don't imply each other, though:
`all_tenants()` still respects `soft_delete_field` (soft-deleted rows stay hidden unless you
*also* call `include_deleted()`), and `include_deleted()` still respects `tenant_field` (you still
need an active scope, or `all_tenants()`, to see across tenants).

## <a id="query-shape-cache"></a>Query plan cache

The tenant filter of a query on the scoped model itself is an ordinary filter value, added the way
`soft_delete_field`'s own `deleted_at=None` filter is, so the [query plan cache](../querying/query-plan-cache.md)
substitutes it per call like any other filter value, and the tenant is no part of the shape key —
unlike the zone name of the date-part lookups, which a cache hit doesn't substitute. A scope of
several values is an ordinary `__in` filter in the same place.

A query reaching a tenant-scoped model through a relation puts the tenant into the JOIN's `ON`
condition. Its plan keeps that scope's values apart from the query's own, and a query running on the
plan binds the scope active when it runs; a scope whose filter has another shape (another number of
tenants under `Tenancy.any_of(...)`) builds the query in full. The same holds for a model a custom
`Meta.manager` filters — see [Query plan cache](../querying/query-plan-cache.md#uncached-shapes).

## <a id="constraints"></a>`UniqueConstraint`/`ExclusionConstraint`

Neither is tenant-aware. A `UniqueConstraint` and PostgreSQL's
own `ExclusionConstraint` (the standard way to enforce "no two rows for the same resource can have
overlapping ranges" — see `DateTimeRangeField`) are both checked purely against the literal columns
you name — there is no automatic tenant scoping added underneath. Two rows with the same
`fields`/`expressions` values under *different* tenants will still collide unless you explicitly
include the tenant column in the constraint yourself (e.g.
`ExclusionConstraint(expressions=[("company_id", "="), ("resource", "="), ("during", "&&")])`).

## <a id="rollout"></a>Rollout

Unlike most of hare-orm's other opt-in `Meta` options, turning on `tenant_field` **changes the
behavior of every existing query** against that model — recommended rollout:

1. Add `Meta.tenant_field` to one model at a time, starting with something low-traffic.
2. Audit every existing call site against that model for a missing `Tenancy.scope()` before
   relying on the auto-filter — `all_tenants()` is your bridge for code you haven't updated yet.
3. Only then move on to the next model.

The filter above is enforced in Python, at the `Manager.get_queryset()` layer — a raw SQL query or a
bug that bypasses the ORM's default manager isn't covered by it. On PostgreSQL the database itself can
enforce it too — [row level security](#row-level-security).

## <a id="row-level-security"></a>The database's backstop: row level security

On PostgreSQL a `Meta.tenant_field` model can have the database keep each transaction to its tenants,
whatever SQL runs in it — a `Policy` with `TenantCondition()`, on a connection with
`tenant_row_level_security`:

```python
from hare.ddl import Policy, RowLevelSecurity, TenantCondition


class Order(Model):
    company_id = fields.IntField(db_index=True)
    ...
    class Meta:
        tenant_field = "company_id"
        row_level_security = RowLevelSecurity.FORCED
        policies = [Policy(name="order_tenant", using=TenantCondition())]
```

```python
config = {"connections": {"default": "postgresql://app@localhost/app?tenant_row_level_security=true"}, ...}
```

- **The transaction's tenants.** Every transaction on the connection sets its tenants right after
  `BEGIN`, from the `Tenancy.scope()` active when it began — `SELECT set_config('hare.tenant', ..., true)`,
  a JSON array of the tenant values as text: one value, `Tenancy.any_of(...)`'s values, or every tenant
  under `Tenancy.ALL`. The setting is local to the transaction — it ends with it, and a pooled connection
  never carries it into the next one.
- **The policy.** `TenantCondition()` is written as "the row's tenant column is one of the transaction's
  tenants" (every row under `Tenancy.ALL`); as the `using` of an `ALL` policy it also checks every row
  written. The setting is read with the column's own type, so the condition uses the column's index.
- **No tenant, no rows.** A transaction begun under no scope, or under a scope given model by model,
  sets no tenant — the policy lets it see and write none of the model's rows, raw SQL included.
- **Queries through the ORM** of such a model run inside a transaction that set its tenants — outside a
  transaction, or in one begun with no tenant, they raise `QueryError` before any SQL instead of
  returning nothing. A query under another scope than the one the transaction began in raises
  `QueryError` too: the transaction's setting can't follow it.
- A model with a `TenantCondition` policy on a connection without `tenant_row_level_security` raises
  `ConfigurationError` on its first query; one without `Meta.tenant_field` when it is declared.
- **Roles.** PostgreSQL doesn't apply row level security to a superuser or a role with `BYPASSRLS`,
  and applies it to the table's owner only with `RowLevelSecurity.FORCED` — connect as a role it
  applies to.
- The Python-side filter stays as it is — the policy is a second check under it, not a replacement.
- Changing `Meta.tenant_field` doesn't change the policy's text by itself — rename the policy in the
  same change so the migration drops and creates it.

SQLite and ClickHouse have no row level security: `Policy` there raises `UnSupportedError` before any SQL.
