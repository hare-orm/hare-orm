# Создание диалекта: схема

Шаги 8–10 [создания диалекта](index.ru.md): редактор схемы, который пишет DDL, интроспектор, который
читает существующую схему, и миграции.

## <a id="schema-editor"></a>8. Команды схемы: редактор схемы

`build_schema_editor_class()` возвращает подкласс `BaseSchemaEditor`. Редактор выполняет публичные
операции (`create_model()`, `add_field()`, `alter_field()`, `add_index()`, ...) как порядок их шагов;
команды каждого вида изменений пишут его **части** — подклассы `SchemaEditorPart` из
`hare.dialects.base.schema.<folder>`, которые редактор создаёт один раз из объявленных им классов и
которые обращаются к нему как `self.editor`:

| Папка | Части |
|---|---|
| `columns/` | `ColumnDefinitions`, `ColumnTypeChanges`, `ColumnBackfill`, `ColumnNarrowingCheck` |
| `tables/` | `TableCreation`, `TableRebuild`, `TableComments`, `TablePartitions` |
| `relations/` | `ForeignKeyRebuild`, `ManyToManyThroughTables` |
| `indexes/` | `IndexStatements`, `GeneratedIndexNames` |
| `constraints/` | `ConstraintStatements`, `ConstraintNames` |
| `triggers/` | `TriggerStatements` |
| `schema_objects/` | `Views`, `MaterializedViews`, `DatabaseFunctions`, `Sequences`, `RowLevelSecurityPolicies`, `Grants`, `EnumTypes`, `Extensions`, `Schemas` |
| `runtime_statements/` | `TableLocks`, `TableClearing`, `TenantConditions` |

Диалект пишет часть по-своему, создав её подкласс и назвав его у своего редактора, — атрибут называется
по имени части в snake case с `_class`, сама часть — без него:

```python
class ColstoreTableComments(TableComments):
    editor: ColstoreSchemaEditor

    def get_table_comment_sql(self, table, comment):
        literal = self.editor.client.dialect.literals.get_string_literal_sql(comment)
        return f"ALTER TABLE {self.editor.quote(table)} MODIFY COMMENT {literal}"


class ColstoreSchemaEditor(BaseSchemaEditor):
    table_comments_class = ColstoreTableComments    # editor.table_comments
```

Базовый класс выводит стандартные команды по шаблонам класса редактора (`TABLE_CREATE_TEMPLATE`,
`FIELD_TEMPLATE`, `INDEX_CREATE_TEMPLATE`, `FOREIGN_KEY_TEMPLATE`, `ADD_FIELD_TEMPLATE`,
`ALTER_FIELD_TYPE_TEMPLATE`, `RENAME_INDEX_TEMPLATE`, ...); диалект переопределяет те, которые его база
записывает иначе, и задаёт шаблону `None`, если такой команды нет, — тогда изменение пересоздаёт таблицу
(`TableRebuild.remake_table()`: новая таблица по модели, копирование строк, замена старой).
`TableComments.get_table_comment_sql()` и `TableComments.get_column_comment_sql()` записывают
комментарии; `ColumnNarrowingCheck.get_decimal_overflow_predicate_sql()` находит хранимые числа, которые
не поместятся после `AlterField` на более узкий десятичный тип.

`Features` решают, какие команды вообще записываются:

| Возможность | Без неё |
|---|---|
| `supports_foreign_keys` | Нет ограничений `FOREIGN KEY`; hare сам выполняет каждое действие `on_delete` и проверку `PROTECT`, как для связи с `db_constraint=False`. |
| `supports_unique_constraints` | Нет ограничений уникальности; уникальный `Index` становится обычным индексом; вставка-или-обновление ищет конфликт только по первичному ключу. |
| `supports_adding_constraints` | Ограничения CHECK записываются в `CREATE TABLE`, ограничения уникальности становятся уникальными индексами, а последующее изменение пересоздаёт таблицу. |
| `supports_partial_indexes`, `supports_index_key_order`, `supports_index_nulls_order`, `supports_concurrent_indexes` | Эта часть индекса не записывается или отклоняется — `PartialIndex` без этого признака создаётся как индекс по всем строкам. |
| `supports_exclusion_constraints`, `supports_deferrable_constraints`, `supports_not_valid_constraints` | Такой вид ограничения отклоняется. |
| `supports_statement_triggers`, `supports_extensions`, `supports_collations` | Триггеры `FOR EACH STATEMENT`, `CreateExtension`, `CreateCollation` отклоняются или пропускаются. |
| `truncates_values_on_type_change` | Задайте её, если сужающая смена типа молча обрезает данные, — тогда hare сначала проверяет данные. |
| `alters_indexed_columns` | Сбросьте её, если СУБД не меняет колонку, покрытую индексом, — hare удалит покрывающие индексы на время изменения и создаст их заново. |

