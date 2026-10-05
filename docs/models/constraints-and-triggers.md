# Constraints and triggers

The constraints a model declares in `Meta.constraints` and the triggers in `Meta.triggers` — created
with the table by `generate_schemas()` and by migrations alike.

```python
from hare.ddl import (
    CheckConstraint, ExclusionConstraint, ExclusionConstraintUsing, RawSQLTerm, Trigger, TriggerEvent,
    TriggerForEach, TriggerTiming, UniqueConstraint,
)
```

## <a id="constraints"></a>Constraints

```python
@dataclass(frozen=True)
class UniqueConstraint:
    fields: tuple[str, ...]
    name: str | None = None
    condition: Q | RawSQLTerm | None = None  # a Q or raw SQL — a partial unique index (Postgres and SQLite)
    deferrable: bool = False           # Postgres only; not together with condition
    initially_deferred: bool = False   # requires deferrable=True
    include: tuple[str, ...] = ()      # non-key INCLUDE columns (Postgres; SQLite creates the index without them)
    nulls_distinct: bool | None = None # Postgres 15+: False = NULLS NOT DISTINCT
    without_overlaps: bool = False     # Postgres 18+: the last field a range rows may not overlap in

@dataclass(frozen=True)
class CheckConstraint:
    check: Q | RawSQLTerm   # a Q over the model's own fields, or raw SQL
    name: str
```

```python
class Membership(Model):
    ...
    class Meta:
        constraints = (
            CheckConstraint(name="check_membership_scope", check=RawSQLTerm("team_id IS NOT NULL OR event_id IS NOT NULL")),
            UniqueConstraint(fields=("team_id",), name="uq_membership_open_team", condition=RawSQLTerm("status = 'open'")),
        )
```

`nulls_distinct=False` makes rows whose fields are equal or NULL collide — `(NULL, 1)` twice violates
`UniqueConstraint(fields=("shelf", "pages"), nulls_distinct=False)`; `True` states the default. It is
PostgreSQL-only: SQLite raises `UnSupportedError` for it. `include` stores more columns in the
constraint's index, so a query reading only them and the key is answered from the index alone.

A condition is a `Q` over the model's own fields, as in Django, or raw SQL wrapped in
`RawSQLTerm(...)` (`from hare.ddl import RawSQLTerm`) — written into the DDL as it is. Anything else (a
plain string, a dict) raises `ConfigurationError`. A `Q` is rendered for the database the DDL runs on,
its values inline:

```python
class Item(Model):
    ...
    class Meta:
        constraints = (
            CheckConstraint(name="item_valid", check=Q(qty__gte=0) & ~Q(name="")),
            UniqueConstraint(fields=("sku",), name="uq_item_active_sku", condition=Q(active=True)),
        )
        indexes = (PartialIndex(fields=("name",), condition=Q(price__gt=Decimal("1")) | Q(qty__lt=3)),)
```

Every lookup and `F()` of the model's own fields works; a lookup crossing a relation, an aggregate,
`Exists(...)` and an empty `Q()` raise `ConfigurationError`. Migration files hold the `Q` itself:
`RenameField` renames the field inside it, and `RemoveField` drops a `CheckConstraint` whose `Q` reads
only the removed field (one also reading another field raises, as for raw SQL). On SQLite a `Decimal`
compares as a number there, and a lookup SQLite only has through hare's own functions (`JSONField`
lookups, regular expressions, ...) raises `ConfigurationError` — such a function doesn't exist
outside hare's connections. `ExclusionConstraint.condition` takes a `Q` as well.

PostgreSQL also gets `ExclusionConstraint` — `EXCLUDE USING <method> (...)`, the database-native way
to reject a row whose combination of values overlaps an existing one, instead of a manual
check-then-act query before every write. The canonical use is overlap-free booking:

```python
class ExclusionConstraintUsing(StrEnum):
    GIST = "gist"; SPGIST = "spgist"; BTREE = "btree"

@dataclass(frozen=True)
class ExclusionConstraint:
    name: str
    expressions: tuple[tuple[str | RawSQLTerm, str], ...]  # (field_name_or_raw_expression, operator) pairs
    using: ExclusionConstraintUsing = ExclusionConstraintUsing.GIST
    condition: Q | RawSQLTerm | None = None
    include: tuple[str, ...] = ()        # non-key INCLUDE columns of the constraint's index
    deferrable: bool = False             # checked at commit (or after SET CONSTRAINTS ... DEFERRED)
    initially_deferred: bool = False     # requires deferrable=True
```

A deferrable exclusion constraint lets a transaction pass through an overlapping state — moving two
bookings past each other, say — as long as none overlap when it commits.

```python
class Event(Model):
    team = fields.ForeignKeyField("models.Team")
    during = DateTimeRangeField()  # hare.dialects.postgresql.fields.ranges

    class Meta:
        constraints = (
            ExclusionConstraint(
                name="no_overlapping_events",
                expressions=(("team", "="), ("during", "&&")),
                using=ExclusionConstraintUsing.GIST,
            ),
        )
```

