# Writing a dialect: the schema

Steps 8 to 10 of [Writing a dialect](index.md): the schema editor writing DDL, the introspector reading
an existing schema, and migrations.

## <a id="schema-editor"></a>8. DDL: the schema editor

`build_schema_editor_class()` returns a `BaseSchemaEditor` subclass. The editor runs the public
operations (`create_model()`, `add_field()`, `alter_field()`, `add_index()`, ...) as the order of their
steps; the statements of each type of change are written by its **parts** — `SchemaEditorPart` subclasses
of `hare.dialects.base.schema.<folder>`, made once per editor from the classes it declares and reaching
the editor as `self.editor`:

| Folder | Parts |
|---|---|
| `columns/` | `ColumnDefinitions`, `ColumnTypeChanges`, `ColumnBackfill`, `ColumnConversionCheck`, `ColumnNarrowingCheck` |
| `tables/` | `TableCreation`, `TableRebuild`, `TableComments`, `TablePartitions` |
| `relations/` | `ForeignKeyRebuild`, `ManyToManyThroughTables` |
| `indexes/` | `IndexStatements`, `GeneratedIndexNames` |
| `constraints/` | `ConstraintStatements`, `ConstraintNames` |
| `triggers/` | `TriggerStatements` |
| `schema_objects/` | `Views`, `MaterializedViews`, `DatabaseFunctions`, `Sequences`, `RowLevelSecurityPolicies`, `Grants`, `EnumTypes`, `Extensions`, `Schemas` |
| `runtime_statements/` | `TableLocks`, `TableClearing`, `TenantConditions` |

A dialect writes a part its own way by subclassing it and naming the subclass on its editor — the
attribute is the part's name in snake case with `_class`, the part itself the name without it:

```python
class ColstoreTableComments(TableComments):
    editor: ColstoreSchemaEditor

    def get_table_comment_sql(self, table, comment):
        return ""    # set by a statement of its own below

    def get_table_comment_statements(self, table, comment):
        literal = self.editor.client.dialect.literals.get_string_literal_sql(comment)
        return [f"ALTER TABLE {table} MODIFY COMMENT {literal}"]


class ColstoreSchemaEditor(BaseSchemaEditor):
    table_comments_class = ColstoreTableComments    # editor.table_comments
```

The base renders standard DDL from the editor's class templates (`TABLE_CREATE_TEMPLATE`,
`FIELD_TEMPLATE`, `INDEX_CREATE_TEMPLATE`, `FOREIGN_KEY_TEMPLATE`, `ADD_FIELD_TEMPLATE`,
`ALTER_FIELD_TYPE_TEMPLATE`, `RENAME_INDEX_TEMPLATE`, ...); a dialect overrides the ones its database
writes differently and sets a template to `None` where the statement doesn't exist — the change then
rebuilds the table (`TableRebuild.remake_table(old_model, new_model, altered_fields=())`: a new table
built from the model the change leads to, the rows copied from the old table field by field — a field
of the same name, or the pair in `altered_fields` for a renamed one — the old table replaced). A database
whose key and unique constraint names are taken in the whole schema while the old table exists overrides
`remake_table()`: PostgreSQL keeps the rows in a copy while it drops the table and creates it anew under
the same names — its foreign keys from other tables, its views and the other objects of its model set
again, its sequences kept with their values.
`TableComments.get_table_comment_sql()` and `TableComments.get_column_comment_sql()` write a comment
inside `CREATE TABLE` and the column definition; a database setting comments by statements of their own
returns `""` from them and the statements from `get_table_comment_statements(table, comment)` and
`get_column_comment_statements(table, column, comment)` — hare runs them right after the table or the
column.

