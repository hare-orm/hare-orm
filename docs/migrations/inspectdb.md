# Models from an existing database

`hare inspectdb [tables...] [--connection ALIAS] [--schema SCHEMA]` reads the tables of an existing
database — PostgreSQL, SQLite or ClickHouse — and prints a module of models for them: fields,
relations, indexes, constraints and table options, as `makemigrations` would write them. The same
is available from code ([below](#generating-model-source-programmatically)).

```bash
hare inspectdb --connection default --schema public users orders > app/models.py
```

## <a id="reconstruction-rules"></a>What is reconstructed

`inspectdb` raises rather than silently mis-generating a model when it hits a name collision it
can't represent: two tables that differ only by case would otherwise produce two identical Python
class names (one silently shadowing the other), and an FK column whose stripped attribute name
(`event_id` → `event`) collides with an unrelated plain column would otherwise drop one of the two
fields. Both fail loudly instead. It also reconstructs a composite foreign key as a single
`ForeignKeyField` (not one field per column) and an index built on a SQL expression rather than a
plain column list — both round-trip back into real model source, not a `# TODO` comment.

A foreign key declared `ON DELETE SET DEFAULT` comes back as `on_delete=SET_DEFAULT` with the
column's `DEFAULT` as `db_default` (a `ForeignKeyField` with a real constraint requires one). If the
column has no `DEFAULT`, the database really resets it to `NULL` (or fails the delete on a `NOT
NULL` column), so `inspectdb` renders `SET_NULL` (or `NO_ACTION`) with a `# TODO` comment instead of
a `SET_DEFAULT` field that couldn't be imported.

Other reconstruction rules:

- An FK column that is also the table's primary key comes back as `OneToOneField(...,
  primary_key=True)`; an FK column without the `_id` suffix (`owner_code`) gets
  `source_field="owner_code"`.
- When a table has two or more relations to the same target (including a self-referencing pair),
  each gets `related_name="<table>_<field>_set"` so their backward relations don't collide.
- A partial index keeps its `WHERE` clause as `PartialIndex(condition=RawSQLTerm(...))`, the
  predicate as the database reports it, on both PostgreSQL and SQLite. An expression index (`lower(note)`) is
  rebuilt as `Index(RawSQLTerm(...))` on both dialects, and a composite foreign key is rebuilt on
  SQLite too.
- An index or constraint keeps its database name (`Index(..., name="orders_placed_idx")`) unless it
  is the one hare generates for the declaration or the one PostgreSQL gives an unnamed index or
  `UNIQUE` constraint (`<table>_<columns>_idx`/`_key`); a plain single-column index under a name of
  its own becomes a named `Meta.indexes` entry instead of `db_index=True`.
- A unique index comes back as the declaration its name belongs to: an unnamed
  `UniqueConstraint(fields=...)` under the name hare generates for one (or the one PostgreSQL gives an
  unnamed `UNIQUE`), `Index(unique=True)` under the name hare generates for that, and a named
  `UniqueConstraint` otherwise — with its `condition=` (a partial unique index, predicate as raw
  SQL) and `deferrable=`/`initially_deferred=` (a single-column `DEFERRABLE UNIQUE` included).
- A `CHECK` predicate comes back without the pair of parentheses PostgreSQL wraps it in
  (`CheckConstraint(check=RawSQLTerm("age >= 0"))`).
- What no hare index can declare — an `INCLUDE (...)` column, a key's `DESC`/`NULLS FIRST` order or
  `COLLATE`, `NULLS NOT DISTINCT` — is left out of the reconstructed index and listed in a `# TODO`.
- A foreign key to a table of another schema than the inspected one stays a plain column with a
  `# TODO` naming its target — a relation can only point at a model of the same run.
- A partition of a partitioned PostgreSQL table is not listed (the partitioned table stands for it),
  unless named explicitly; `drift` doesn't report partitions as untracked tables either.
- A PostgreSQL identity column (`GENERATED ... AS IDENTITY`, not the primary key) becomes an integer
  field with `generated=True` and a `# TODO`: hare never writes it and reads the assigned value
  back, but has no identity-column DDL of its own. A quoted numeric default
  (`'-1.5'::double precision`) comes back as a number.
- A `UNIQUE`/indexed column whose field type can't be indexed (`JSONField`) loses the
  index with a `# TODO`, instead of producing a model that can't be imported.
- A single-column `UNIQUE` foreign key comes back as `OneToOneField`.
- A foreign key's own index (a plain index over exactly its key columns) isn't written out —
  `ForeignKeyField` [has it by default](../models/relations.md#index-on-the-key-column); a foreign
  key without one, and without another index starting with its columns, gets `db_index=False`.
- A JSON column's default (`'{}'::jsonb`) comes back as the document itself (`db_default={}`), and
  a SQLite expression default (`DEFAULT (datetime('now'))`) keeps its parentheses:
  `SqlDefault("(datetime('now'))")`.
- A column or table whose Python name would shadow one of the generated module's imports (a column
  `fields`, a table `model`) gets a trailing underscore (`fields_` with `source_field="fields"`,
  class `Model_` with `Meta.table = "model"`), and relations point at the renamed class.

## <a id="generating-model-source-programmatically"></a>Generating model source programmatically

`hare inspectdb` prints what `hare.inspectdb.SchemaInspector` returns:

```python
from hare.inspectdb import SchemaInspector

source = await SchemaInspector.inspect(connection, ["users", "orders"], schema="public")
```

`inspect(connection, tables=None, schema=None)` generates one importable module for the tables
(every table of `schema` when `tables` is omitted; `schema` is the connection's default schema when
`None`, and is ignored by a dialect without schemas). It raises `UnsupportedDialectError` for a dialect without an introspector,
`SchemaNotFoundError`/`TableNotFoundError` for a missing schema or table and
`DuplicateModelClassNameError` for two tables deriving one class name.
`SchemaInspector.render_module(table_infos, dialect)` renders tables already read with
`DatabaseCatalog` (see [Building a model from an introspected table](../models/runtime-models.md#building-a-model-from-an-introspected-table)).

Each table is first turned into the same model state a declared model has
(`hare.inspectdb.InspectedModelBuilder(table_info, ModelGenerationOptions(dialect, app_label)).build()` returns an
`InspectedModel` with `state` — a migrations `ModelState` of real field, index and constraint
objects — plus the `# TODO` notes and the `Meta` layout). The source is that state written the way
a migration file writes it — every field, index and constraint as its `deconstruct()` call — and
`ModelFactory` builds a class from the very same state, so the generated source and a class built
at runtime never disagree.
