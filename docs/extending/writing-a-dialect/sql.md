# Writing a dialect: SQL

Steps 3 to 6 of [Writing a dialect](index.md): names, literals and parameters, column types and values,
expressions and lookups, and the clauses of a statement.

## <a id="dialect"></a>3. Names, literals and parameters: `SqlLiterals`, `SqlParameters`

```python
class ColstoreLiterals(SqlLiterals):
    identifier_quote_char = "`"
    alias_quote_char = "`"


class ColstoreParameters(SqlParameters):
    placeholder_template = "{{p{}}}"          # {p1}, {p2}, ...


class ColstoreDialect(Dialect):
    name = "colstore"
    otel_system_name = "colstore"           # OpenTelemetry db.system.name
    features = Features(
        supports_transactions=False,
        supports_select_for_update=False,
        supports_update_limit_order_by=False,
        supports_foreign_keys=False,
        supports_unique_constraints=False,
    )

    def build_literals(self):
        return ColstoreLiterals(self)

    def build_parameters(self):
        return ColstoreParameters(self)
```

`SqlLiterals` writes names and values into the SQL text; names and literals are escaped only here — the
schema editor, the SQL renderer and the introspector all ask it:

| Member | Meaning |
|---|---|
| `identifier_quote_char`, `alias_quote_char`, `quote_identifier(name)`, `qualify_table_name(table, schema)` | Quoting of table/column/index/constraint names and of SELECT aliases. |
| `get_string_literal_sql(text)`, `get_literal_sql(value)`, `get_boolean_literal_sql(value)`, `get_bytes_literal_sql(value)`, `get_array_literal_sql(element_sqls)` | Literals written into SQL text — a string, a column default, a boolean of a query, bytes, an array. |

`SqlParameters` binds values:

| Member | Meaning |
|---|---|
| `placeholder_template`, `get_placeholder(index)`, `numbers_parameters` | A bound parameter's placeholder, `{}` standing for its index from 1 (`$1` on PostgreSQL, `?` on SQLite). Cached query shapes substitute values by position, so any placeholder style works with the query cache. |
| `get_parameter_cast_type(value, position)`, `get_field_parameter_cast_type(field)`, `get_row_key_cast_type(field)`, `get_json_object_value_cast_type(value, value_type)`, `get_cast_parameter_sql(sql, value)` | The type a bound value is cast to where nothing around it gives the parameter a type — see [`get_parameter_cast_type()`](#get-parameter-cast-type) |
| `get_bindable_number(value)` | How a number standing for a JSON number is bound. |
| `single_parameter_in_list_min_length` | The length from which an `__in`/`__not_in` list binds as one parameter. |
| `get_default_rows_source_sql(row_count)`, `get_column_arrays_rows_source_sql(column_types, first_index)` | The sources a multi-row `INSERT` reads rows of defaults, or of one array per column, from — None for a statement per row and `VALUES`. |
| `get_copy_column_type(field)` | The type a bulk load (`copy()`) is told a field's column has — the type the table declares by default. |
| `supports_copy_column_type(column_type)` | Whether the bulk load loads a column of that type. |

### <a id="get-parameter-cast-type"></a>`get_parameter_cast_type()`

The type a bound literal is cast to where nothing around the parameter types it — by where it stands
(`ParameterPosition`: a `CASE` branch, a selected or compared literal, a function argument), as a
column's value, as a key column's value in a row of `bulk_update()` (the column's by default), as a value
of a JSON object, or as rendered SQL. None or the SQL unchanged by default; PostgreSQL, which types a
parameter only from what's around it, casts; ClickHouse casts the keys, a bare literal taking the
narrowest type that holds it.

`Dialect.sql_context` is the context every statement renders in; the query class sets
`SQL_CONTEXT = dialect.sql_context`.

## <a id="types"></a>4. Column types and values: `build_types()`

`build_types()` returns a `TypeRegistry` (`hare.dialects.base.types`) mapping field classes to how the
dialect stores them. A field class uses the mapping of its nearest registered base class, so a
third-party field inherits its parent's storage:

```python
def build_types(self):
    types = TypeRegistry()
    types.register(BigIntField, TypeMapping(column_type="Int64"))
    types.register(DatetimeField, TypeMapping(column_type="DateTime64(6, 'UTC')"))
    types.register(UUIDField, TypeMapping(column_type="UUID"))
    types.register(CharField, TypeMapping(column_type=lambda field: "String"))
    types.register(
        DecimalField,
        TypeMapping(column_type=lambda field: f"Decimal({field.max_digits}, {field.decimal_places})"),
    )
    return types
