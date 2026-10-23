# Schema per tenant

With a schema per tenant (PostgreSQL), each tenant keeps the tables of some models in a database schema
of its own — `tenant_acme.invoice`, `tenant_7.invoice` — while the other models keep one table shared by
every tenant in the connection's own schema (`public`). A query never carries a tenant filter: the active
[`Tenancy.scope(tenant)`](multi-tenancy.md#tenancy-scope) picks the schema, and the connection reaches it
through PostgreSQL's schema search path. It is an alternative to [`Meta.tenant_field`](multi-tenancy.md)
(a tenant column in a shared table): the tenants' rows are apart at the database level, a tenant's data
is dropped with one `DROP SCHEMA`, and every index and constraint is per tenant.

## <a id="setup"></a>Setting it up

The connection names its tenants' schemas with `tenant_schema_template` — lower-case letters, digits and
`_` around one `{tenant}`:

```python
config = {
    "connections": {
        "default": {
            "engine": "postgresql",
            "credentials": {
                "host": "localhost", "database": "app", "user": "app", "password": "...",
                "tenant_schema_template": "tenant_{tenant}",
            },
        },
    },
    "apps": {"billing": {"models": ["billing.models"], "default_connection": "default"}},
}
```

In a URL it is a query parameter, with the braces encoded:
`postgresql://app@localhost/app?tenant_schema_template=tenant_%7Btenant%7D`. Any other template (no
placeholder, two placeholders, upper-case letters, `-`) raises `ConfigurationError` when the connection
is configured. SQLite and ClickHouse have no schemas per tenant — `Features.supports_tenant_schemas` is
False, and `TenantSchemas` raises `UnSupportedError` before any SQL.

A model whose table lives in each tenant's schema declares `Meta.tenant_schema = True`:

```python
class Customer(Model):          # shared - one table in "public"
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=100)


class Invoice(Model):           # one table in each tenant's schema
    id = fields.IntField(primary_key=True)
    customer = fields.ForeignKeyField("billing.Customer", related_name="invoices")
    total = fields.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        tenant_schema = True
```

- It can't be given with `Meta.schema` (`ConfigurationError`): the table's schema is the active tenant's.
- A tenant's model may relate to a shared model (`Invoice.customer` above): its foreign key in each
  tenant's schema references the shared table. A shared model can't have a `ForeignKeyField`,
  `OneToOneField` or `ManyToManyField` to a tenant's model — which tenant's table would it reference? —
  `ConfigurationError` names the field; declare the relation on the tenant's model.
- Deleting a shared row — `delete()`, `hard_delete()`, `QuerySet.delete()`, inside a tenant's scope or
  outside any — carries out the `on_delete` of the tenants' rows pointing at it in every tenant's schema,
  in one transaction: their `CASCADE` and soft delete, `SET_NULL`, and a `PROTECT` row of any tenant
  raises `ProtectedError` before anything is written. `restore(cascade=True)` brings back the rows
  deleted with it in every tenant's schema, and `delete_preview()` counts and lists them all. The
  transaction visits the schemas one by one (a `SET LOCAL search_path`), so the delete reads the list of
  tenant schemas and costs a few statements per tenant. A hard delete the database's own `ON DELETE`
  carries out in every schema visits none — only for a `PROTECT` relation, a `db_constraint=False` one,
  captured changes or an overridden `delete()` of a tenant's model. A schema without a model's table
  (made before its migration) holds no row of it and is passed over.
- A `Meta.tenant_schema` model on a connection without `tenant_schema_template` raises
  `ConfigurationError` on its first query and when its tables are created.

## <a id="tenant-values"></a>Tenant values

A tenant's schema is the template with `{tenant}` replaced by the tenant value: an `int` (`7` →
`tenant_7`) or a string of lower-case letters, digits and `_` (`"acme"` → `tenant_acme`). Any other value
— `"Acme"`, `"acme-1"`, `""`, a `UUID` (pass `uuid.hex`), a `float` — raises `ValidationError`, as does a
schema name longer than PostgreSQL's 63 bytes. There is no normalization: `"Acme"` is refused rather than
lower-cased into another tenant's schema. `7` and `"7"` name the same schema.

## <a id="tenant-schemas"></a>Creating, dropping and listing tenants

`hare.models.tenancy.tenant_schemas.TenantSchemas`:

```python
from hare.models.tenancy.tenant_schemas import TenantSchemas

await TenantSchemas.create("acme")                      # CREATE SCHEMA tenant_acme + the tables of every tenant_schema model
await TenantSchemas.create(7, create_tables=False)       # the schema only - `migrate` creates the tables
await TenantSchemas.get_tenants()                       # ["7", "acme"] - the schemas the template names, as text
await TenantSchemas.drop("acme")                        # DROP SCHEMA tenant_acme CASCADE - every table and row of the tenant
```

