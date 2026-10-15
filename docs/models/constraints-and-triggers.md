# Constraints and triggers

The constraints a model declares in `Meta.constraints` and the triggers in `Meta.triggers` — created with
the table by `generate_schemas()` and by migrations alike, changed and dropped with their declarations.

```python
from hare.ddl import (
    CheckConstraint, ExclusionConstraint, ExclusionConstraintUsing, RawSQLTerm, Trigger, TriggerEvent,
    TriggerForEach, TriggerTiming, UniqueConstraint,
)
```

## <a id="constraints"></a>Constraints

The two constraint classes of `hare.ddl`, declared in `Meta.constraints`:

```python
@dataclass(frozen=True)
class UniqueConstraint:
    fields: tuple[str, ...]
    name: str | None = None
    condition: Q | RawSQLTerm | None = None  # a Q or raw SQL — a partial unique index (PostgreSQL and SQLite)
    deferrable: bool = False           # PostgreSQL only; not together with condition
    initially_deferred: bool = False   # requires deferrable=True
    include: tuple[str, ...] = ()      # non-key INCLUDE columns (PostgreSQL; SQLite creates the index without them)
    nulls_distinct: bool | None = None # PostgreSQL 15+: False = NULLS NOT DISTINCT
    without_overlaps: bool = False     # PostgreSQL 18+: the last field a range rows may not overlap in

@dataclass(frozen=True)
class CheckConstraint:
    check: Q | RawSQLTerm   # a Q over the model's own fields, or raw SQL
    name: str
```

```python
class Book(Model):
    ...
    class Meta:
        constraints = (
            CheckConstraint(name="book_valid", check=Q(pages__gt=0) & ~Q(title="")),
            UniqueConstraint(fields=("isbn",), name="uq_book_isbn_in_print", condition=Q(in_print=True)),
        )
```

### <a id="unique-constraint"></a>`UniqueConstraint`

`fields` names one field or more of the model's own columns — an empty tuple, a name that isn't a field
or a `ManyToManyField` raise `ConfigurationError`. Without `name`, one is generated.

`nulls_distinct=False` makes rows whose fields are equal or NULL collide — `(NULL, 1)` twice violates
`UniqueConstraint(fields=("shelf", "position"), nulls_distinct=False)`; `True` states the default. It is
PostgreSQL-only (15+): a model declaring it on another database raises `ConfigurationError` when it is
bound to the connection (`Hare.init()`, `makemigrations`), before a migration no `migrate` could apply is
written. `include` stores more columns in the constraint's index, so a query reading only them and the
key is answered from the index alone.

`without_overlaps=True` makes the last field a range that rows equal in the other fields may not overlap
in; it needs two fields or more and no `condition` (`ConfigurationError`). `deferrable=True` with a
`condition` raises `ConfigurationError` too, and on SQLite `deferrable=True` raises `UnSupportedError`
when the table is created.

### <a id="conditions"></a>Conditions: `Q` and `RawSQLTerm`

A condition — of a `UniqueConstraint`, a `CheckConstraint`, a `PartialIndex` or an `ExclusionConstraint`
— is a `Q` over the model's own fields or raw SQL wrapped in `RawSQLTerm(...)`, written into the DDL as
it is. Anything else (a plain string, a dict) raises `ConfigurationError`. A `Q` is rendered for the
database the DDL runs on, its values inline:

```python
class Book(Model):
    ...
    class Meta:
        constraints = (CheckConstraint(name="book_priced_or_free", check=RawSQLTerm("price IS NOT NULL OR is_free")),)
        indexes = (PartialIndex(fields=("title",), condition=Q(price__gt=Decimal("1")) | Q(pages__lt=3)),)
```

Every lookup and `F()` of the model's own fields works; a lookup crossing a relation, an aggregate,
`Exists(...)` and an empty `Q()` raise `ConfigurationError`. Migration files hold the `Q` itself:
`RenameField` renames the field inside it, and `RemoveField` drops a `CheckConstraint` whose `Q` reads
only the removed field (one also reading another field raises, as for raw SQL).

On SQLite:

- a text lookup (`contains`, `startswith`, `iendswith`, ...) is written as `GLOB`, which matches the same
  rows case-sensitively: SQLite refuses its `LIKE` in such a condition under `case_sensitive_like`; a
  pattern read from another column raises `UnSupportedError`;