An `AlterField` to a narrower type is refused while a stored value doesn't fit it: the new field's
`get_narrowing_limit(old_field)` gives a `NarrowingLimit`, and the limit asks the editor's
`ColumnNarrowingCheck` for the predicate finding the values past it
(`NarrowingLimit.get_overflow_predicate_sql(check, quoted_column)`). A dialect overrides the predicates its
database writes otherwise: `get_integer_overflow_predicate_sql()`, `get_float_overflow_predicate_sql()`,
`get_decimal_overflow_predicate_sql()`, `get_text_overflow_predicate_sql()`,
`get_day_overflow_predicate_sql()` (with `get_time_of_day_predicate_sql()`),
`get_enum_overflow_predicate_sql()` and `get_held_value_overflow_predicate_sql()` for a value held in a
container. A limit only one dialect's fields give — an address narrowed to a network on PostgreSQL, a
moment or a byte length on ClickHouse — lives in that dialect's package and calls a predicate of that
dialect's own check. `ColumnConversionCheck.check_alter_field_conversion()` runs
before a column's type changes and checks nothing by default; a database that converts the stored values
after the column has changed (or turns one it can't convert into NULL or another value) raises there for
such a value.

### <a id="names"></a>Names, and DDL from the migration state alone

Every key, foreign key and unique constraint is created under a name hare computes — never one the
database makes up — so a later migration finds it by the same name, and no DDL depends on what the
catalog holds: `sqlmigrate` shows exactly the SQL `migrate` runs.

- A primary key: `ColumnDefinitions.get_primary_key_clause(name)` — ` CONSTRAINT <name> PRIMARY KEY` by
  default; a composite one, `PRIMARY_KEY_CONSTRAINT_CREATE_TEMPLATE`.
- A foreign key: `FOREIGN_KEY_TEMPLATE` gets the constraint's `{name}`, made from its table and its
  columns as an index's name is (`GeneratedNames.get_foreign_key_name(table, columns)`) — `SET CONSTRAINTS`
  names a constraint by its schema, every table's constraint of that name at once.
- A unique constraint — a `unique=True` field's own too:
  `ConstraintStatements.get_unique_constraint_clause(model, constraint)` gives its clause, written into
  `CREATE TABLE` and into `ALTER TABLE ... ADD`; a dialect whose tables take no constraint once created
  returns None, and the constraint is made as the unique index of its name,
  `ConstraintStatements.get_unique_index_sql(model, constraint, safe)` — as SQLite does, and as a constraint
  with a condition is everywhere. A second unique constraint over the same columns is added after the table,
  as adding it to an existing table makes it: a database may leave a repeated one out of `CREATE TABLE`.

After a table or a column is renamed, `GeneratedIndexNames` renames what was named after it: indexes by
`RENAME_INDEX_IF_EXISTS_TEMPLATE` (or drops and creates them where it is None), keys and foreign keys by
`RENAME_CONSTRAINT_TEMPLATE` — a database without it keeps their old names. A schema is dropped
(`Schemas.drop_schema()`) without `CASCADE`: a schema holding something hare doesn't know of is the
database's to refuse.

The editor reads the database only for the data a change needs — whether the stored values fit a narrower
type, a sequence's next value, the values an `ENUM` still holds; how to write the DDL is decided by the
models of the migration state alone. An object the migrations don't know of standing in the way makes the
database refuse the statement.

`Features` steer what DDL is written at all:

| Feature | Without it |
|---|---|
| `supports_foreign_keys` | No `FOREIGN KEY` constraints; hare runs every `on_delete` action and `PROTECT` check itself, as for a `db_constraint=False` relation. |
| `supports_unique_constraints` | No unique constraints: a uniqueness is refused — a model's when it is bound, a migration's before its first statement; an automatic through table keeps no unique pair; an upsert targets the primary key only. |
| `creates_tables_with_forward_references` | Set it when a table is created with a foreign key to one not created yet (the reference checked when a row is written) — `generate_schemas` then creates a cycle of foreign keys table by table; without it a table of the cycle gets the foreign keys to the tables after it once they exist. |
| `supports_partial_indexes`, `supports_index_key_order`, `supports_index_nulls_order`, `supports_concurrent_indexes` | The index part is left out or rejected — a `PartialIndex` without the feature is created as the index of every row. |
| `supports_deferrable_constraints`, `supports_not_valid_constraints` | The constraint type is rejected. |
| `supports_extensions`, `supports_collations` | `CreateExtension`, `CreateCollation` are rejected or skipped. |
| `truncates_values_on_type_change` | Set it when a narrowing type change silently cuts data — hare checks the data first. |
| `alters_indexed_columns` | Clear it when the database refuses to change a column an index covers — hare drops the covering indexes around the change and creates them again. |

A feature only says the database has it; the base editor writes none of its syntax. The dialect that sets
one writes the SQL in its parts — the clauses below are class methods of a part, which the core calls as
`dialect.schema_editor_class.<part>_class.<method>()` where it has no editor:

| Feature | What the dialect's parts implement |
|---|---|
| Non-key index columns (`include=`) | `IndexStatements.get_index_include_sql(quoted_columns)` — the clause after the index keys; `""` by default, which leaves the columns out. |
| `UniqueConstraint(nulls_distinct=...)` | `ConstraintStatements.get_nulls_distinct_sql(nulls_distinct)` — the clause; also set `Features.supports_nulls_distinct`. |
| Enum fields' `CHECK` (`EnumMembersCondition`) | `ConstraintStatements.get_enum_members_check_sql(quoted_column, value_sqls)` — the predicate: the column holds one of the members' values; ISO `IN (...)` by default. |
| Exclusion constraints | `ConstraintStatements.get_exclusion_constraint_extension(constraint, fields_by_name)` — the extension a constraint needs; `ConstraintStatements.exclusion_constraint_sql(model, constraint)` — its definition. |
| `supports_concurrent_indexes` | the editor's `add_index(model, index, concurrently)` and `remove_index(...)`; the base builds and drops the plain way. `IndexStatements.get_index_create_sql()` and `IndexStatements.get_index_drop_sql()` give the plain statements to start from. |
| `supports_not_valid_constraints` | `ConstraintStatements.add_check_constraint_not_valid()` and `validate_constraint()`; the base adds the constraint as usual and validates nothing. |
| `supports_extensions` | `Extensions.create_extension()`, `drop_extension()` and `get_extension_create_sql()`. |
| `supports_enum_types` | `EnumTypes.get_enum_type_create_sql()`, `drop_enum_type()` and `alter_enum_type()` — the `ENUM` types of `NativeEnumField` columns. |
| `supports_views` | `Views.get_view_create_sqls()`, `drop_view()` and `rename_view()`; the base `alter_view()` drops the view, creates the new one and grants on it again. |
| `supports_materialized_views` | `MaterializedViews.get_materialized_view_create_sqls()` (with the unique index of `unique_columns`), `drop_materialized_view()`, `rename_materialized_view()` and `refresh_materialized_view()`. |
| `supports_database_functions` | `DatabaseFunctions.get_function_create_sqls()`, `drop_database_function()`, `alter_database_function()` and `rename_database_function()`. |
| `supports_sequences` | `Sequences.get_sequence_create_sqls()`, `get_sequence_owner_sqls()`, `drop_sequence()`, `alter_sequence()`, `rename_sequence()` and `get_next_sequence_value()`. |
| `supports_row_level_security` | `RowLevelSecurityPolicies.get_row_level_security_sqls()`, `get_policy_create_sqls()`, `drop_policy()`, `alter_policy()` and `rename_policy()` — see [`supports_row_level_security`](#supports-row-level-security) |
| `supports_grants` | `Grants.get_grant_sqls()` and `get_revoke_sqls()`. |
| `supports_collations` | `Extensions.create_collation()` and `drop_collation()`. |
| `supports_schemas` | `Schemas.move_table_to_schema()`, for a model whose `Meta.schema` changes, and `move_to_schema(model, object, old_schema, new_schema)` of `Views`, `MaterializedViews`, `DatabaseFunctions` and `Sequences` — the model's own objects moved with its table (`UnSupportedError` by default, before any DDL). |
| Deferrable constraints | `TableClearing.defer_cascade_foreign_keys(model, connection)` — defers the foreign keys a cascade runs into. |
| Test databases | `TableClearing.clear_tables(connection, quoted_tables)` — empties tables between tests (one script of `DELETE FROM` per table by default). |

### <a id="supports-row-level-security"></a>`supports_row_level_security`

`RowLevelSecurityPolicies.get_row_level_security_sqls()`, `get_policy_create_sqls()`, `drop_policy()`,
`alter_policy()` and `rename_policy()`; for tenancy by row level security also
`TenantConditions.get_tenant_condition_sql(quoted_column, column_type)` (the predicate of
`TenantCondition`), `TransactionStatements.get_tenant_setting_sql(tenant_texts)` (the statement after
`BEGIN` setting the transaction's tenants) and the connection option `tenant_row_level_security`.

The same goes for lookups only one database has: the dialect registers them from its `install()` with
[`Field.register_lookup()`](../custom-lookups.md), the extension they need in `required_extension=`, their
operators [marked as ones only dialects implement](../custom-lookups.md#one-database) and implemented in
the overrides of its `FilterOperators` — as PostgreSQL does for its trigram lookups. Its own fields it
registers in its types (`TypeMapping.column_type`), the fields setting `COLUMN_TYPE_FROM_DIALECT = True`.
A declaration given per database — a `GeneratedField`'s `{"sqlite": ..., "postgresql": ...}` — is picked by
`Dialect.get_declared_variant(variants)`, the variant under the dialect's name by default. A dialect's own
[request query](../../integrations/request-queries.md#dialects) subclasses `RequestQuery` in its package
and sets `dialect` to its dialect object: the filters are checked against that dialect only.

### <a id="table-options"></a>Table options

A database whose `CREATE TABLE` takes more than columns declares a `TableOptions` subclass; models list
it in `Meta.table_options`, one entry per dialect, and a connection uses the entry its dialect picks —
`Dialect.get_table_options(entries)`, the entry of the dialect's own options class, else its
`default_table_options`:

```python
@dataclasses.dataclass(frozen=True)
class ColstoreTableOptions(TableOptions):
    engine: str = "MergeTree()"
    order_by: tuple[str, ...] = ()
    partition_by: RawSQLTerm | None = None

    def raise_if_unsupported(self, model):
        if not self.order_by:
            raise ConfigurationError(f"{model.__name__}: ColstoreTableOptions needs order_by")

    def get_create_suffix_sql(self, model, quote):
        sql = f" ENGINE = {self.engine} ORDER BY ({', '.join(quote(column) for column in self.order_by)})"
        return sql + (f" PARTITION BY {self.partition_by.sql}" if self.partition_by else "")


class Event(Model):
    class Meta:
        primary_key = None
        table_options = [ColstoreTableOptions(order_by=("created_at",))]
```

The dialect names the class in `build_table_options_class()`. Migrations keep the options in the model
state and write them into migration files through `deconstruct()`; a change of them is applied by the
`TablePartitions` part's `alter_table_options()` — by default the table is rebuilt with the new options.
`hare drift` compares them with what the introspector reads, and `inspectdb` writes them into
`Meta.table_options` (see the introspector below).

Options that hold parts of the table the migrations add and remove one at a time — partitions — take five
more hooks, all with a default that means "none":

| Hook | What it gives |
|---|---|
| `get_partitions()` | The partitions by name; each partition object has a `name` and a `deconstruct()`. |
| `accepts_partition(partition)` | Whether a partition is one of these options' — the entry `AddPartition`/`RemovePartition` changes, its DDL run on a connection whose dialect picks that entry. |
| `with_partitions(partitions)` | The options with another set of them. |
| `can_change_partitions_to(new_options)` | Whether the other options are reached by adding and removing partitions — else the change is one `AlterModelOptions`. |
| `with_field_names(column_to_field_name)` | The options naming model fields where they name columns — options read from a database name columns. |

Options storing the rows elsewhere than in the model's own table — a table spread over the servers of a
cluster — take three more:

| Hook | What it gives |
|---|---|
| `get_storage_table_name(table_name)` | The table the DDL of the model's columns and storage is written for — the model's own by default. |
| `get_companion_table_sqls(model, quote, client, replaces=...)` | The statements creating the table put before the one storing the rows; none by default. |
| `keeps_row_versions()` | Whether the table keeps several rows of one key that the database merges. |

The table rebuild's `copy_table_rows(model, new_table_name, old_table_name, column_mapping)` copies the
rows into the rebuilt table — one `INSERT ... SELECT` by default. The dialect's
`get_journal_table_options(connection)` stores the journal of applied migrations (ClickHouse's on a
cluster is replicated), and `synchronize_table(connection, table_name)` waits for the rows other servers
wrote to it before the journal is read.

`raise_if_unsampled(model)` rejects a `sample()` of a table its options allow none of — ClickHouse's
without `sample_by`; nothing by default. A dialect writing the primary key of a table elsewhere than
among its columns (ClickHouse, in its engine's `PRIMARY KEY` clause) returns `""` from its table creation
part's `get_composite_pk_constraint_sql()` and leaves `PRIMARY KEY` out of its column definitions part's
`get_field_sql()`.

`makemigrations` then writes `AddPartition`/`RemovePartition` for each partition that came or went, and
they call the `TablePartitions` part's
`add_partition(model, partition)`/`remove_partition(model, partition)` (`UnSupportedError` by default);
`TablePartitions.get_partition_create_sqls(model, safe)` returns the statements creating the partitions
right after the table's `CREATE TABLE`. PostgreSQL's [partitioned
tables](../../dialects/postgresql/indexes-and-tables.md#partitioning) are built on these hooks.

### <a id="models-without-a-primary-key"></a>Models without a primary key

A table of a columnar database often has no primary key: `Meta.primary_key = None` (see
[Meta options](../../models/meta-options.md#primary_key)). Reads, filters, aggregates, `bulk_create()`
and `QuerySet.update()`/`delete()` work; what needs a key to identify a row (`save()` of a fetched row,
`instance.delete()`, relations to the model, ...) raises `ConfigurationError`.

## <a id="introspector"></a>9. Reading an existing schema: the introspector

`build_introspector_class()` returns a `SchemaIntrospector` subclass implementing
`fetch_default_schema()`, `fetch_table_names(connection, schema, include_partitions)` and
`fetch_tables(connection, tables, schema) -> list[TableInfo]`; `fetch_schema_names(connection)` lists the
schemas for a [schema per tenant](../../soft-delete-versions-tenants/schema-per-tenant.md)
(`UnSupportedError` by default). The rest — `inspectdb`, drift, the reverse type mapping to fields —
works from the neutral `TableInfo`/`ColumnInfo`/`IndexInfo`/ `ForeignKeyInfo`. A table's storage goes
into `TableInfo.table_options` through
`table_options_class.from_observed({"engine": ..., "order_by": ...})`, which keeps the names the class
declares and gives None when every option has its default.

[Drift](../../migrations/drift.md) reads the database and its shadow alike with
`fetch_schema_parts(connection, schema, tables, object_names) -> list[SchemaPart]`: by default the parts of
each table's `TableInfo` and the schema's `ENUM` types, plus `fetch_object_parts(connection, schema,
object_names)` — the views, materialized views, functions and dictionaries named, by their
`SchemaPartType`, each as the database prints it (none by default). A `SchemaPart` is a part's type, its
table, its name and a `definition` of plain values, lists and dicts compared as they are.
`build_shadow_database_class()` of the dialect returns the `ShadowDatabase` subclass
(`hare.dialects.base.schema.shadow`) the shadow is built in — an asynchronous context manager whose
entering creates it and sets its `client`, whose leaving removes it whatever happened, with
`get_shadow_schema_name(schema)` (the shadow's schema of a schema of the models),
`get_shadow_table_options(options)` (a table's options as the shadow takes them — ClickHouse puts a
`Replicated*` table's Keeper path under its own) and `replace_shadow_names(text)` (a text the shadow
printed, its names made the database's); None (the default) makes drift raise `UnSupportedError`.

How the database echoes an expression back is the introspector's too: `NOW_EXPRESSIONS` (the spellings of
the current time), `SEQUENCE_DEFAULT_PREFIXES` (a default drawing a sequence's next value),
`split_type_cast(sql)` (the type cast ending an expression, split off — PostgreSQL's
`'active'::character varying`), `NUMERIC_CAST_TYPES` (the casts whose quoted literal is a number) and
`get_generated_field(column, attribute_names_by_column)` (the field class path and arguments of a
field of the dialect that generates its column itself, recognized by the generation expression —
PostgreSQL's stored `TSVectorField`; None keeps a `GeneratedField`). The defaults read nothing of the
type. Without an introspector (`None`, the default) `inspectdb` and `hare drift` raise
`UnsupportedDialectError` (`hare.inspectdb.exceptions`, an `UnSupportedError`).

## <a id="migrations"></a>10. Migrations

The migration journal and every operation go through the schema editor, so they need nothing of their
own. `Features.can_rollback_ddl` decides whether a migration runs in a transaction, the
`TableLocks.get_migration_lock_sql()` whether two `migrate` runs wait for each other. A renderer writing a
function, collation or operator hare installs on its own connections calls
`sql_context.note_connection_function(name)`: an index's keys and a constraint's condition are written
with a list noting them, and one noted refuses the DDL (`UnSupportedError`) — the SQL text, raw SQL
included, is never searched (the SQLite functions hare registers on each connection — see [SQLite:
functions](../../dialects/sqlite/functions.md#in-constraints-and-indexes)).

`ForeignKeyRebuild.add_foreign_key_constraint_not_valid()` and
`ConstraintStatements.add_unique_constraint_using_index()` back `AddField(..., not_valid=True)` and
`AddConstraint(..., using_index=...)`; the base adds the constraint checked, and builds the constraint's
own index and drops the old one. The class method `ColumnTypeChanges.rewrites_table_on_alter()` tells
which field changes rewrite the table — every column type change by default.

The [migration safety check](../../migrations/zero-downtime.md#checking-migrations) takes its rules from
`MigrationSafetyRules`: `get_rules()` lists them — the base gives the ones every database shares, a
dialect adds its own, each a `MigrationSafetyRule` subclass in a file of its own with a `code`
(`MigrationRiskCode`) and `check_operation(context)` (or `check_migration(contexts)` for a rule about the
whole migration) returning a `MigrationRisk` from `context.get_risk(...)`. The `MigrationSafetyContext`
it gets holds the migration and the operation, the states before and after it, the dialect and its
`features`, `large_table_rows` and the rows of each table the migration changes (`table_row_counts`).
`count_table_rows(client, table_name, schema, at_most)` counts a table's rows up to `at_most` (an
estimate will do), 0 for a missing table; the base returns None — every table counts as large.