Each takes `using=` — the connection alias, optional with a single connection; `create()` returns the
schema's name. They run outside any tenant scope and transaction — inside one they raise `QueryError` (a
schema created in a transaction isn't visible yet to the tenant's own connection). `create()` with
`create_tables=True` (the default) creates the tables from the current models, as `generate_schemas()`
does — for a project without migrations, and for tests. A project with migrations creates the schema with
`create_tables=False` and runs `migrate`, which brings every tenant schema up to date
([Migrations](#migrations)). `get_tenants()` lists the schemas whose name the template gives, so a schema
created by hand as `tenant_beta` counts as the tenant `"beta"`. A query in the scope of a tenant whose
schema doesn't exist raises `ConfigurationError` naming it, until the schema is created.

## <a id="queries"></a>Queries

Inside `Tenancy.scope(tenant)` of a single tenant, every query of the connection runs on a client of that
tenant's schema: its connections have the search path `tenant_<tenant>, public` (the connection's own
`schema` setting in place of `public` when given). A tenant's model reads and writes its tenant's table;
a shared model is found in `public` through the same search path — joins between the two
(`select_related("customer")`) are ordinary joins.

```python
with Tenancy.scope("acme"):
    await Invoice.objects.create(id=1, customer=customer, total=10)
    await Invoice.objects.select_related("customer").all()      # tenant_acme.invoice JOIN public.customer
with Tenancy.scope(7):
    await Invoice.objects.count()                                # tenant_7.invoice - 0
```

- A tenant's model queried with no scope, a scope of several tenants (`Tenancy.any_of(...)`),
  `Tenancy.ALL`, or a scope given model by model raises `QueryError` — its table is reached inside a
  scope of a single tenant. A shared model works in every one of them, through the connection's own
  client.
- A queryset runs in the schema of the tenant active when it is awaited, as a `Meta.tenant_field` model's
  queryset filters by that tenant: built inside `Tenancy.scope("acme")` and awaited inside
  `Tenancy.scope(7)`, it reads `tenant_7`; awaited with no scope, it raises `QueryError`. The queries it
  runs then (`prefetch_related()`, a subquery) use the same schema, as for a
  [router](multi-tenancy.md#routers).
- The client of each tenant's schema is a connection pool of its own, opened on its first query and
  closed with the connection (`Hare.close_connections()`). A connection of it left unused for the
  connection's `max_inactive_connection_lifetime` (300 seconds by default) is closed, so the server holds
  sessions for the tenants queried lately, not for every tenant ever queried.
  `Connections.get(connection_alias)` gives it inside a scope;
  `ConnectionHandler.get_main(connection_alias)` always gives the connection itself.
- With `tenant_schema_template`, a `Tenancy.scope()` value must be a valid tenant value on that
  connection even for a query on a shared model — a scope of `"Acme"` raises `ValidationError` on the
  first query of the connection.
- `Meta.tenant_field` works next to it: a tenant's model may also declare a tenant column, filtered
  inside its schema as usual.

## <a id="transactions"></a>Transactions

A transaction runs in the schema of the tenant active when it began — its queries all go through one
connection. Inside it, a query under another tenant's scope, or under no tenant when it began in a
tenant's schema (and the other way round), raises `QueryError` instead of silently running in the
transaction's schema:

```python
with Tenancy.scope("acme"):
    async with Transactions.atomic():
        await Invoice.objects.create(...)              # tenant_acme
        with Tenancy.scope("beta"):
            await Invoice.objects.count()              # QueryError - the transaction runs in tenant_acme
```

`Transactions.autonomous()` inside a scope opens its connection in the same tenant's schema.

## <a id="migrations"></a>Migrations

`migrate` on a connection with `tenant_schema_template` runs in two steps:

1. **The shared schema** — with the journal `public.hare_migrations`. Operations on shared models' tables
   run; operations on `Meta.tenant_schema` models' tables are skipped.
2. **Each tenant's schema** (the schemas `TenantSchemas.get_tenants()` lists), in order of the schema
   name — each with its own journal `tenant_<tenant>.hare_migrations`, inside `Tenancy.scope(tenant)`
   (the tenant value as text). Operations on tenant models' tables run; operations on shared models'
   tables are skipped.

So a tenant added later (`create(..., create_tables=False)`) gets every migration on the next `migrate`,
and the others get only what they lack. The migration state is the same for every schema —
`CreateModel(..., options={"tenant_schema": True})` records the option.

- An operation on a model's table (`CreateModel`, `AddField`, `AddIndex`, a view or policy declared on
  the model, ...) follows the model's `Meta.tenant_schema`.
- An operation bound to no model — `RunSQL`, `RunPython`, `CreateExtension`, `CreateSchema`, an enum type
  — runs in the shared schema. `RunSQL(..., tenant_schema=True)` and `RunPython(..., tenant_schema=True)`
  run in each tenant's schema instead; the function of such a `RunPython` runs once per tenant, with the
  tenant's scope active, so its queries of tenant models reach that tenant's table.
- Inside `Tenancy.scope(tenant)`, `migrate` brings only that tenant's schema to the target — the shared
  schema and the other tenants are left as they are.
- `migrate` to `app.zero` unapplies in the shared schema and in every tenant's schema.
- `sqlmigrate` shows the SQL of every operation — shared and tenant ones — as written for one schema.
- Changing `Meta.tenant_schema` of an existing model is refused by `makemigrations`
  (`ConfigurationError`): its rows would move between the shared schema and every tenant's. Write it by
  hand — a new model, a `RunPython` copying the rows, then removing the old model.
- `hare drift` compares the shared models in the shared schema; `detect_drift()` with a tenant's client
  (`Connections.get("default").get_schema_client("tenant_acme")`) compares the `Meta.tenant_schema`
  models in that tenant's schema. Neither reports the other side's tables as missing, and a tenant
  model's foreign key to a shared model is read whichever schema after the tenant's holds it.

## <a id="choosing"></a>Choosing between a tenant column and a schema per tenant

| | `Meta.tenant_field` | `Meta.tenant_schema` |
|---|---|---|
| Where a tenant's rows are | one shared table, a tenant column | a table in the tenant's schema |
| Isolation | the ORM's filter | the database: another tenant's table isn't on the search path |
| Queries across tenants | `Tenancy.any_of(...)`, `Tenancy.ALL` | not through the ORM — one scope, one tenant |
| Removing a tenant | `DELETE` by the tenant column | `TenantSchemas.drop(tenant)` |
| Many tenants | one table for all | one set of tables and indexes, and one connection pool, per tenant |
| Databases | every dialect | PostgreSQL |
