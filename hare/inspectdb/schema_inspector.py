from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from hare.ddl.enums import GrantTarget
from hare.ddl.schema_objects.database_sequence import DatabaseSequence
from hare.declarations.declaration_value_serializer import DeclarationValueSerializer
from hare.declarations.import_manager import ImportManager
from hare.inspectdb.generation.declarations import ModelGenerationOptions
from hare.inspectdb.generation.inspected_model_builder import InspectedModelBuilder
from hare.inspectdb.generation.inspected_models import InspectedModels
from hare.inspectdb.generation.many_to_many_tables import ManyToManyTables
from hare.inspectdb.generation.model_naming import ModelNaming
from hare.inspectdb.generation.model_source_builder import ModelSourceBuilder
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.migrations.constants import DEFAULT_MIGRATION_TABLE_NAME
from hare.transactions.constants import DISTRIBUTED_DECISIONS_TABLE_NAME

if TYPE_CHECKING:
    from hare.ddl.schema_objects.database_function import DatabaseFunction
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect
    from hare.inspectdb.generation.inspected_model import InspectedModel
    from hare.inspectdb.introspection.composite_foreign_key_info import CompositeForeignKeyInfo
    from hare.inspectdb.introspection.foreign_key_info import ForeignKeyInfo
    from hare.inspectdb.introspection.schema_introspector import SchemaIntrospector
    from hare.inspectdb.introspection.schema_part import SchemaPart
    from hare.inspectdb.introspection.table_info import TableInfo
from hare.inspectdb.exceptions import DuplicateModelClassNameError


