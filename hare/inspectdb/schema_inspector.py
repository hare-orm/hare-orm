from __future__ import annotations

from typing import TYPE_CHECKING

from hare.ddl.enums import GrantTarget
from hare.ddl.schema_objects.database_sequence import DatabaseSequence
from hare.inspectdb.generation.declarations import ModelGenerationOptions
from hare.inspectdb.generation.many_to_many_tables import ManyToManyTables
from hare.inspectdb.generation.model_naming import ModelNaming
from hare.inspectdb.generation.model_source_builder import ModelSourceBuilder
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.migrations.writer.import_manager import ImportManager
from hare.migrations.writer.migration_writer import MigrationWriter

if TYPE_CHECKING:
    from hare.ddl.schema_objects.database_function import DatabaseFunction
    from hare.ddl.security.grant import Grant
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect
    from hare.inspectdb.introspection.table_info import TableInfo
from hare.inspectdb.exceptions import DuplicateModelClassNameError


class SchemaInspector:
    """Reads an existing database's schema and generates hare-orm model source for it - what
    `hare inspectdb` prints. A thin orchestrator over SchemaIntrospector (introspection) and
    ModelSourceBuilder (source rendering)."""

    @staticmethod
    async def inspect(connection: DatabaseClient, tables: list[str] | None = None, schema: str | None = None) -> str:
        """Generates hare-orm model source for the given tables (or every table in the schema, if
        omitted), as one importable module.

        Args:
            connection: The connection to the database to read.
            tables: The tables to generate models for; every table of ``schema`` when omitted.
            schema: The schema to inspect (an attached database on SQLite, a database of the server
                where databases are the schemas), the connection's default one when None.

        Returns:
            The module source.

        Raises:
            UnsupportedDialectError: The connection's dialect has no introspector.
            SchemaNotFoundError: ``schema`` doesn't exist.
            TableNotFoundError: A table in ``tables`` doesn't exist.
            DuplicateModelClassNameError: If two distinct tables in this run derive the same
                Python class name (e.g. two tables differing only by case, legal as distinct
                tables under Postgres's case-sensitive quoted-identifier rules) - generating both
                would silently produce two "class Foo(Model):" definitions back to back, the
                second shadowing the first with no error/warning and that table's model quietly
                vanishing.
        """
        dialect = connection.dialect.name
        if schema is None:
            schema = await DatabaseCatalog.get_default_schema(connection)
        table_names = (
            tables if tables is not None else await DatabaseCatalog.get_table_names(connection, schema=schema)
        )
        table_name_by_class_name: dict[str, str] = {}
        for table_name in table_names:
            class_name = ModelNaming.get_class_name(table_name)
            existing_table_name = table_name_by_class_name.get(class_name)
            if existing_table_name is not None and existing_table_name != table_name:
                raise DuplicateModelClassNameError(existing_table_name, table_name, class_name)
            table_name_by_class_name[class_name] = table_name
        # A table list just fetched from the schema itself needs no existence re-check.
        table_infos = await DatabaseCatalog.inspect_tables(
            connection, table_names, schema=schema, verify_exists=tables is not None
        )
        # The views, functions, table-less sequences and other schema objects of a whole schema are
        # named, as no model is made of them.
        views = await DatabaseCatalog.get_views(connection, schema=schema) if tables is None else []
        sequences, functions, grants = (
            await DatabaseCatalog.get_unattached_schema_objects(connection, schema=schema)
            if tables is None
            else ([], [], [])
        )
        schema_object_notes = (
            await DatabaseCatalog.get_schema_object_notes(connection, schema=schema) if tables is None else []
        )
        return SchemaInspector.render_module(
            table_infos,
            dialect,
            views,
            connection.dialect,
            unattached_objects=[*sequences, *functions],
            unattached_object_grants=grants,
            schema_object_notes=schema_object_notes,
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
    def get_unattached_object_note(schema_object: DatabaseSequence | DatabaseFunction, grants: list[Grant]) -> str:
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
        object_grants = [grant for grant in grants if grant.on == target and grant.object_name == schema_object.name]
        lines = [
            f"# NOTE: {object_type} {schema_object.name!r} belongs to no table - declare it in Meta.{option} of "
            "the model using it" + (", and its grants in Meta.grants:" if object_grants else ":"),
        ]
        for declared in (schema_object, *object_grants):
            lines.extend(
                f"#     {line}" for line in MigrationWriter.render_value(declared, ImportManager()).splitlines()
            )
        return "\n".join(lines)

    @staticmethod
    def render_module(
        table_infos: list[TableInfo],
        dialect: str,
        views: list[tuple[str, bool, tuple[str, ...]]] | None = None,
        connection_dialect: Dialect | None = None,
        unattached_objects: list[DatabaseSequence | DatabaseFunction] | None = None,
        unattached_object_grants: list[Grant] | None = None,
        schema_object_notes: list[str] | None = None,
    ) -> str:
        """Renders several tables' models as one module. A class named like one of the module's imports
        gets a trailing underscore, and the relations to it follow.

        Args:
            table_infos: The tables.
            dialect: The dialect they were read from.
            views: The schema's views - each named in a note at the end, with the triggers on it.
            connection_dialect: The dialect of the connection the tables were read on.
            unattached_objects: The sequences and functions of the schema no table holds - each named in
                a note with its declaration.
            unattached_object_grants: The grants on them - each written in the note of its object.
            schema_object_notes: The notes naming the schema's other objects no model declares.

        Returns:
            The module source.
        """
        class_name_by_table_name = {
            table_info.name: ModelNaming.get_class_name(table_info.name) for table_info in table_infos
        }
        while True:
            foreign_key_target_overrides = {
                table_name: f"models.{class_name}" for table_name, class_name in class_name_by_table_name.items()
            }
            blocks = []
            module_bound_names: set[str] = set()
            module_imports = ImportManager()
            many_to_many_tables = ManyToManyTables(table_infos, foreign_key_target_overrides)
            for table_info in table_infos:
                through_table_note = many_to_many_tables.notes_by_table.get(table_info.name)
                if through_table_note is not None:
                    blocks.append(through_table_note)
                    continue
                builder = ModelSourceBuilder(
                    table_info,
                    ModelGenerationOptions(
                        dialect,
                        foreign_key_target_overrides=foreign_key_target_overrides,
                        class_name=class_name_by_table_name[table_info.name],
                        connection_dialect=connection_dialect,
                        many_to_many_fields=tuple(many_to_many_tables.fields_by_table.get(table_info.name, ())),
                    ),
                    module_imports,
                )
                blocks.append(builder.build_body())
                module_bound_names |= builder.bound_names
            shadowing_table_names = [
                table_name
                for table_name, class_name in class_name_by_table_name.items()
                if class_name in module_bound_names
            ]
            if not shadowing_table_names:
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
            for table_name in shadowing_table_names:
                taken_names = module_bound_names | set(class_name_by_table_name.values())
                class_name = class_name_by_table_name[table_name]
                while class_name in taken_names:
                    class_name = f"{class_name}_"
                class_name_by_table_name[table_name] = class_name