```

| `TypeMapping` member | Meaning |
|---|---|
| `column_type` | The column type, or a `(field) -> str`. Without, the field's own `SQL_TYPE`. |
| `generated_sql` | The DDL of a database-generated primary key column. |
| `function_cast` | A `(field, term) -> term` wrapped around the column wherever it is compared, ordered or copied. |
| `to_db`, `to_lookup`, `to_python` | Replace the field's `to_db_value()`, `to_lookup_value()` and `from_db_value()` — each value binds and reads through the query's connection. |
| `json_term` | The text a column's value is written into a JSON object as, for a value stored in a form JSON can't hold (the columnar dialect's 16-byte UUIDs). |
| `extension` | The database extension the column type needs (`postgis` for PostgreSQL's spatial types) — created wherever a field of the class is used, and added to migrations as `CreateExtension`. |
| `naive_datetime_is_utc` | `to_python` is the field's own `from_db_value()`, with a naive datetime from the driver read as a UTC instant. |
| `inserted_by_select` | An INSERT writing the column writes its rows as a `SELECT` of them, not as `VALUES` — for a value written as an expression the dialect's `VALUES` doesn't read; or a `(field) -> bool` deciding it per field. |

`Features` describe how values behave: `enforces_numeric_ranges` (without, hare checks integer and
decimal ranges itself), `supports_virtual_generated_columns`. Where a value is stored or computed
differently, the dialect's `TermRenderers` give the SQL ([next section](#functions-and-lookups)). A
function the database has only for some argument types (PostgreSQL's `ROUND(numeric, int)`) is a renderer
too.

A package adding a field can register its storage on any dialect:

```python
DialectRegistry.get_dialect("colstore").types.register(MoneyField, TypeMapping(column_type="Decimal(18, 2)"))
```

## <a id="functions-and-lookups"></a>5. Expressions and lookups: `TermRenderers`, `FilterOperators`

Every expression renders standard SQL by default. `build_renderers()` returns the `TermRenderers` that
replace what the database writes differently, found through the term's class hierarchy:

```python
def build_renderers(self):
    renderers = TermRenderers(self)
    renderers.register_function("LENGTH", self.render_length)         # (function, sql_context) -> sql
    renderers.register_name(functions.Coalesce, lambda term, sql_context: "ifNull")
    return renderers

@staticmethod
def render_length(length, sql_context):
    return f"lengthUTF8({length.get_arg_sql(length.args[0], sql_context)})"
