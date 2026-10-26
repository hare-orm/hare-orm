# Views, functions, sequences and access

A model declares in its `Meta` the database objects that live beside its table: views, materialized
views, functions, sequences, row level security with its policies, and the privileges roles get. They are
created by `Hare.generate_schemas()` and by migrations — `makemigrations` writes an operation for each
one added, changed, renamed or removed (see [Operations](../migrations/operations.md#schema-objects)) —
and dropped with the model.

Views and materialized views exist on PostgreSQL and ClickHouse (see
[ClickHouse views](../dialects/clickhouse/schema-objects.md)); functions, sequences, row level security
and grants on PostgreSQL only. On a database without one (SQLite, and ClickHouse for the latter) a model
declaring it, and every operation on it, raises `UnSupportedError` before any SQL is sent — see the
`Features` flags in [Dialects and features](../dialects/dialects-and-features.md).

```python
from hare import fields
from hare.ddl import (
    DatabaseFunction,
    DatabaseSequence,
    FunctionVolatility,
    Grant,
    GrantTarget,
    MaterializedView,
    Policy,
    PolicyCommand,
    Privilege,
    RawSQLTerm,
    RowLevelSecurity,
    View,
)
from hare.models import Model
from hare.query.queryset import ModelQuery


class Invoice(Model):
    id = fields.IntField(primary_key=True)
    number = fields.BigIntField(null=True)
    tenant_id = fields.IntField()
    amount = fields.IntField()
    paid = fields.BooleanField(default=False)

    class Meta:
        sequences = [DatabaseSequence("invoice_number", start=1000, owned_by="number")]
        functions = [
            DatabaseFunction(
                "current_tenant",
                returns="integer",
                body=RawSQLTerm("SELECT nullif(current_setting('app.tenant', true), '')::integer"),
                language="sql",
                volatility=FunctionVolatility.STABLE,
            ),
        ]
        views = [View("paid_invoices", query=ModelQuery().filter(paid=True).values("id", "amount"))]
        materialized_views = [
            MaterializedView(
                "invoice_totals",
                query=RawSQLTerm("SELECT tenant_id, sum(amount) AS total FROM invoice GROUP BY tenant_id"),
                unique_columns=("tenant_id",),
            ),
        ]
        row_level_security = RowLevelSecurity.ENABLED
        policies = [
            Policy(
                "tenant_rows",
                command=PolicyCommand.SELECT,
                roles=("reporting",),
                using=RawSQLTerm("tenant_id = current_tenant()"),
            ),
        ]
        grants = [
            Grant(privileges=(Privilege.SELECT,), roles=("reporting",)),
            Grant((Privilege.SELECT,), ("reporting",), on=GrantTarget.VIEW, object_name="paid_invoices"),
        ]
```

Every object is created in the model's schema (`Meta.schema`), like its table. A view, materialized view,
function and sequence has a name of its own in the schema: two models declaring one of the same name (an
abstract base declaring it for its subclasses, say) raise `ConfigurationError`, as does one model
declaring a view, materialized view and sequence of one name. `Meta` takes a list or tuple of each; an
entry of another class, or two entries of one name (two equal grants), raise `ConfigurationError` when
the model is declared. A declaration checks its own arguments — type and range — and raises
`ConfigurationError` for a wrong one.

## <a id="views"></a>Views

```python
@dataclass(frozen=True)
class View:
    name: str
    query: RawSQLTerm | QuerySet | ModelQuery
    read_columns: tuple[tuple[str, str], ...] = ()  # keyword only
```

A named `SELECT`. `query` is a queryset, `ModelQuery()`, or `RawSQLTerm` of raw SQL — anything else (plain
text, a function) raises `ConfigurationError`. `ModelQuery()` is a queryset of the model declaring the
view, which isn't built yet while its `Meta` is: its calls (`ModelQuery().filter(paid=True).values("id")`,
a manager's own methods too) are recorded and made on the model's queryset once the model is built; a
queryset of another model is given as it is. A queryset is written as SQL with its values inline
(`queryset.sql(parameters_inline=True)`), for the connection it runs on: the migration state and a
migration file keep that SQL as `RawSQLTerm`, so a queryset whose SQL changes (a field renamed, the
model's table renamed) makes `makemigrations` write the view anew — removed before the rename and added
after it.

`read_columns` are the columns the view reads, `(table, column)` each. A queryset's are taken from the
query itself — its selected columns, conditions, joins and subqueries, no SQL is read — and written into
the migration file; for raw SQL, name them yourself. A migration that changes, renames or removes one of
them, or deletes its table, drops the view before and creates it again after, its declaration changed or
not — PostgreSQL refuses to change the type of a column a view reads. A raw SQL view without
`read_columns` is left alone while its declaration doesn't change.

Changing a view drops it and creates the new version, then grants the model's `Meta.grants` on it again —
a view created anew has no privileges. A view or materialized view whose `read_columns` name the changed
view is dropped before it and created again after it, through any chain of such views; one reading it
only from raw SQL without `read_columns` isn't known — remove it in the same migration first.

## <a id="materialized-views"></a>Materialized views

```python
@dataclass(frozen=True)
class MaterializedView(View):
    name: str
    query: RawSQLTerm | QuerySet | Callable[[], QuerySet]
    with_data: bool = True
    unique_columns: tuple[str, ...] = ()
```

The rows of a query, kept in the database until refreshed. `with_data=False` creates the view empty and
unreadable until its first refresh. `unique_columns` — columns of the view, not fields of the model — get
a unique index named `<view>_unique`, renamed with the view; a concurrent refresh needs it.

A refresh fills the view with the rows of its query again:

```python
await Invoice.objects.refresh_materialized_view("invoice_totals")
await Invoice.objects.refresh_materialized_view("invoice_totals", concurrently=True)
```

The refresh runs on the connection the model writes to (`.using(...)` picks another one), inside the
current transaction when there is one. `concurrently=True` keeps the view readable while it refreshes —
it needs `unique_columns` and a view filled before (`ConfigurationError` without `unique_columns`). A
view the model doesn't declare raises `QueryError`. A migration
refreshes one with [`RefreshMaterializedView`](../migrations/operations.md#schema-objects).

Changing a materialized view drops it and creates the new version — filled anew unless `with_data=False`
— and grants the model's grants on it again.

## <a id="functions"></a>Functions

```python
@dataclass(frozen=True)
class DatabaseFunction:
    name: str
    returns: str                          # "integer", "SETOF text", "TABLE (id integer, total numeric)"
    body: RawSQLTerm
    arguments: tuple[str, ...] = ()       # ("tenant_id integer", "since date")
    language: str | None = None           # None: plpgsql on PostgreSQL
    volatility: FunctionVolatility = FunctionVolatility.VOLATILE
    security_definer: bool = False
```

A function stored in the database, called from SQL — a view, a policy condition, a raw query, a column
default. `body` is `RawSQLTerm` of the function's body (plain text raises `ConfigurationError`);
`returns`, `arguments` and `body` are written into the DDL as given, so they aren't portable. The body is
dollar-quoted (`$hare_function$`); a body holding that quote raises `ConfigurationError`. `language` is a
plain name; `volatility` — `VOLATILE`, `STABLE` or `IMMUTABLE` — is what the function promises the
planner; `security_definer=True` runs it with its owner's privileges.

The function is known by its name and argument types. A change that keeps both and the result type
replaces it in place (`CREATE OR REPLACE FUNCTION`), keeping its privileges; a change of the arguments or
the result type drops it and creates the new one, granting the model's grants on it again. A function a
view or policy depends on can't be dropped that way — change those in the same migration first. A
`LANGUAGE sql` function is checked against the tables it reads when created, so it can only read tables
that exist by then: a model's functions are created after the tables of the migration, before its views
and policies.

## <a id="sequences"></a>Sequences

```python
@dataclass(frozen=True)
class DatabaseSequence:
    name: str
    start: int | None = None
    increment: int = 1
    minimum: int | None = None
    maximum: int | None = None
    cycle: bool = False
    cache: int = 1
    owned_by: str | None = None
```

A counter handing out numbers. Each number is a 64-bit integer; `increment` can't be 0 (negative counts
down), `minimum` must be below `maximum`, `start` within them, and `cache` — how many numbers a session
takes in advance — within 1..1 000 000. Without `start` the sequence starts at `minimum` counting up (1
by default) and at `maximum` counting down. `owned_by` names a field of the model: the sequence is
dropped with that column, and the field must have a column of its own.

A model's sequences are created before its table, so a column default can take from one; the owner column
is set once the table exists. The next number is read at runtime on the connection the model writes to:

```python
number = await Invoice.objects.get_next_sequence_value("invoice_number")
```

A number taken is never handed out again, even when the transaction rolls back. A sequence the model
doesn't declare raises `QueryError`.

Changing a sequence keeps its current number: the new increment, bounds, cache, cycling and owner column
apply from the next number on. `start` only sets where a restart of the sequence begins; an unset bound
or start goes back to its default.

## <a id="row-level-security"></a>Row level security

```python
class Meta:
    row_level_security = RowLevelSecurity.ENABLED   # or RowLevelSecurity.FORCED, or None (off)
```

`ENABLED` filters the table's rows for every role but its owner through its policies; a role no policy
lets in sees no rows. `FORCED` applies the policies to the owner too — the setting for an application
connecting as the role that owns its tables. `None` — the default — turns it off. `ENABLED` without any
policy closes the table to every other role, which then reaches its rows only through a
`security_definer` function. `Meta.policies` with `row_level_security` off raises `ConfigurationError`:
the database would keep the policies without applying them.

```python
@dataclass(frozen=True)
class Policy:
    name: str
    command: PolicyCommand = PolicyCommand.ALL     # ALL, SELECT, INSERT, UPDATE, DELETE
    roles: tuple[str, ...] = ()                    # empty: every role
    using: Q | RawSQLTerm | TenantCondition | None = None
    with_check: Q | RawSQLTerm | TenantCondition | None = None
    permissive: bool = True
```

`using` is the condition a row a statement reads, updates or deletes has to meet; `with_check` the
condition a row it inserts or updates has to meet — without it an `ALL` or `UPDATE` policy checks
`using`. Each is a `Q` over the model's own fields, its values written inline, or `RawSQLTerm` of raw SQL
(calling a function, reading a setting), or `TenantCondition()` — the row's `Meta.tenant_field` is one of
the transaction's tenants
([Multi-tenancy](../soft-delete-versions-tenants/multi-tenancy.md#row-level-security)). A policy needs at
least one of them; an `INSERT` policy takes no `using`, a `SELECT` or `DELETE` one no `with_check` —
`ConfigurationError` otherwise. A row passes when any permissive policy and every restrictive one
(`permissive=False`) of the command lets it. `PUBLIC`, `CURRENT_USER`, `CURRENT_ROLE` and `SESSION_USER`
in `roles` are written as the keywords; every other role is quoted.

A row `using` hides is skipped without an error: a query doesn't read it, an `update()`/`delete()`
doesn't count it (`0` rows). A row a `with_check` refuses (an insert, or an update writing values outside
it) raises `OperationalError` — PostgreSQL's `insufficient_privilege` (SQLSTATE `42501`,
`new row violates row-level security policy for table "invoice"`), as a missing `GRANT` does.

A policy's name is per table. A change of its roles or conditions alters it in place; a new command or
type, or a condition taken away, drops it and creates the new one.

## <a id="grants"></a>Grants

```python
@dataclass(frozen=True)
class Grant:
    privileges: tuple[Privilege, ...]
    roles: tuple[str, ...]
    on: GrantTarget = GrantTarget.TABLE      # TABLE, VIEW, MATERIALIZED_VIEW, SEQUENCE, FUNCTION
    object_name: str | None = None           # the name of the view, sequence or function of the model
    columns: tuple[str, ...] = ()            # fields - on the table only
    with_grant_option: bool = False
```

Privileges the roles get — on the model's table, or on a view, materialized view, sequence or function
the model declares, named by `object_name`. The privileges have to exist on the type of object:

| `on` | Privileges |
|---|---|
| `TABLE`, `VIEW` | `SELECT`, `INSERT`, `UPDATE`, `DELETE`, `TRUNCATE`, `REFERENCES`, `TRIGGER`, `ALL` |
| `MATERIALIZED_VIEW` | `SELECT`, `ALL` |
| `SEQUENCE` | `USAGE`, `SELECT`, `UPDATE`, `ALL` |
| `FUNCTION` | `EXECUTE`, `ALL` |

`columns` limits `SELECT`, `INSERT`, `UPDATE` and `REFERENCES` on the table to some fields' columns.
`with_grant_option=True` lets the roles grant the privileges on. A grant has no name — it is known by
everything it grants: a changed grant is revoked and the new one granted. Removing a grant revokes its
privileges from its roles, which takes them away even when another grant of the model gives the same
ones. An object a grant names that the model doesn't declare raises `ConfigurationError` when the grant
runs. A role has to exist on the server before the grant runs — hare creates no roles.

## <a id="in-migrations"></a>In migrations

`makemigrations` compares each type by name: an object only renamed is renamed, one changed under its
name is altered (`AlterView`, `AlterFunction`, `AlterSequence`, `AlterPolicy`, ...) — or, when the
migration changes a column or a table its raw SQL may read, removed before that change and added after
it — the others are removed and added; grants are revoked and granted. An object left as declared is left
alone, whatever its raw SQL reads. The operations run in an order where each object finds
what it uses:

- First, before any field or table changes: grants revoked, policies, materialized views and views
  dropped, then renames.
- Then the model, field, index and constraint operations. A new model's `CreateModel` creates its
  sequences before the table; its other objects come next.
- After every table is in place: sequences, functions, views, materialized views, row level security,
  policies and grants created or changed — each type after the types it may use. A view or a
  materialized view whose `read_columns` name another one comes after it (a view reading a materialized
  view); raw SQL is never read for it, so name the columns read there.
- Last: functions and sequences dropped.

A view, materialized view or policy whose SQL names a field or column the migration removes or changes is
dropped before the field operations and created again after them, even when it is otherwise unchanged —
PostgreSQL won't drop or change a column one depends on. hare knows the columns a view reads from its
queryset or `read_columns`, those a policy reads from its `Q` conditions and `TenantCondition()`, and those
a trigger reads from its `Q` `when` condition (a trigger reading a changed column is dropped and created
again the same way); a
raw SQL query without `read_columns` or a raw SQL condition is left as written — change it in the same
migration.

Creating a view, materialized view, function or sequence grants the model's grants on it at once, so an
object dropped and created again keeps its privileges. Dropping a model drops its views and materialized
views before its table, then its functions and sequences; its policies and table privileges go with the
table. A table PostgreSQL rebuilds (a changed partitioning) gets its views, row level security, policies
and grants back.

`hare drift` reads each object a model declares back from the database and from its
[shadow](../migrations/drift.md#shadow) alike, as PostgreSQL prints it — so a declaration written in other
words but the same is no drift:

- a view or materialized view by its query (`pg_get_viewdef()`) — and a materialized view's unique index
  over its `unique_columns`;
- a function by the arguments, result type, body, language, volatility and `security_definer` of each of
  its overloads;
- a sequence by its start, increment, bounds, cache, cycling and owner column;
- row level security as enabled, forced or off — whether or not the model declares it;
- a policy by its type, command, roles and conditions;
- a grant by every privilege of every role, with its grant option.

An object the database lacks comes back as its `Add...` operation, a changed one as its `Alter...`. An
object no model declares — a function or a view made by hand — isn't reported.