- a `Decimal` compares as a number;
- a lookup SQLite only has through hare's own functions (`JSONField` lookups, regular expressions, ...)
  raises `UnSupportedError` — such a function doesn't exist outside hare's connections (see [In
  constraints and indexes](../dialects/sqlite/functions.md#in-constraints-and-indexes)).

### <a id="exclusion-constraint"></a>`ExclusionConstraint` (PostgreSQL)

`ExclusionConstraint` is `EXCLUDE USING <method> (...)`: it rejects a row whose combination of values
overlaps an existing one, without a check-then-act query before every write.

```python
class ExclusionConstraintUsing(StrEnum):
    GIST = "gist"; SPGIST = "spgist"; BTREE = "btree"; HASH = "hash"

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

The usual use is periods that may not overlap — a book can't be lent twice at once:

```python
from hare.dialects.postgresql.fields import DateTimeRangeField


class Loan(Model):
    book = fields.ForeignKeyField("models.Book")
    during = DateTimeRangeField()

    class Meta:
        constraints = (
            ExclusionConstraint(
                name="no_overlapping_loans",
                expressions=(("book", "="), ("during", "&&")),
                using=ExclusionConstraintUsing.GIST,
            ),
        )
```

A deferrable exclusion constraint lets a transaction pass through an overlapping state — moving two loans
past each other, say — as long as none overlap when it commits.

GiST has no operator class of its own for a plain scalar column (the `book` key above, a number, text, a
date, a uuid, ...) — that comes from the `btree_gist` extension. A GiST `ExclusionConstraint` naming such
a field needs it, so hare creates it the way it creates `citext` for a `CitextField`:
`generate_schemas()` runs `CREATE EXTENSION btree_gist`, and the migration autodetector adds a
`CreateExtension("btree_gist")` — no `Meta.extensions` entry needed. A `RawSQLTerm` expression's type
isn't known, so for one over a scalar value add `"btree_gist"` to `Meta.extensions` yourself. A `HASH`
constraint takes exactly one expression with `=` and no `include` — a hash index has a single key and no
`INCLUDE` columns; another declaration raises `ConfigurationError`.

An expression entry can also be a `RawSQLTerm` instead of a field name, for a computed expression the
model's own fields don't cover directly:

```python
ExclusionConstraint(
    name="no_overlapping_reader_loans",
    expressions=((RawSQLTerm("lower(reader_email)"), "="), ("during", "&&")),
)
```

> [!WARNING]
> `ExclusionConstraint.expressions` can't reference a column on a joined table — a raw expression is
> limited to the model's own table. A rule needing a joined column is enforced in application code
> instead (inside a transaction, with row locking).

## <a id="triggers"></a>Triggers

```python
class TriggerEvent(StrEnum): INSERT = "INSERT"; UPDATE = "UPDATE"; DELETE = "DELETE"
class TriggerTiming(StrEnum): BEFORE = "BEFORE"; AFTER = "AFTER"; INSTEAD_OF = "INSTEAD OF"  # PostgreSQL, views only
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
    deferrable: bool = False           # PostgreSQL CONSTRAINT TRIGGER
    initially_deferred: bool = False   # requires deferrable=True
    enabled: bool = True               # False: kept but not fired (PostgreSQL)
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

A trigger of `Meta.triggers` fires on the model's table, so `INSTEAD_OF` — which the database fires on a
view only — raises `ConfigurationError` when the model is defined; a `Meta.managed = False` model over a
view may declare one.

### <a id="trigger-body-and-when"></a>`body` and `when`

`body` and a raw `when` are `RawSQLTerm(...)`, as a raw condition of a constraint or an index is; plain
text is refused with `ConfigurationError`. `when` can be a `Q` over the model's own fields instead: it
compares the row the trigger fires for — `NEW`, or `OLD` of a trigger on `DELETE` alone — with its values
written into the DDL, and it follows `RenameField`/`RemoveField` like a constraint's condition (a field a
trigger's condition names can't be removed). A `Q` condition needs a row trigger, and one event row: a
`STATEMENT` trigger, or a trigger on `DELETE` together with another event, takes `RawSQLTerm(...)`
instead.

```python
Trigger(
    name="trg_book_stock_check",
    on=TriggerEvent.UPDATE,
    timing=TriggerTiming.BEFORE,
    when=Q(stock__lt=0),
    body=RawSQLTerm("RAISE EXCEPTION 'stock can''t go negative'; RETURN NEW;"),
)
```

`inspectdb` and `drift` read a SQLite trigger's `WHEN` back written with or without parentheses around it
(`WHEN OLD.price <> NEW.price`, `WHEN (OLD.price <> NEW.price)`).

### <a id="deferrable-triggers"></a>Deferrable triggers (PostgreSQL)

`deferrable=True` generates a PostgreSQL `CREATE CONSTRAINT TRIGGER` instead of a plain `CREATE TRIGGER`
— its check can be postponed to the end of the transaction (`SET CONSTRAINTS ... DEFERRED`) rather than
firing right after each row. `initially_deferred=True` makes that the default for every transaction,
without an explicit `SET CONSTRAINTS`. Both require `timing=TriggerTiming.AFTER` and
`for_each=TriggerForEach.ROW` — a constraint trigger can't be `BEFORE` or statement-level
(`ConfigurationError`) — and SQLite raises `UnSupportedError` for `deferrable=True`, since it has no such
concept.

### <a id="disabled-triggers"></a>A disabled trigger (PostgreSQL)

`enabled=False` creates the trigger and disables it (`ALTER TABLE ... DISABLE TRIGGER`) — it stays in the
database, firing for nothing; SQLite, which fires every trigger it has, raises `UnSupportedError`.