```

hare's own dialects build theirs on `CheckedTermRenderers` (`hare.dialects.base.renderers`): its
`add_dialect_renderers()` registers the dialect's renderers, grouped by theme into classes of one
`register(renderers)` each — `<Dialect>JsonRenderers`, `<Dialect>TemporalRenderers`,
`<Dialect>NumberRenderers`, `<Dialect>TextRenderers`, and those of spatial terms, vectors and full-text
search. Building it checks that every term each of hare's dialects must render — the JSON, temporal,
numeric and text terms the core leaves to the dialect — has a renderer, and raises `TypeError` naming the
missing ones. A third-party dialect may start from it the same way, or from a plain `TermRenderers`.

A subclass of `TermRenderers` also gives the terms the dialect puts in place of an expression where its
SQL needs another:

| Member | Meaning |
|---|---|
| `is_distinct_from_operator` | The NULL-safe inequality (`IS NOT` on SQLite). |
| `get_decimal_compared_term()`, `get_decimal_value_term()`, `get_decimal_dividend()`, `get_assigned_decimal_term()` | Decimals stored as text: compared, mixed with other numbers, divided, assigned by an `UPDATE`. |
| `get_enum_number_term()` | The column of an integer enum field where its number is computed with — an operand of arithmetic, an argument of a function of numbers — on a database whose enum type takes neither. |
| `get_exact_integer_operand()` | An operand of an integer sum, difference or product an `UPDATE` writes into a column, widened where the database's arithmetic wraps around — the written value's check then sees the exact result. |
| `get_json_path_comparand()` | What the JSON value at a path is compared with — JSON stored as text. |
| `get_integer_aggregate_as_float()` | An average or a statistic of integers, read as a float. |
| `get_datetime_part_comparand()` | A date or time of day compared with a datetime's part. |
| `get_concatenated_argument_sql(sql, argument)` | An argument of a text concatenation, cast where the database types it only from what's around it. |
| `get_ordering_term()` | What `ORDER BY` sorts by for a term. |
| `get_value_range_ordering_term()` | What a window orders by when its frame is a `RANGE` a number of values away from the current row — a number, where the column's own value takes no offset (a decimal stored as text). |
| `get_composite_distinct_key()` | What `COUNT(DISTINCT ...)` counts a composite key by. |
| `get_never_null_column_count_argument(term)` | What `COUNT` takes for a column of the queried table that holds no NULL — the column by default; `*` where counting rows is cheaper than reading the column. |
| `get_connection_only_function(sql)` | A function hare installs on its own connections that DDL can't use. |

`build_filter_operators()` returns the `FilterOperators` — a mapping from a lookup's own operator to the
dialect's replacement (`FilterOperators(self, {Lookups.is_in: colstore_is_in, ...})`, `{}` for none). An
operator only dialects implement (`DialectImplementedOperators`) has to be replaced, or the lookup is
unsupported: `FilterOperators.supports_lookup()` — and with it `Model._meta.get_lookups(path, dialect)` —
leaves it out, and a filter using it fails before any SQL is built.

A value list too long to bind one parameter per value is the dialect's to shorten: subclass `LargeInList`
(`hare.dialects.base.parameters.large_in_list`) and map `Lookups.is_in`/`not_in`/`row_is_in`/
`row_not_in` to its methods of the same names in `build_filter_operators()`. The base class holds the
algorithm once — when the short form applies, how `NULL`s in the list are compared apart, how `not_in` is
negated, the row-value form for a composite key; the dialect gives only the container the values are
bound into as **one** parameter: `get_membership_criterion(field, values, non_null_values, element_type)`
(PostgreSQL's `= ANY($1::type[])`, SQLite's `IN (SELECT value FROM json_each(?))`) and
`get_row_container(value_rows, element_types)` for value rows. Returning `None` keeps the plain
`IN (...)` list.

A renderer changes how a term's SQL is written, never what a query's plan is keyed by — the dialect is
part of every plan key. An expression class of the dialect's own (PostgreSQL's array, trigram and spatial
functions) declares how its attributes meet the plan in `plan_parts` and resolves its arguments with
`ExpressionArguments.get_result()`, as any [custom
expression](../custom-functions-and-expressions.md#writing-a-custom-expression) does.

## <a id="query-class"></a>6. Statement clauses: `QueryClauses`

There is one `QueryBuilder` for every database. It renders ISO SQL and asks the dialect's `QueryClauses`
for every clause databases write differently; the base `QueryClauses` writes ISO SQL
(`OFFSET ... ROWS FETCH FIRST ... ROWS ONLY`) and raises `UnSupportedError` for a clause ISO SQL lacks:

| Member | Writes |
|---|---|
| `get_statement_context(builder, sql_context)` | The context a statement's own terms render in — the same one by default; a dialect whose renderers need to know something of the statement around a term (that it is grouped) returns a context of its own holding it. |
| `get_bulk_load_statement_sql(table, columns)` | The statement a bulk load (`copy()`) is reported under to observers, spans and the query counter. |
| `get_distinct_sql(builder, sql_context)` | `DISTINCT` / `DISTINCT ON (...)`. |
| `get_row_lock_sql(builder, sql_context)` | A row lock (`select_for_update()`): `FOR UPDATE`, `NO KEY`, `OF`, `NOWAIT`, `SKIP LOCKED`. |
| `get_statement_end_sql(builder, sql_context)` | What a subquery, or a query with clauses of the dialect's `QuerySet` methods, ends with after its `LIMIT` — nothing by default; ClickHouse writes its `SETTINGS` there, giving a joining subquery `join_use_nulls = 1`, which a mutation's condition runs without otherwise. |
| `get_main_table_suffix_sql(builder, sql_context)` | What follows the first table of `FROM` — its sample (`get_table_sample_sql()`) by default; ClickHouse writes `FINAL` and `SAMPLE ... OFFSET ...` there. |
| `get_prewhere_sql(builder, sql_context)`, `get_limit_by_sql(builder, sql_context)` | A condition read before `WHERE`, after the JOINs; a limit of rows per group, after `ORDER BY` — nothing by default. |
| `get_limit_offset_sql(limit_sql, offset_sql)` | The bounds of a query's rows. |
| `get_returning_sql(returned)` | `RETURNING`, from `ReturnedValue`s — a value's SQL, the alias it is returned under, the name of a plain column. |
| `get_lateral_sql(subquery_sql)` | A subquery joined `LATERAL` — ISO `LATERAL (...)` by default. |
| `get_table_sample_sql(method, percent_sql, seed_sql)` | The sample of a table in `FROM` — ISO `TABLESAMPLE ... REPEATABLE (...)` by default. |
| `get_merge_sql(...)`, `get_merge_when_sql(when)`, `get_merge_returning_sql(returned, action_alias_sql)` | A `MERGE` from rendered parts (`MergeWhenSql`) — ISO `MERGE` by default; a `DO NOTHING` branch, `WHEN NOT MATCHED BY SOURCE` and `RETURNING` raise `UnSupportedError` there. |
| `get_on_conflict_sql(builder, sql_context)` | The conflict handling of an `INSERT`. |
| `get_update_sql(builder, sql_context)` | An `UPDATE` — reading other tables in `FROM`, ordering and limiting its rows where the database can. |
| `get_delete_sql(builder, sql_context)` | A `DELETE` — ISO `DELETE FROM` with its conditions by default; a database deleting rows another way (an `ALTER TABLE ... DELETE` mutation) writes its own. |
| `get_typed_placeholder_template()`, `get_values_table_columns_sql()`, `get_update_from_values_sql()`, `get_insert_rows_source_sql()` | The statements hare writes as text: an `UPDATE` from a `VALUES` table (`bulk_update()`), an `INSERT` of rows from a source. |
| `get_upsert_inserted_flag_sql()` | The `RETURNING` expression telling an upsert's inserted row from an updated one. |
| `get_explain_sql(sql, output_format, options, features)` | The `EXPLAIN` statement `QuerySet.sql(explain=True)` and `explain()` use. |

The query class only binds statements to the dialect:

```python
ColstoreQuery = DeclaredSubclass.make(
    Query, "ColstoreQuery", __package__, "A query in Colstore's SQL.", SQL_CONTEXT=COLSTORE_DIALECT.sql_context
)
```

### <a id="queryset-methods"></a>`QuerySet` methods of the dialect

A database's own query modifiers become `QuerySet` methods without hare knowing them. Register them in
`install()` with a function that gets the query's builder and the call's arguments and returns the
builder — the columnar test dialect's `random_share()`:

```python
def install(self):
    super().install()
    QuerySetExtensions.register("random_share", self.name, self.apply_random_share)