GiST has no operator class of its own for a plain scalar column (the `team` key above, a number,
text, a date, a uuid, ...) — that comes from the `btree_gist` extension. A GiST `ExclusionConstraint`
naming such a field needs it, so hare creates it the way it creates `citext` for a `CitextField`:
`generate_schemas()` runs `CREATE EXTENSION btree_gist`, and the migration autodetector adds a
`CreateExtension("btree_gist")` — no `Meta.extensions` entry needed. A `RawSQLTerm` expression's type
isn't known, so for one over a scalar value add `"btree_gist"` to `Meta.extensions` yourself.

An expression entry can also be a raw `RawSQLTerm` instead of a field name, for a computed
expression the model's own fields don't cover directly:

```python
ExclusionConstraint(
    name="no_overlapping_events_ci",
    expressions=((RawSQLTerm("lower(team_name)"), "="), ("during", "&&")),
)
```

> [!WARNING]
> `ExclusionConstraint.expressions` can't reference a column on a joined table — a raw
> expression is limited to the model's own table. If your "overlap" rule needs a joined column,
> it isn't a drop-in fit; enforce it in application code instead (inside a transaction, with row
> locking).

## <a id="triggers"></a>Triggers

```python
class TriggerEvent(StrEnum): INSERT = "INSERT"; UPDATE = "UPDATE"; DELETE = "DELETE"
class TriggerTiming(StrEnum): BEFORE = "BEFORE"; AFTER = "AFTER"; INSTEAD_OF = "INSTEAD OF"  # Postgres, views only
class TriggerForEach(StrEnum): ROW = "ROW"; STATEMENT = "STATEMENT"  # SQLite: ROW only

@dataclass(frozen=True)
class Trigger:
    name: str
    on: str              # a TriggerEvent member, or raw text like "INSERT OR UPDATE OF parent_id"
    body: RawSQLTerm      # the full procedural body
    timing: TriggerTiming = TriggerTiming.AFTER
    for_each: TriggerForEach = TriggerForEach.ROW
    when: Q | RawSQLTerm | None = None   # the WHEN condition
    language: str | None = None          # the dialect's default (PostgreSQL: plpgsql)
    deferrable: bool = False           # Postgres CONSTRAINT TRIGGER
    initially_deferred: bool = False   # requires deferrable=True
```

`deferrable=True` generates a PostgreSQL `CREATE CONSTRAINT TRIGGER` instead of a plain `CREATE
TRIGGER` — its check can be postponed to the end of the transaction (`SET CONSTRAINTS ... DEFERRED`)
rather than firing immediately after each row. `initially_deferred=True` makes that the default for
every transaction, without an explicit `SET CONSTRAINTS`. Both require `timing=TriggerTiming.AFTER`
and `for_each=TriggerForEach.ROW` — a constraint trigger can't be `BEFORE` or statement-level — and
SQLite raises `UnSupportedError` for `deferrable=True`, since it has no such concept.

`body` and a raw `when` are `RawSQLTerm(...)`, as a raw condition of a constraint or an index is;
plain text is refused with `ConfigurationError`. `when` can be a `Q` over the model's own fields
instead: it compares the row the trigger fires for — `NEW`, or `OLD` of a trigger on `DELETE` alone —
with its values written into the DDL, and it follows `RenameField`/`RemoveField` like a constraint's
condition (a field a trigger's condition names can't be removed). A `Q` condition needs a row
trigger, and one event row: a `STATEMENT` trigger, or a trigger on `DELETE` together with another
event, takes `RawSQLTerm(...)` instead.

```python
Trigger(
    name="trg_stock_check",
    on=TriggerEvent.UPDATE,
    timing=TriggerTiming.BEFORE,
    when=Q(quantity__lt=0),
    body=RawSQLTerm("RAISE EXCEPTION 'stock can''t go negative'; RETURN NEW;"),
)
```

```python
class Category(Model):
    ...
    class Meta:
        triggers = (
            Trigger(
                name="trg_category_depth",
                on=TriggerEvent.INSERT,
                timing=TriggerTiming.BEFORE,
                body=RawSQLTerm("""
                    IF NEW.parent_id IS NULL THEN
                        NEW.depth := 1;
                    ELSE
                        SELECT depth + 1 INTO NEW.depth FROM "category" WHERE id = NEW.parent_id;
                    END IF;
                    RETURN NEW;
                """),
            ),
        )
```

On PostgreSQL, both a `CREATE FUNCTION ... RETURNS TRIGGER` and the `CREATE TRIGGER` attaching it are
generated together — a trigger has no way to attach without a function to call. On SQLite, `body` is
embedded directly inside `CREATE TRIGGER ... BEGIN ... END`, with no separate function object.

**Both `constraints` and `triggers` are applied automatically when a migration's `CreateModel`
operation runs** — they don't need a hand-written `RunSQL` alongside them.