Возможность только сообщает, что она у базы есть; базовый редактор её синтаксис не пишет. SQL пишут части
диалекта, который её выставил; фразы ниже — методы класса части, и ядро вызывает их как
`dialect.schema_editor_class.<part>_class.<method>()` там, где редактора у него нет:

| Возможность | Что реализуют части диалекта |
|---|---|
| Неключевые колонки индекса (`include=`) | `IndexStatements.get_index_include_sql(quoted_columns)` — фраза после ключей индекса; по умолчанию `""`, и колонки в индекс не попадают. |
| `UniqueConstraint(nulls_distinct=...)` | `ConstraintStatements.get_nulls_distinct_sql(nulls_distinct)` — фраза; ещё выставьте `Features.supports_nulls_distinct`. |
| Ограничения исключения | `ConstraintStatements.get_exclusion_constraint_extension(constraint, fields_by_name)` — расширение, которое нужно ограничению; `ConstraintStatements.exclusion_constraint_sql(model, constraint)` — его определение. |
| `supports_concurrent_indexes` | `add_index(model, index, concurrently)` и `remove_index(...)` редактора; базовый редактор создаёт и удаляет индекс обычным способом. `IndexStatements.get_index_create_sql()` и `IndexStatements.get_index_drop_sql()` дают обычные команды, от которых можно оттолкнуться. |
| `supports_not_valid_constraints` | `ConstraintStatements.add_check_constraint_not_valid()` и `validate_constraint()`; базовый редактор добавляет ограничение как обычно и ничего не проверяет отдельно. |
| `supports_extensions` | `Extensions.create_extension()`, `drop_extension()` и `get_extension_create_sql()`. |
| `supports_enum_types` | `EnumTypes.get_enum_type_create_sql()`, `drop_enum_type()` и `alter_enum_type()` — типы `ENUM` колонок `NativeEnumField`. |
| `supports_views` | `Views.get_view_create_sqls()`, `drop_view()` и `rename_view()`; базовый `alter_view()` удаляет представление, создаёт новое и снова выдаёт права на него. |
| `supports_materialized_views` | `MaterializedViews.get_materialized_view_create_sqls()` (с уникальным индексом по `unique_columns`), `drop_materialized_view()`, `rename_materialized_view()` и `refresh_materialized_view()`. |
| `supports_database_functions` | `DatabaseFunctions.get_function_create_sqls()`, `drop_database_function()`, `alter_database_function()` и `rename_database_function()`. |
| `supports_sequences` | `Sequences.get_sequence_create_sqls()`, `get_sequence_owner_sqls()`, `drop_sequence()`, `alter_sequence()`, `rename_sequence()` и `get_next_sequence_value()`. |
| `supports_row_level_security` | `RowLevelSecurityPolicies.get_row_level_security_sqls()`, `get_policy_create_sqls()`, `drop_policy()`, `alter_policy()` и `rename_policy()` — см. [`supports_row_level_security`](#supports-row-level-security) |
| `supports_grants` | `Grants.get_grant_sqls()` и `get_revoke_sqls()`. |
| `supports_collations` | `Extensions.create_collation()` и `drop_collation()`. |
| `supports_schemas` | `Schemas.move_table_to_schema()` — для модели, у которой меняется `Meta.schema`. |
| Отложенные ограничения | `TableClearing.defer_cascade_foreign_keys(model, connection)` — откладывает внешние ключи, в которые упирается каскад. |
| Тестовые базы | `TableClearing.clear_tables(connection, quoted_tables)` — очищает таблицы между тестами (по умолчанию — один скрипт с `DELETE FROM` для каждой таблицы). |

### <a id="supports-row-level-security"></a>`supports_row_level_security`

`RowLevelSecurityPolicies.get_row_level_security_sqls()`, `get_policy_create_sqls()`, `drop_policy()`,
`alter_policy()` и `rename_policy()`; для арендаторов через row level security ещё
`TenantConditions.get_tenant_condition_sql(quoted_column, column_type)` (условие `TenantCondition`),
`TransactionStatements.get_tenant_setting_sql(tenant_texts)` (команда после `BEGIN`, записывающая
арендаторов транзакции) и параметр подключения `tenant_row_level_security`.

То же относится к операторам фильтра, которые есть только у одной базы: диалект регистрирует их в своём
`install()` через [`Field.register_lookup()`](../custom-lookups.ru.md), называя себя в `dialects=`, а
нужное расширение — в `required_extension=`; так PostgreSQL регистрирует свои триграммные операторы.

### <a id="table-options"></a>Параметры хранения таблицы

База, у которой `CREATE TABLE` принимает больше, чем колонки, объявляет подкласс `TableOptions`; модели
перечисляют его в `Meta.table_options`, по одной записи на диалект, а подключение использует запись
своего диалекта:

```python
@dataclasses.dataclass(frozen=True)
class ColstoreTableOptions(TableOptions):
    dialect_name: ClassVar[str] = "colstore"

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

Диалект называет класс в `build_table_options_class()`. Миграции хранят параметры в состоянии модели и
записывают их в файлы миграций через `deconstruct()`; их изменение применяет `alter_table_options()`
части `TablePartitions` — по умолчанию таблица пересоздаётся с новыми параметрами. `hare drift`
сравнивает их с тем, что прочитано из базы, а `inspectdb` записывает их в `Meta.table_options` (см.
чтение схемы ниже).

Параметры, в которых есть части таблицы, добавляемые и удаляемые миграциями по одной, — партиции, —
получают ещё четыре хука; значение по умолчанию у каждого означает «таких частей нет»:

| Хук | Что даёт |
|---|---|
| `get_partitions()` | Партиции по имени; объект партиции называет свой диалект в `dialect_name`, имеет `name` и `deconstruct()`. |
| `with_partitions(partitions)` | Те же параметры с другим набором партиций. |
| `can_change_partitions_to(new_options)` | Достижимы ли другие параметры добавлением и удалением партиций — иначе изменение идёт одним `AlterModelOptions`. |
| `with_field_names(column_to_field_name)` | Параметры с именами полей модели вместо имён колонок — параметры, прочитанные из базы, называют колонки. |

Параметрам, которые хранят строки не в собственной таблице модели, — таблица, распределённая по серверам
кластера, — нужны ещё три хука:

| Хук | Что даёт |
|---|---|
| `get_storage_table_name(table_name)` | Таблицу, для которой пишется DDL колонок и хранения модели, — по умолчанию собственную таблицу модели. |
| `get_companion_table_sqls(model, quote, client, replaces=...)` | Команды, создающие таблицу перед той, что хранит строки; по умолчанию никаких. |
| `keeps_row_versions()` | Хранит ли таблица несколько строк одного ключа, которые база сливает, — тогда ключ не проверяется (`checks_constraints_before_write`). |

`copy_table_rows(model, new_table_name, old_table_name, column_mapping)` перестройки таблицы копирует
строки в перестроенную таблицу — по умолчанию одним `INSERT ... SELECT`.
`get_journal_table_options(connection)` диалекта задаёт хранение журнала применённых миграций (на
кластере ClickHouse он реплицируется), а `synchronize_table(connection, table_name)` ждёт строк, которые
в него записали другие серверы, перед чтением журнала.

`raise_if_unsampled(model)` отклоняет `sample()` таблицы, параметры которой выборку не допускают, — у
ClickHouse без `sample_by`; по умолчанию ничего не делает. Диалект, который пишет первичный ключ таблицы
не среди её колонок (ClickHouse — в клаузе `PRIMARY KEY` движка), возвращает `""` из
`get_composite_pk_constraint_sql()` своей части создания таблиц и не пишет `PRIMARY KEY` в
`get_field_sql()` своей части определений колонок.

Тогда `makemigrations` пишет `AddPartition`/`RemovePartition` на каждую добавленную или убранную
партицию, а они вызывают `add_partition(model, partition)`/`remove_partition(model, partition)` части
`TablePartitions` (по умолчанию — `UnSupportedError`);
`TablePartitions.get_partition_create_sqls(model, safe)` возвращает команды создания партиций сразу после
`CREATE TABLE` таблицы. На этих хуках построены [партиционированные
таблицы](../../dialects/postgresql/indexes-and-tables.ru.md#partitioning) PostgreSQL.

### <a id="models-without-a-primary-key"></a>Модели без первичного ключа

У таблицы колоночной базы часто нет первичного ключа: `Meta.primary_key = None` (см.
[Опции Meta](../../models/meta-options.ru.md#primary_key)). Чтение, фильтры, агрегаты, `bulk_create()` и
`QuerySet.update()`/`delete()` работают; то, чему нужен ключ, чтобы найти строку (`save()` прочитанной
строки, `instance.delete()`, связи с моделью и т. п.), даёт `ConfigurationError`.

## <a id="introspector"></a>9. Чтение существующей схемы

`build_introspector_class()` возвращает подкласс `SchemaIntrospector`, реализующий
`fetch_default_schema()`, `fetch_table_names(connection, schema, include_partitions)` и
`fetch_tables(connection, tables, schema) -> list[TableInfo]`; `fetch_schema_names(connection)`
перечисляет схемы для [схемы на арендатора](../../soft-delete-versions-tenants/schema-per-tenant.ru.md)
(по умолчанию `UnSupportedError`). Остальное — `inspectdb`, поиск расхождений, обратное сопоставление
типов и полей — работает с общими `TableInfo`/`ColumnInfo`/ `IndexInfo`/`ForeignKeyInfo`. Хранение
таблицы попадает в `TableInfo.table_options` через
`table_options_class.from_observed({"engine": ..., "order_by": ...})`, который оставляет имена,
объявленные классом, и даёт `None`, если все параметры по умолчанию; `fetch_declared_table_options()`
позволяет поиску расхождений считать то же хранение, записанное базой иначе, совпадающим с объявленным, а
`fetch_declared_schema_objects(connection, schema, declared, table_info, column_to_field_name)`
возвращает объявленные моделью представления, материализованные представления и словари такими, какими их
держит база, — по умолчанию как объявлены, база не спрашивается.

То, как база возвращает выражение, тоже решает чтение схемы: `NOW_EXPRESSIONS` (написания текущего
момента), `SEQUENCE_DEFAULT_PREFIXES` (значение по умолчанию из последовательности),
`split_type_cast(sql)` (приведение типа в конце выражения, отделённое от него, — например
`'active'::character varying` в PostgreSQL), `NUMERIC_CAST_TYPES` (приведения, при которых литерал в
кавычках — число), `strip_type_casts(sql)` (выражение без всех приведений — чтобы сравнить объявленное
значение по умолчанию с прочитанным) и `get_generated_field(column, attribute_names_by_column)` (путь
класса и аргументы поля диалекта, которое само вычисляет свою колонку, узнанного по выражению колонки, —
хранимый `TSVectorField` PostgreSQL; None оставляет `GeneratedField`). По умолчанию ничего такого не
читается. Без чтения схемы (`None`, по умолчанию) `inspectdb` и `hare drift` дают
`UnsupportedDialectError` (`hare.inspectdb.exceptions`, это `UnSupportedError`).

## <a id="migrations"></a>10. Миграции

Журнал миграций и каждая операция идут через редактор схемы, поэтому своего им ничего не нужно.
`Features.can_rollback_ddl` решает, выполняется ли миграция в транзакции,
`TableLocks.get_migration_lock_sql()` — ждут ли друг друга два запуска `migrate`, а
`get_connection_only_function(sql)` рендереров называет функцию, которую hare устанавливает только в
своих соединениях и которую поэтому нельзя использовать в командах схемы (функции SQLite, которые hare
регистрирует на каждом подключении, — см. [SQLite:
функции](../../dialects/sqlite/functions.ru.md#in-constraints-and-indexes)).

`ForeignKeyRebuild.add_foreign_key_constraint_not_valid()` и
`ConstraintStatements.add_unique_constraint_using_index()` стоят за `AddField(..., not_valid=True)` и
`AddConstraint(..., using_index=...)`; базовый редактор добавляет ограничение с проверкой строк, а для
`using_index` строит собственный индекс ограничения и удаляет прежний. Метод класса
`ColumnTypeChanges.rewrites_table_on_alter()` говорит, какие изменения поля переписывают таблицу, — по
умолчанию любая смена типа колонки.

[Проверка миграций](../../migrations/zero-downtime.ru.md#checking-migrations) берёт правила из
`MigrationSafetyRules`: их перечисляет `get_rules()` — базовый класс даёт общие для всех баз, диалект
добавляет свои; каждое правило — подкласс `MigrationSafetyRule` в своём файле с `code`
(`MigrationRiskCode`) и `check_operation(context)` (или `check_migration(contexts)` для правила обо всей
миграции), возвращающим `MigrationRisk` из `context.get_risk(...)`.
`count_table_rows(client, table_name, schema, at_most)` считает строки таблицы до `at_most` (подойдёт и
оценка), 0 — для таблицы, которой нет; базовый класс возвращает None — тогда любая таблица считается
большой.