class SchemaInspector:
    """Reads an existing database's schema and generates hare-orm model source for it - what
    `hare inspectdb` prints. A thin orchestrator over SchemaIntrospector (introspection) and
    ModelSourceBuilder (source rendering)."""

    @staticmethod
    async def get_target_tables(
        connection: DatabaseClient, tables: list[TableInfo], schema: str | None
    ) -> list[TableInfo]:
        """Read referenced layouts without adding their models to the requested output."""
        known = {table.name for table in tables}
        pending = tables
        targets = []
        while pending:
            references: list[ForeignKeyInfo | CompositeForeignKeyInfo] = []
            for table in pending:
                references.extend(table.foreign_keys.values())
                references.extend(table.composite_foreign_keys)
            missing = {
                foreign_key.target_table
                for foreign_key in references
                if foreign_key.target_table not in known
                and getattr(foreign_key, "target_schema", None) in (None, schema)
            }
            if not missing:
                break
            pending = await DatabaseCatalog.inspect_tables(connection, sorted(missing), schema=schema)
            known.update(missing)
            targets.extend(pending)
        return targets

    @staticmethod
    def get_table_with_reference_type_notes(
        table: TableInfo, tables_by_name: dict[str, TableInfo], introspector_class: type[SchemaIntrospector]
    ) -> TableInfo:
        """Annotate reference types the reconstructed target cannot reproduce, without changing catalog data.

        A foreign key inherits its target field's storage type. When both tables are
        inspected, retain a diagnostic if the source column has a different type.
        Primary-key references may have their own identity-specific spelling.
        """
        columns = []
        changed = False
        for column in table.columns:
            foreign_key = table.foreign_keys.get(column.name)
            target = tables_by_name.get(foreign_key.target_table) if foreign_key is not None else None
            if foreign_key is not None and target is not None and not column.is_pk:
                target_schema = foreign_key.target_schema if foreign_key.target_schema is not None else table.schema
                target_column = next((item for item in target.columns if item.name == foreign_key.target_column), None)
                if target_schema == target.schema and target_column is not None:
                    reference_type = introspector_class.get_referenced_column_type(target_column)
                    source_path, _, source_ambiguous = introspector_class.map_column_type(column)
                    target_path, _, target_ambiguous = introspector_class.map_column_type(
                        replace(target_column, db_type=reference_type, is_pk=False, generates_key=False)
                    )
                    if introspector_class.column_types_differ(reference_type, column) or (
                        not source_ambiguous and not target_ambiguous and source_path != target_path
                    ):
                        reason = (
                            f"the foreign key column has type {column.db_type!r}, but the reconstructed target "
                            f"{target.name}.{target_column.name} supplies {reference_type!r}; recreating this "
                            "relation changes its column type - review the target field type before migrating"
                        )
                        column = replace(column, todo_reason="; ".join(filter(None, (column.todo_reason, reason))))
                        changed = True
            columns.append(column)
        return replace(table, columns=columns) if changed else table

    @staticmethod
    async def inspect(connection: DatabaseClient, tables: list[str] | None = None, schema: str | None = None) -> str:
        """Generates hare-orm model source for the given tables (or every table in the schema, if
        omitted), as one importable module.

        Args:
            connection: The connection to the database to read.
            tables: The tables to generate models for; every table of ``schema`` when omitted.
            schema: The schema to inspect (a schema, an attached database or a database of the
                server - the namespace of tables the database has), the connection's default one when None.

        Returns:
            The module source.

        Raises:
            UnsupportedDialectError: The connection's dialect has no introspector.
            SchemaNotFoundError: ``schema`` doesn't exist.
            TableNotFoundError: A table in ``tables`` doesn't exist.
            DuplicateModelClassNameError: If two distinct tables in this run derive the same
                Python class name (e.g. two tables differing only by case, legal as distinct
                tables where quoted names are case-sensitive) - generating both
                would silently produce two "class Foo(Model):" definitions back to back, the
                second shadowing the first with no error/warning and that table's model quietly
                vanishing.
        """
        dialect = connection.dialect.name
        if schema is None:
            schema = await DatabaseCatalog.get_default_schema(connection)
        if tables is not None:
            table_names = tables
        else:
            # hare's own bookkeeping - the migration journal, the decisions of distributed transactions - is
            # no table of the application.
            bookkeeping_tables = {DEFAULT_MIGRATION_TABLE_NAME, DISTRIBUTED_DECISIONS_TABLE_NAME}
            table_names = [
                table_name
                for table_name in await DatabaseCatalog.get_table_names(connection, schema=schema)
                if table_name not in bookkeeping_tables
            ]
        table_infos = await DatabaseCatalog.inspect_tables(connection, table_names, schema=schema)
        table_name_by_class_name: dict[str, str] = {}
        for table_info in table_infos:
            table_name = table_info.name
            class_name = ModelNaming.get_class_name(table_name)
            existing_table_name = table_name_by_class_name.get(class_name)
            if existing_table_name is not None and existing_table_name != table_name:
                raise DuplicateModelClassNameError(existing_table_name, table_name, class_name)
            table_name_by_class_name[class_name] = table_name
        target_table_infos = await SchemaInspector.get_target_tables(connection, table_infos, schema)
        # The views, functions, table-less sequences and other schema objects of a whole schema are
        # named, as no model is made of them.
        if tables is None:
            views = await DatabaseCatalog.get_views(connection, schema=schema)
            sequences, functions, grants = await DatabaseCatalog.get_unattached_schema_objects(
                connection, schema=schema
            )
            schema_object_notes = await DatabaseCatalog.get_schema_object_notes(connection, schema=schema)
        else:
            views, sequences, functions, grants, schema_object_notes = [], [], [], [], []
        return SchemaInspector.render_module(
            table_infos,
            dialect,
            views,
            connection.dialect,
            unattached_objects=[*sequences, *functions],
            unattached_object_grants=grants,
            schema_object_notes=schema_object_notes,
            target_table_infos=target_table_infos,
        )

    @staticmethod
    def get_view_note(name: str, is_materialized: bool, trigger_names: tuple[str, ...]) -> str:
        """The note naming a view inspectdb makes no model of, and how to declare it.

        Args:
            name: The view's name.
            is_materialized: Whether it is a materialized view.
            trigger_names: The triggers on it.

        Returns:
            The comment line.
        """
        view_type, option, declaration = (
            ("materialized view", "materialized_views", "MaterializedView")
            if is_materialized
            else ("view", "views", "View")
        )
        note = (
            f"# NOTE: {view_type} {name!r} isn't a model - declare it in Meta.{option} of the model it reads "
            f"({declaration}({name!r}, query=...))"
        )
        if trigger_names:
            names = ", ".join(repr(trigger_name) for trigger_name in trigger_names)
            note += f"; add its triggers {names} with a RunSQL migration"
        return note + "."

    @staticmethod
    def get_unattached_object_note(
        schema_object: DatabaseSequence | DatabaseFunction, grants: list[SchemaPart]
    ) -> str:
        """The note naming a sequence or function of no table, with its declaration to put in the Meta
        of the model using it - and the grants on it.

        Args:
            schema_object: The sequence or function.
            grants: The grants on the schema's sequences and functions.

        Returns:
            The comment lines.
        """
        is_sequence = isinstance(schema_object, DatabaseSequence)
        option, object_type, target = (
            ("sequences", "sequence", GrantTarget.SEQUENCE)
            if is_sequence
            else ("functions", "function", GrantTarget.FUNCTION)
        )
        parts = [
            part
            for part in grants
            if part.definition["on"] == target and part.definition["object_name"] == schema_object.name
        ]
        object_grants = [part.value for part in parts if part.value is not None]
        lines = [
            f"# NOTE: {object_type} {schema_object.name!r} belongs to no table - declare it in Meta.{option} of "
            "the model using it" + (", and its grants in Meta.grants:" if object_grants else ":"),
        ]
        for declared in (schema_object, *object_grants):
            lines.extend(
                f"#     {line}"
                for line in DeclarationValueSerializer.render_value(declared, ImportManager()).splitlines()
            )
        for part in parts:
            if part.value is None:
                lines.append(f"# TODO: grant requires an explicit migration: {dict(part.definition)!r}")
        return "\n".join(lines)

    @staticmethod
    def render_module(
        table_infos: list[TableInfo],
        dialect: str,
        views: list[tuple[str, bool, tuple[str, ...]]] | None = None,
        connection_dialect: Dialect | None = None,
        unattached_objects: list[DatabaseSequence | DatabaseFunction] | None = None,
        unattached_object_grants: list[SchemaPart] | None = None,
        schema_object_notes: list[str] | None = None,
        target_table_infos: list[TableInfo] | None = None,
    ) -> str:
        """Prepare all model declarations, then render one module with collision-free import aliases.

        Every class and field name is reserved before the first value is serialized;
        discovering an import never rebuilds a model or changes relation targets.

        Args:
            table_infos: The tables.
            dialect: The dialect they were read from.
            views: The schema's views - each named in a note at the end, with the triggers on it.
            connection_dialect: The dialect of the connection the tables were read on.
            unattached_objects: The sequences and functions of the schema no table holds - each named in
                a note with its declaration.
            unattached_object_grants: The grants on them - each written in the note of its object.
            schema_object_notes: The notes naming the schema's other objects no model declares.
            target_table_infos: Referenced layouts used for name mapping without emitting their models.

        Returns:
            The module source.
        """
        class_name_by_table_name = {
            table_info.name: ModelNaming.get_class_name(table_info.name) for table_info in table_infos
        }
        foreign_key_target_overrides = {
            table_name: f"models.{class_name}" for table_name, class_name in class_name_by_table_name.items()
        }
        module_imports = ImportManager()
        module_imports.reserved_names |= set(class_name_by_table_name.values())
        introspector_class = DatabaseCatalog.get_dialect_introspector_class(connection_dialect or dialect)
        many_to_many_tables = ManyToManyTables(
            table_infos,
            foreign_key_target_overrides,
            introspector_class,
        )
        tables_by_name = {table.name: table for table in (*table_infos, *(target_table_infos or []))}
        prepared: list[tuple[ModelSourceBuilder, InspectedModel] | str] = []
        for table_info in table_infos:
            through_table_note = many_to_many_tables.notes_by_table.get(table_info.name)
            if through_table_note is not None:
                prepared.append(through_table_note)
                continue
            builder = ModelSourceBuilder(
                SchemaInspector.get_table_with_reference_type_notes(table_info, tables_by_name, introspector_class),
                ModelGenerationOptions(
                    dialect,
                    foreign_key_target_overrides=foreign_key_target_overrides,
                    skip_many_to_many_through_tables=False,
                    class_name=class_name_by_table_name[table_info.name],
                    connection_dialect=connection_dialect,
                    many_to_many_fields=tuple(many_to_many_tables.fields_by_table.get(table_info.name, ())),
                ),
                module_imports,
            )
            prepared.append((builder, builder.prepare()))
        declarations = [(item[0].model_builder, item[1]) for item in prepared if not isinstance(item, str)]
        for table in target_table_infos or ():
            target_builder = InspectedModelBuilder(
                table,
                ModelGenerationOptions(
                    dialect,
                    foreign_key_target_overrides=foreign_key_target_overrides,
                    skip_many_to_many_through_tables=False,
                    connection_dialect=connection_dialect,
                ),
            )
            declarations.append((target_builder, target_builder.build()))
        InspectedModels.finalize(declarations)
        blocks = [
            item
            if isinstance(item, str)
            else (item[0].render(item[1]) if item[1].state is not None else item[1].skipped_note or "")
            for item in prepared
        ]
        import_lines = ModelSourceBuilder.get_import_lines(module_imports)
        view_notes = [
            *(SchemaInspector.get_view_note(*view) for view in views or ()),
            *(schema_object_notes or ()),
        ]
        blocks.extend(["\n".join(view_notes)] if view_notes else [])
        blocks.extend(
            SchemaInspector.get_unattached_object_note(schema_object, unattached_object_grants or [])
            for schema_object in unattached_objects or ()
        )
        return "\n".join(import_lines) + "\n\n\n" + "\n\n\n".join(blocks) + "\n"