def apply_random_share(self, builder, percent):
    return builder.where(LiteralValue(f"abs(random()) % 100 < {int(percent)}"))
```

`Event.objects.filter(...).random_share(10)` records the call — before `Hare.init()` too — and the query
applies it when it is built for its connection; on a connection of another dialect it raises
`UnSupportedError`. A package can register a method for a dialect it doesn't define.

`QuerySetExtensions.register(name, dialect_name, apply)` (from `hare.query.queryset.extensions`) is
usually called in the dialect's `install()`. `apply(builder, *args, **kwargs)` returns the query builder
with the call applied. A call is recorded on the queryset (before `Hare.init()` too) and carried into
`values()`, `count()` and the other queries built from it; when the query is built for its connection,
the implementation of that connection's dialect applies it. On a connection whose dialect registered
none, the query raises `UnSupportedError` instead of dropping the call. A name QuerySet already has, or a
private one, is refused with `ConfigurationError`. The name and arguments of each call are part of the
plan key — written into the query after it is built, they are part of its SQL text; a call with an
argument that can't be part of a key (a list, a dict) keeps the query from keeping a plan. An argument
that describes itself to a plan — a `Q`, an expression — is part of the key by its structure, its values
bound like a filter's.

`register()` takes four more keyword arguments, for a method that needs more than the builder:

| Argument | Meaning |
|---|---|
| `reads_query=True` | `apply(builder, extension_query, *args, **kwargs)` gets the query too — a `QuerySetExtensionQuery`: its `model`, `connection`, `dialect`, `table`, whether it `is_write` or `is_summary`, and `get_condition(q)`, `get_expression(name_or_expression)` and `join(builder, joins)`, which resolve a condition or a field as the query's own filters are, their values recorded into its plan. |
| `takes_condition=True` | The method takes `filter()`'s arguments — `Q` objects and filters — recorded as one `Q` that `apply` gets (ClickHouse's `prewhere()`). Every dialect registering the name declares it alike, or `register()` raises `ConfigurationError`. |
| `changes_rows=True` | A call changes which rows the query returns beyond its conditions (ClickHouse's `limit_by()`): `count()` and `exists()` count the rows of the query as a derived table. |
| `read_result=` | `await read_result(extension_query, result, *args, **kwargs)` reads the result of a rows query with the call and returns what it returns (ClickHouse's `with_totals()` runs a second statement and returns its rows with the totals). |

`QueryBuilder.set_dialect_clause(name, value)` keeps what a call sets for the dialect's `QueryClauses` to
write — in `get_main_table_suffix_sql()`, `get_prewhere_sql()`, `get_limit_by_sql()` and
`get_statement_end_sql()`, asked of a query only when it has such clauses. `hare stubs` and the mypy
plugin declare the methods of the dialects of a project's connections, with the parameters of `apply`
after the builder (and the query).
