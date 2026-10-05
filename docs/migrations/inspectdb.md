# Models from an existing database

`hare inspectdb [tables...] [--connection ALIAS] [--schema SCHEMA]` reads the tables of an existing
database — PostgreSQL, SQLite or ClickHouse — and prints a module of models for them: fields, relations,
indexes, constraints and table options, as `makemigrations` would write them. The same is available from
code ([below](#generating-model-source-programmatically)).

```bash
hare inspectdb --connection default --schema public books authors > library/models.py
```

## <a id="reconstruction-rules"></a>What is reconstructed

`inspectdb` raises rather than silently mis-generating a model when it hits a name collision it can't
represent: two tables that differ only by case would otherwise produce two identical Python class names
(one silently shadowing the other), and an FK column whose stripped attribute name (`author_id` →
`author`) collides with an unrelated plain column would otherwise drop one of the two fields. Both fail
loudly instead. It also reconstructs a composite foreign key as a single `ForeignKeyField` (not one field
per column) and an index built on a SQL expression rather than a plain column list — both round-trip back
into real model source, not a `# TODO` comment. A composite foreign key is a relation when it references
the target's whole primary key under the column names a composite relation has,
`<field>_<target key column>` (`owner_a`, `owner_b` for a target keyed by `a`, `b`); one under other
names (`a`, `b`) stays plain fields with a `# TODO` naming it — a composite relation names its columns
after the field alone, so the columns have to be renamed to declare it. A table whose name holds a dot,
which no `Meta.table` holds, is skipped with a comment saying so.

A foreign key declared `ON DELETE SET DEFAULT` comes back as `on_delete=SET_DEFAULT` with the column's
`DEFAULT` as `db_default` (a `ForeignKeyField` with a real constraint requires one). If the column has no
`DEFAULT`, the database really resets it to `NULL` (or fails the delete on a `NOT NULL` column), so
`inspectdb` renders `SET_NULL` (or `NO_ACTION`) with a `# TODO` comment instead of a `SET_DEFAULT` field
that couldn't be imported. `ON DELETE SET NULL` of a `NOT NULL` column, which fails the delete in the
database, comes back as `NO_ACTION` with a `# TODO` likewise.

A PostgreSQL `ON DELETE NO ACTION DEFERRABLE INITIALLY IMMEDIATE` — the constraint `on_delete=PROTECT`
creates — comes back as `PROTECT`. What no relation declares — an `ON UPDATE` action, another deferral
(`DEFERRABLE INITIALLY DEFERRED`) — is named in a `# TODO`: a migration of the model creates the
constraint without it, and `drift` reports it (SQLite reports the `ON UPDATE` action alone, not the
deferral).

Other reconstruction rules:

- An FK column that is also the table's primary key comes back as `OneToOneField(..., primary_key=True)`;
  an FK column without the `_id` suffix (`owner_code`) gets `source_field="owner_code"`.
- When a table has two or more relations to the same target (including a self-referencing pair), each
  gets `related_name="<table>_<field>_set"` so their backward relations don't collide.
- A partial index keeps its `WHERE` clause as `PartialIndex(condition=RawSQLTerm(...))`, the predicate as
  the database reports it, on both PostgreSQL and SQLite. An expression index (`lower(note)`) is rebuilt
  as `Index(RawSQLTerm(...))` on both dialects — a key without the parentheses SQLite keeps around it
  (`(lower(note))`) — and a composite foreign key is rebuilt on SQLite too.
- An index or constraint keeps its database name (`Index(..., name="book_published_idx")`) unless it is
  the one hare generates for the declaration or the one PostgreSQL gives an unnamed index or `UNIQUE`
  constraint (`<table>_<columns>_idx`/`_key`); a plain single-column index under a name of its own
  becomes a named `Meta.indexes` entry instead of `db_index=True`.
- A unique index comes back as the declaration its name belongs to: an unnamed
  `UniqueConstraint(fields=...)` under the name hare generates for one (or the one PostgreSQL gives an
  unnamed `UNIQUE`), `Index(unique=True)` under the name hare generates for that, and a named
  `UniqueConstraint` otherwise — with its `condition=` (a partial unique index, predicate as raw SQL) and
  `deferrable=`/`initially_deferred=` (a single-column `DEFERRABLE UNIQUE` included).
- A `CHECK` predicate comes back without the pair of parentheses PostgreSQL wraps it in
  (`CheckConstraint(check=RawSQLTerm("age >= 0"))`).
- An index comes back with its `INCLUDE (...)` columns (`include=`), each key's `DESC`/`NULLS FIRST`
  order (`"-a"`, `F("a").desc(nulls_last=True)`, an expression key as
  `OrderedIndexKey(RawSQLTerm("lower(c)"), Order.DESC_NULLS_FIRST)`), a collated key as an expression
  (`RawSQLTerm('d COLLATE "C"')`) and `NULLS NOT DISTINCT` (`UniqueConstraint(nulls_distinct=False)`).
  What no hare index can declare — the sort order of a collated key on SQLite — is left out and listed in
  a `# TODO`.
- A foreign key to a table of another schema than the inspected one stays a plain column with a `# TODO`
  naming its target — a relation can only point at a model of the same run.
- A partition of a partitioned PostgreSQL table is not listed (the partitioned table stands for it),
  unless named explicitly; `drift` doesn't report partitions as untracked tables either.
- A view or a materialized view is no model: `inspectdb` of a whole schema names each one in a `# NOTE`
  at the end of the module — to be declared in `Meta.views`/`Meta.materialized_views` of the model it
  reads — with the triggers on it (`INSTEAD OF`), which a `RunSQL` migration adds.
- A PostgreSQL identity column (`GENERATED ... AS IDENTITY`, not the primary key) becomes an integer
  field with `generated=True` and a `# TODO`: hare never writes it and reads the assigned value back, but
  has no identity-column DDL of its own. A quoted numeric default (`'-1.5'::double precision`) comes back
  as a number.
- A generated column becomes a `GeneratedField` of its expression — except a stored PostgreSQL `tsvector`
  whose expression is the one a `TSVectorField` writes, which comes back as that `TSVectorField` with its
  `source_fields`, `config` and `weights`, so `__search` keeps its configuration.
- A `UNIQUE`/indexed column whose field type can't be indexed (`JSONField`) loses the index with a
  `# TODO`, instead of producing a model that can't be imported.
- A single-column `UNIQUE` foreign key comes back as `OneToOneField`.
- A foreign key's own index (a plain index over exactly its key columns) isn't written out —
  `ForeignKeyField` [has it by default](../models/relations.md#index-on-the-key-column); a foreign key
  without one, and without another index starting with its columns, gets `db_index=False`.
- An array column's default (`'{1,2}'::integer[]`) comes back as the list (`db_default=[1, 2]`), its
  elements read by the array's element type.
- A column of a type no hare field writes exactly (`real`, `json`, an unconstrained `numeric`, SQLite's
  `BOOLEAN`/`NUMERIC`) comes back as the nearest field with a `# TODO` naming both types: `drift` reports
  the difference until the column is altered to the field's type.
- A column of a PostgreSQL `ENUM` type — an array of one too — comes back as
  [`NativeEnumField`](../dialects/postgresql/fields.md) of an enum the module declares of the type's
  labels, in their order (`Mood = StrEnum('Mood', {'SAD': 'sad', 'OK': 'ok'})`, a label that isn't a name
  made one: `so-so` → `SO_SO`, `1st` → `VALUE_1ST`), with `type_name=` and `description=''` — the field
  would describe its members in a comment the column hasn't. Rename the enum and its members as you like;
  the labels are its values. Another user-defined type (a composite one) comes back as a `TextField` with
  a `# TODO` naming it.
- A ClickHouse `Enum8`/`Enum16` column comes back as the enum field that writes the same type, of an enum
  the module declares, named after the column (`StatusLabels`), with `description=''`: a `CharEnumField`
  of a `StrEnum` when its labels carry the numbers a `CharEnumField` gives its values (a table hare
  created), an `IntEnumField` of an `IntEnum` of the labels and their numbers when every label is a name
  (`Enum8('LOW' = 1, 'HIGH' = 2)` → `IntEnum('LevelLabels', {'LOW': 1, 'HIGH': 2})`). Any other enum —
  labels that aren't names, or an `Enum16` whose numbers an `Enum8` holds — comes back as a `CharField`
  of its labels with a `# TODO`.
- PostgreSQL `timestamp` and `time` without a time zone, `interval` and `character(n)` come back as
  [`NaiveDatetimeField`, `NaiveTimeField`, `IntervalField` and
  `FixedCharField(max_length=n)`](../dialects/postgresql/fields.md#wall-clock-fields); ClickHouse `Date`,
  `DateTime` and a `DateTime64` other than `DatetimeField`'s `DateTime64(6, 'UTC')` as
  [`ClickhouseDateField` and `ClickhouseDatetimeField`](../dialects/clickhouse/types.md#fields) of the
  column's precision and time zone.
- A column of a type hare has no field for — PostgreSQL `bit`, `money`, `xml`, SQLite `ANY` of a `STRICT`
  table — comes back as a `TextField` with a `# TODO` naming the type.
- A PostgreSQL default of the moment of the statement (`statement_timestamp()`) comes back as `Now()`,
  which is written so; `now()` and `CURRENT_TIMESTAMP` — the start of the transaction — stay
  `SqlDefault("now()")`, so a migration keeps the column's time.
- A table without a primary key whose only key is one composite foreign key under a `UNIQUE` is a model
  of its own — a `OneToOneField` and `Meta.primary_key = None` — not a many-to-many through table, which
  joins two sides.
- A JSON column's default (`'{}'::jsonb`) comes back as the document itself (`db_default={}`), and a
  SQLite expression default (`DEFAULT (datetime('now'))`) keeps its parentheses:
  `SqlDefault("(datetime('now'))")`.
- A column or table whose Python name would shadow one of the generated module's imports (a column
  `fields`, a table `model`) gets a trailing underscore (`fields_` with `source_field="fields"`, class
  `Model_` with `Meta.table = "model"`), and relations point at the renamed class.

## <a id="generating-model-source-programmatically"></a>Generating model source programmatically

`hare inspectdb` prints what `hare.inspectdb.SchemaInspector` returns:

```python
from hare.inspectdb import SchemaInspector

source = await SchemaInspector.inspect(connection, ["books", "authors"], schema="public")
```

```python
class SchemaInspector:
    @staticmethod
    async def inspect(connection: DatabaseClient, tables: list[str] | None = None, schema: str | None = None) -> str: ...

    @staticmethod
    def render_module(
        table_infos: list[TableInfo], dialect: str, views: list[tuple[str, bool, tuple[str, ...]]] | None = None
    ) -> str: ...
```

`inspect()` generates one importable module for the tables (every table of `schema` when `tables` is
omitted; `schema` is the connection's default schema when `None`, and is ignored by a dialect without
schemas). It raises `UnsupportedDialectError` for a dialect without an introspector,
`SchemaNotFoundError`/`TableNotFoundError` for a missing schema or table and
`DuplicateModelClassNameError` for two tables deriving one class name. `render_module()` renders tables
already read with `DatabaseCatalog` (see [Reading a table:
`DatabaseCatalog`](../models/runtime-models.md#reading-a-table)); `views` — each view's name, whether it
is materialized and its triggers — are the views it names in the `# NOTE` at the end.

Each table is first turned into the same model state a declared model has
(`hare.inspectdb.InspectedModelBuilder(table_info, ModelGenerationOptions(dialect, app_label)).build()`
returns an `InspectedModel` with `state` — a migrations `ModelState` of real field, index and constraint
objects — plus the `# TODO` notes and the `Meta` layout). The source is that state written the way a
migration file writes it — every field, index and constraint as its `deconstruct()` call — and
`ModelFactory` builds a class from the very same state, so the generated source and a class built at
runtime never disagree.
