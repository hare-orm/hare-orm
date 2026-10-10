from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from hare.ddl.conditions.constraint_condition import ConstraintCondition
from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.foreign_key_constraint import ForeignKeyConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.enums import GeneratedNamePrefix
from hare.ddl.generated_names import GeneratedNames
from hare.ddl.indexes.index import Index
from hare.dialects.base.schema.data.model_sql_data import ModelSqlData
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.fields.enums import OnDelete
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.models import Model
from hare.models.instances.instance_checks import InstanceChecks
from hare.models.meta_info import MetaInfo


class TableCreation(SchemaEditorPart):
    """The statements creating models' tables: the tables in dependency order with their columns, keys,
    constraints and indexes, the through tables of many-to-many relations, and the schema objects
    created before and after them."""

    __slots__ = ()

    def get_create_schema_sql(self, safe: bool = True) -> str:
        """Returns the DDL creating every model of this connection, in the order the tables
        reference each other: database schemas, extensions, tables with their indexes and
        constraints, automatic through tables, then triggers - a trigger body may use any table.

        Args:
            safe: Whether each object is created only when it doesn't exist yet.

        Returns:
            The DDL script.

        Raises:
            ConfigurationError: The tables' foreign keys reference each other in a cycle.
        """
        models_to_create = self.get_models_to_create()
        schemas = dict.fromkeys(model._meta.schema for model in models_to_create if model._meta.schema)
        statements = [sql for schema in schemas if (sql := self.editor.schemas.get_schema_create_sql(schema, safe))]
        statements += [
            sql
            for extension in self.get_required_extensions(models_to_create)
            if (sql := self.editor.extensions.get_extension_create_sql(extension))
        ]
        statements += [
            self.editor.enum_types.get_enum_type_create_sql(enum_type, safe)
            for enum_type in self.editor.enum_types.get_required_enum_types(models_to_create)
        ]
        statements += [
            statement
            for model in models_to_create
            for statement in self.get_schema_objects_before_table_sqls(model, safe)
        ]

        model_table_keys = frozenset((model._meta.schema, model._meta.db_table) for model in models_to_create)
        pending_tables = [self.get_model_sql_data(model, safe, model_table_keys) for model in models_to_create]
        created_table_keys = {sql_data.table_key for sql_data in pending_tables}
        many_to_many_tables_sql: list[str] = []
        created: set[tuple[str | None, str]] = set()
        while pending_tables:
            # A reference to a table hare doesn't create (a Meta.managed = False model's) never
            # holds up the order.
            next_table = next(
                (
                    sql_data
                    for sql_data in pending_tables
                    if (sql_data.references & created_table_keys) <= created | {sql_data.table_key}
                ),
                None,
            )
            if next_table is None:
                raise ConfigurationError("Can't create schema due to cyclic fk references")
            pending_tables.remove(next_table)
            created.add(next_table.table_key)
            statements.append(next_table.get_table_creation_sql())
            many_to_many_tables_sql += next_table.many_to_many_tables_sql
        statements += many_to_many_tables_sql
        statements += [
            statement
            for model in models_to_create
            for trigger in model._meta.triggers
            for statement in self.editor.trigger_statements.get_trigger_create_sqls(model, trigger, safe=safe)
        ]
        statements += [
            statement
            for model in models_to_create
            for statement in self.get_schema_objects_after_table_sqls(model, safe)
        ]
        return "\n".join(statements)

    def get_model_sql_data(
        self,
        model: type[Model],
        safe: bool = False,
        model_table_keys: frozenset[tuple[str | None, str]] = frozenset(),
    ) -> ModelSqlData:
        """Returns the DDL creating a model: its table with its indexes, constraints and comments,
        and its automatic through tables.

        Args:
            model: The model.
            safe: Whether each object is created only when it doesn't exist yet.
            model_table_keys: The ``(schema, table)`` of the other models created with it - an
                automatic through table named like one of them is that model's table, not a
                through table to create.

        Returns:
            The statements and the tables the model's table references.
        """
        schema = model._meta.schema
        table_options = model._meta.get_table_options(self.editor.client.dialect)
        if table_options is not None:
            table_options.raise_if_unsupported(model, self.editor.client.features)
        storage_table_name = (
            table_options.get_storage_table_name(model._meta.db_table) if table_options else model._meta.db_table
        )
        qualified_table_name = self.editor.qualify_table_name(storage_table_name, schema)
        table_definitions, references, composite_foreign_key_constraints = (
            self.editor.column_definitions.get_column_definitions(model)
        )

        if model._meta.has_composite_primary_key:
            # A composite primary key's fields are plain columns - the table-level PRIMARY KEY
            # names all of them.
            composite_pk_columns = [
                model._meta.fields_db_projection[name] for name in model._meta.primary_key_attribute
            ]
            if composite_pk_sql := self.get_composite_pk_constraint_sql(model, composite_pk_columns):
                table_definitions.append(composite_pk_sql)
        # Inline, as every database can create a table-level FOREIGN KEY with the table.
        table_definitions.extend(
            self.get_foreign_key_constraint_clause(constraint) for constraint in composite_foreign_key_constraints
        )
        constraint_clauses, constraint_statements = self.get_create_constraint_sqls(model, safe)
        table_definitions.extend(constraint_clauses)
        table_definitions.extend(self.get_inner_statements())

        table_create_string = self.editor.TABLE_CREATE_TEMPLATE.format(
            prefix=table_options.get_create_prefix_sql() if table_options else "",
            exists=self.get_exists_sql(safe),
            table_name=qualified_table_name,
            fields="\n    {}\n".format(",\n    ".join(table_definitions)),
            comment=self.editor.table_comments.get_table_comment_sql(
                table=qualified_table_name, comment=model._meta.table_description
            )
            if model._meta.table_description
            else "",
            extra=self.table_generate_extra(table=model._meta.db_table)
            + (table_options.get_create_suffix_sql(model, self.editor.quote) if table_options else ""),
        )
        table_create_string = "\n".join(
            [
                table_create_string,
                # First: what follows names the model's table, which may be this one.
                *(
                    table_options.get_companion_table_sqls(
                        model, self.editor.quote, self.editor.client, replaces=False
                    )
                    if table_options
                    else ()
                ),
                *(
                    table_options.get_after_create_sqls(
                        model, qualified_table_name, self.editor.quote, self.editor.client.features
                    )
                    if table_options
                    else ()
                ),
                *self.editor.table_partitions.get_partition_create_sqls(model, safe),
                *self.get_model_index_sqls(model, safe),
            ]
        )
        table_create_string += self.post_table_hook()

        many_to_many_tables_sql = []
        for many_to_many_field_name in sorted(model._meta.many_to_many_fields):
            many_to_many_field = cast(
                "ManyToManyFieldInstance[Model]", model._meta.fields_map[many_to_many_field_name]
            )
            if (many_to_many_field.through_schema or schema, many_to_many_field.through) in model_table_keys:
                continue
            if many_to_many_create_string := self.get_many_to_many_table_definition(model, many_to_many_field, safe):
                many_to_many_tables_sql.append(many_to_many_create_string)

        return ModelSqlData(
            table_key=(schema, model._meta.db_table),
            model=model,
            table_sql=table_create_string,
            constraint_sqls=constraint_statements,
            references=references,
            many_to_many_tables_sql=many_to_many_tables_sql,
        )

    def get_models_to_create(self) -> list[type[Model]]:
        """Returns every registered model this connection creates the table of - not a
        ``Meta.managed = False`` one, whose table is never hare's, and not a swapped one, which has
        none. A client of a tenant schema creates the ``Meta.tenant_schema`` models, the
        connection's own client the others.

        Returns:
            The models.

        Raises:
            ConfigurationError: A ``Meta.tenant_schema`` model's connection has no
                ``tenant_schema_template``.
        """
        from hare import Hare
        from hare.core.hare_context import HareContext

        context = HareContext.get_current()
        apps = context.apps if context is not None and context._inited and context.apps is not None else Hare.apps
        if not apps:
            return []
        models_to_create = []
        in_tenant_schema = self.editor.client.tenant_schema is not None
        for model in apps.get_models_iterable():
            if model._meta.default_connection != self.editor.client.connection_alias:
                continue
            if model._meta.tenant_schema and self.editor.client.tenant_schema_template is None:
                raise model._meta.get_missing_tenant_schema_error(self.editor.client)
            if model._meta.tenant_schema is not in_tenant_schema:
                continue
            InstanceChecks.check(model)
            if model._meta.managed is not False and model._meta.swapped is None:
                models_to_create.append(model)
        return models_to_create

    def get_many_to_many_table_definition(
        self, model: type[Model], field: ManyToManyFieldInstance[Model], safe: bool = False
    ) -> str | None:
        """Returns the statements creating an automatic through table: the table, its unique index
        over both sides for a ``unique`` field, and the indexes over its key columns.

        Args:
            model: The model declaring the M2M field.
            field: The M2M field.
            safe: Whether each object is created only when it doesn't exist yet.

        Returns:
            The statements, None for a field without an automatic through table (a ``through=``
            model has its own table, the generated backward field shares the forward one's).
        """
        if field._generated or field.through_model is not None:
            return None
        related_model = field.related_model
        if not related_model:
            return None
        many_to_many_schema = model._meta.schema
        through_table_name = field.through
        qualified_through = self.editor.qualify_table_name(through_table_name, many_to_many_schema)

        # Without a database constraint (creates_foreign_key) the through-table's FK columns get
        # no FK constraint at all.
        backward_columns, backward_constraint = self.get_many_to_many_side_columns(
            through_table_name,
            field.backward_keys,
            model._meta,
            field.db_on_delete,
            self.editor.foreign_key_rebuild.creates_foreign_key(field),
        )
        forward_columns, forward_constraint = self.get_many_to_many_side_columns(
            through_table_name,
            field.forward_keys,
            related_model._meta,
            field.db_on_delete,
            self.editor.foreign_key_rebuild.creates_foreign_key(field),
        )
        column_lines = [*backward_columns, *forward_columns]
        column_lines.extend(
            self.get_foreign_key_constraint_clause(constraint)
            for constraint in (backward_constraint, forward_constraint)
            if constraint
        )
        fields_string = "\n    {}\n".format(",\n    ".join(column_lines))
        many_to_many_create_string = self.editor.MANY_TO_MANY_TABLE_TEMPLATE.format(
            exists=self.get_exists_sql(safe),
            table_name=qualified_through,
            fields=fields_string,
            extra=self.table_generate_extra(table=through_table_name),
            comment=self.editor.table_comments.get_table_comment_sql(
                table=qualified_through, comment=field.description
            )
            if field.description
            else "",
        )
        many_to_many_create_string += self.post_table_hook()
        if field.unique and self.editor.client.features.supports_unique_constraints:
            many_to_many_create_string += "\n" + self.editor.index_statements.get_unique_index_sql(
                through_table_name, [*field.backward_keys, *field.forward_keys], schema=many_to_many_schema, safe=safe
            )
        for index_keys in field.get_through_index_keys():
            many_to_many_create_string += "\n" + self.editor.index_statements.get_table_index_sql(
                through_table_name, index_keys, schema=many_to_many_schema, safe=safe
            )
        return many_to_many_create_string

    def get_many_to_many_side_columns(
        self,
        through_table_name: str,
        side_keys: tuple[str, ...],
        target_meta: MetaInfo,
        on_delete: str,
        db_constraint: bool,
    ) -> tuple[list[str], ForeignKeyConstraint | None]:
        """The column definitions of one side of a many-to-many through table: one column with an
        inline ``REFERENCES`` for a single-column key, or a plain column per key part plus a
        table-level composite ``FOREIGN KEY`` constraint.
        """
        from hare.query.key_columns import KeyColumns

        pk_fields = target_meta.pk_fields if target_meta.has_composite_primary_key else (target_meta.pk,)
        pk_columns = KeyColumns.get_source_columns(target_meta)
        target_table = self.editor.qualify_table_name(target_meta.db_table, target_meta.schema)
        # on_delete=SET_NULL needs nullable columns - on both sides, as the backward field has the
        # same on_delete.
        nullability = "" if on_delete == OnDelete.SET_NULL else " NOT NULL"

        if len(side_keys) == 1:
            column_type = self.editor.column_definitions.get_pk_column_type(pk_fields[0])
            inline_foreign_key = ""
            if db_constraint:
                inline_foreign_key = self.editor.column_definitions.get_foreign_key_reference_string(
                    "", side_keys[0], target_table, pk_columns[0], on_delete, ""
                )
            return [f"{self.editor.quote(side_keys[0])} {column_type}{nullability}{inline_foreign_key}"], None

        columns = [
            f"{self.editor.quote(key)} {self.editor.column_definitions.get_pk_column_type(pk_field)}{nullability}"
            for key, pk_field in zip(side_keys, pk_fields, strict=True)
        ]
        constraint = None
        if db_constraint:
            constraint = ForeignKeyConstraint(
                fields=side_keys,
                to_table=target_table,
                to_fields=pk_columns,
                on_delete=on_delete,
                name=GeneratedNames.get_foreign_key_name(
                    through_table_name, side_keys, target_meta.db_table, pk_columns
                ),
            )
        return columns, constraint

    def get_inner_statements(self) -> list[str]:
        return []

    def table_generate_extra(self, table: str) -> str:
        return ""

    def post_table_hook(self) -> str:
        return ""

    def get_create_constraint_sqls(self, model: type[Model], safe: bool = False) -> tuple[list[str], list[str]]:
        """Returns the DDL of a new table's ``Meta.constraints``. A constraint goes into the ``CREATE
        TABLE`` body. A unique constraint is instead a unique index created after the table - only
        when missing, with ``safe`` - where the database can't drop a table constraint in place, and
        wherever it has a condition. A database without unique constraints gets none.

        Args:
            model: The model.
            safe: Whether each object is created only when it doesn't exist yet.

        Returns:
            The clauses inside ``CREATE TABLE`` and the statements after it.

        Raises:
            ConfigurationError: An entry of ``Meta.constraints`` isn't a constraint.
            UnSupportedError: A constraint the dialect has no DDL for.
        """
        dialect = self.editor.client.dialect
        qualified_table_name = self.editor.qualify_table_name(model._meta.db_table, model._meta.schema)
        table_clauses: list[str] = []
        statements: list[str] = []
        for constraint in model._meta.constraints:
            if isinstance(constraint, CheckConstraint):
                constraint_sql = self.editor.CHECK_CONSTRAINT_CREATE_TEMPLATE.format(
                    name=self.editor.quote(constraint.name),
                    check=ConstraintCondition.get_sql(constraint.check, model, self.editor.client),
                )
            elif isinstance(constraint, ExclusionConstraint):
                constraint_sql = self.editor.constraint_statements.exclusion_constraint_sql(model, constraint)
            elif isinstance(constraint, UniqueConstraint):
                if not dialect.features.supports_unique_constraints:
                    continue
                self.editor.constraint_statements.check_unique_constraint_supported(constraint)
                constraint_column_names = model._meta.get_column_names(constraint.fields)
                index_name = constraint.name or GeneratedNames.get_index_name(
                    GeneratedNamePrefix.UNIQUE_CONSTRAINT, model, constraint_column_names
                )
                quoted_columns = self.editor.column_definitions.get_key_columns_sql(
                    constraint_column_names, constraint.without_overlaps
                )
                include_sql = constraint.get_include_sql(model, dialect, self.editor.quote)
                if constraint.condition or not dialect.features.supports_adding_constraints:
                    where_sql = (
                        f" WHERE ({ConstraintCondition.get_sql(constraint.condition, model, self.editor.client)})"
                        if constraint.condition
                        else ""
                    )
                    statements.append(
                        f"CREATE UNIQUE INDEX {self.get_exists_sql(safe)}{self.editor.quote(index_name)} "
                        f"ON {qualified_table_name} "
                        f"({quoted_columns}){include_sql}{constraint.get_nulls_sql(dialect)}{where_sql}"
                    )
                    continue
                constraint_sql = self.editor.UNIQUE_CONSTRAINT_CREATE_TEMPLATE.format(
                    index_name=self.editor.quote(index_name),
                    nulls=constraint.get_nulls_sql(dialect),
                    fields=quoted_columns,
                    include=include_sql,
                )
                if constraint.deferrable:
                    constraint_sql += " DEFERRABLE INITIALLY " + (
                        "DEFERRED" if constraint.initially_deferred else "IMMEDIATE"
                    )
            else:  # pragma: nocoverage
                raise ConfigurationError(f"Unsupported Meta.constraints entry: {constraint!r}")
            table_clauses.append(constraint_sql)
        return table_clauses, statements

    def get_model_index_sqls(self, model: type[Model], safe: bool = False) -> list[str]:
        """Returns the statements creating a model's indexes - ``index=True`` fields and
        ``Meta.indexes`` - each once.

        Args:
            model: The model.
            safe: Whether each index is created only when it doesn't exist yet.

        Returns:
            The statements.
        """
        index_sqls = [
            self.editor.index_statements.get_index_sql(model, list(column_names), safe=safe)
            for _field_name, column_names in model._meta.get_field_index_columns()
        ]
        for index in model._meta.indexes:
            if isinstance(index, Index):
                index_sqls.append(index.get_sql(self.editor, model, safe))
            else:
                index_sqls.append(
                    self.editor.index_statements.get_index_sql(model, model._meta.get_column_names(index), safe=safe)
                )
        return [index_sql for index_sql in dict.fromkeys(index_sqls) if index_sql]

    def get_required_extensions(self, models: Sequence[type[Model]]) -> list[str]:
        """Returns every extension the models need - ``Meta.extensions`` entries, the extension of
        a field type an extension provides (``CitextField``), and the operator class extension of
        an ``ExclusionConstraint`` - each once, in first-use order.

        Args:
            models: The models.

        Returns:
            The extension names.
        """
        extensions: dict[str, None] = {}
        for model in models:
            for extension in model._meta.extensions:
                extensions[extension] = None
            for field in model._meta.fields_map.values():
                if field_extension := field.get_required_extension(self.editor.client.dialect):
                    extensions[field_extension] = None
            for constraint in model._meta.constraints:
                if isinstance(constraint, ExclusionConstraint) and (
                    constraint_extension := self.editor.constraint_statements.get_exclusion_constraint_extension(
                        constraint, model._meta.fields_map
                    )
                ):
                    extensions[constraint_extension] = None
                if (
                    isinstance(constraint, UniqueConstraint)
                    and constraint.without_overlaps
                    and (
                        key_extension := self.editor.constraint_statements.get_without_overlaps_extension(
                            constraint.fields, model._meta.fields_map
                        )
                    )
                ):
                    extensions[key_extension] = None
            if model._meta.pk_without_overlaps and (
                key_extension := self.editor.constraint_statements.get_without_overlaps_extension(
                    model._meta.primary_key_attribute, model._meta.fields_map
                )
            ):
                extensions[key_extension] = None
        return list(extensions)

    def get_foreign_key_constraint_clause(self, constraint: ForeignKeyConstraint) -> str:
        """Returns a table-level ``CONSTRAINT ... FOREIGN KEY`` clause.

        Args:
            constraint: The foreign key constraint.

        Returns:
            The clause.
        """
        return self.editor.FOREIGN_KEY_CONSTRAINT_CREATE_TEMPLATE.format(
            name=self.editor.quote(constraint.name),
            fields=", ".join(self.editor.quote(field_name) for field_name in constraint.fields),
            table=constraint.to_table,
            to_fields=", ".join(self.editor.quote(field_name) for field_name in constraint.to_fields),
            on_delete=constraint.on_delete,
        )

    def get_composite_foreign_key_constraint(
        self, model: type[Model], foreign_key_field: ForeignKeyFieldInstance[Model]
    ) -> ForeignKeyConstraint:
        """Table-level ForeignKeyConstraint for a composite-target FK/O2O - called once per
        logical field, not per shadow column."""
        to_fields = tuple(
            to_field.source_field or to_field.model_field_name for to_field in foreign_key_field.to_field_instances
        )
        related_table = foreign_key_field.related_model._meta.db_table
        return ForeignKeyConstraint(
            fields=foreign_key_field.source_fields,
            to_table=self.editor.qualify_table_name(related_table, foreign_key_field.related_model._meta.schema),
            to_fields=to_fields,
            on_delete=foreign_key_field.db_on_delete,
            name=GeneratedNames.get_foreign_key_name(
                model._meta.db_table, foreign_key_field.source_fields, related_table, to_fields
            ),
        )

    def get_single_column_foreign_key_constraint(
        self, model: type[Model], key_field_name: str
    ) -> ForeignKeyConstraint:
        """The table-level ``FOREIGN KEY`` of a single-column relation - the constraint its inline
        ``REFERENCES`` clause makes, under the same name.

        Args:
            model: The model declaring the relation.
            key_field_name: The relation's key field.

        Returns:
            The constraint.
        """
        key_field = model._meta.fields_map[key_field_name]
        foreign_key_field = cast("ForeignKeyFieldInstance[Model]", key_field.reference)
        db_field = model._meta.fields_db_projection[key_field_name]
        to_field_name = (
            foreign_key_field.to_field_instance.source_field or foreign_key_field.to_field_instance.model_field_name
        )
        related_model = foreign_key_field.related_model
        return ForeignKeyConstraint(
            fields=(db_field,),
            to_table=self.editor.qualify_table_name(related_model._meta.db_table, related_model._meta.schema),
            to_fields=(to_field_name,),
            on_delete=foreign_key_field.db_on_delete,
            name=GeneratedNames.get_foreign_key_name(
                model._meta.db_table, (db_field,), related_model._meta.db_table, (to_field_name,)
            ),
        )

    def get_unique_constraint_name(self, model: type[Model], field_names: list[str]) -> str:
        return GeneratedNames.get_index_name(GeneratedNamePrefix.UNIQUE_CONSTRAINT, model, field_names)

    def get_composite_pk_constraint_sql(self, model: type[Model], field_names: list[str]) -> str:
        """The table-level ``PRIMARY KEY`` of a composite primary key, among the column definitions.

        Args:
            model: The model.
            field_names: The key's columns.

        Returns:
            The constraint; ``""`` for a dialect writing the key elsewhere in the ``CREATE TABLE``.

        Raises:
            UnSupportedError: The key is ``without_overlaps`` and the server can't enforce it.
        """
        if model._meta.pk_without_overlaps and not self.editor.client.features.supports_without_overlaps:
            raise UnSupportedError(
                f"CompositePrimaryKey(without_overlaps=True) of {model.__name__} is not supported by the "
                f"{self.editor.client.dialect} server of this connection"
            )
        return self.editor.PRIMARY_KEY_CONSTRAINT_CREATE_TEMPLATE.format(
            index_name=self.editor.quote(
                GeneratedNames.get_index_name(GeneratedNamePrefix.PRIMARY_KEY, model, field_names)
            ),
            fields=self.editor.column_definitions.get_key_columns_sql(field_names, model._meta.pk_without_overlaps),
        )

    @staticmethod
    def get_exists_sql(safe: bool) -> str:
        """Returns the ``IF NOT EXISTS`` of a statement creating an object only when it's missing.

        Args:
            safe: Whether the object is created only when it doesn't exist yet.

        Returns:
            The clause with its trailing space, or an empty string.
        """
        return "IF NOT EXISTS " if safe else ""

    def get_schema_objects_before_table_sqls(self, model: type[Model], safe: bool = False) -> list[str]:
        """The statements creating what a model's table needs before it - its sequences, a column
        default may take from one.

        Args:
            model: The model.
            safe: Create each object only when it doesn't exist yet.

        Returns:
            The statements.

        Raises:
            UnSupportedError: The model declares sequences the dialect doesn't have.
        """
        if model._meta.sequences:
            self.editor.raise_if_unsupported("supports_sequences", "sequences")
        return [
            statement
            for sequence in model._meta.sequences
            for statement in self.editor.sequences.get_sequence_create_sqls(model, sequence, safe)
        ]

    def get_schema_objects_after_table_sqls(self, model: type[Model], safe: bool = False) -> list[str]:
        """The statements creating what a model declares beside its table once the table exists:
        its sequences' owner columns, functions, dictionaries, views, materialized views, row level security,
        policies and grants - in that order, each may use the ones before.

        Args:
            model: The model.
            safe: Create each object only when it doesn't exist yet.

        Returns:
            The statements.

        Raises:
            UnSupportedError: The model declares an object the dialect doesn't have.
        """
        meta = model._meta
        declared_types = (
            (meta.sequences, "supports_sequences", "sequences"),
            (meta.functions, "supports_database_functions", "database functions"),
            (meta.dictionaries, "supports_dictionaries", "dictionaries"),
            (meta.views, "supports_views", "views"),
            (meta.materialized_views, "supports_materialized_views", "materialized views"),
            (meta.row_level_security, "supports_row_level_security", "row level security policies"),
            (meta.policies, "supports_row_level_security", "row level security policies"),
            (meta.grants, "supports_grants", "grants"),
        )
        for declared, feature, objects in declared_types:
            if declared:
                self.editor.raise_if_unsupported(feature, objects)
        statements = [
            statement
            for sequence in meta.sequences
            for statement in self.editor.sequences.get_sequence_owner_sqls(model, sequence)
        ]
        statements += [
            statement
            for function in meta.functions
            for statement in self.editor.database_functions.get_function_create_sqls(model, function, safe)
        ]
        statements += [
            statement
            for dictionary in meta.dictionaries
            for statement in self.editor.dictionaries.get_dictionary_create_sqls(model, dictionary, safe)
        ]
        statements += [
            statement for view in meta.views for statement in self.editor.views.get_view_create_sqls(model, view, safe)
        ]
        statements += [
            statement
            for view in meta.materialized_views
            for statement in self.editor.materialized_views.get_materialized_view_create_sqls(model, view, safe)
        ]
        if meta.row_level_security is not None:
            statements += self.editor.row_level_security_policies.get_row_level_security_sqls(
                model, None, meta.row_level_security
            )
        statements += [
            statement
            for policy in meta.policies
            for statement in self.editor.row_level_security_policies.get_policy_create_sqls(model, policy, safe)
        ]
        statements += [
            statement for grant in meta.grants for statement in self.editor.grants.get_grant_sqls(model, grant)
        ]
        return statements
