from __future__ import annotations

from collections.abc import Callable, Collection
from typing import TYPE_CHECKING, cast

from hare.exceptions import UnSupportedError
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.inspectdb.exceptions import SchemaNotFoundError
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.inspectdb.introspection.enums import SchemaPartType
from hare.inspectdb.introspection.schema_part import SchemaPart
from hare.migrations.autodetection.required_extensions import RequiredExtensions
from hare.migrations.drift.constants import (
    DRIFT_NAMED_OBJECT_OPERATIONS,
    DRIFT_SCHEMA_OBJECT_PART_TYPES,
    DRIFT_UNMANAGED_LEFT_OUT_OPTIONS,
)
from hare.migrations.drift.drift_result import DriftResult
from hare.migrations.drift.shadow.schema_comparison import SchemaComparison
from hare.migrations.drift.shadow.shadow_state import ShadowState
from hare.migrations.loading.recorder.noop_recorder import NoopRecorder
from hare.migrations.operations import CreateExtension, CreateSchema, HareOperation
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.models.enums import ModelOption
from hare.transactions.constants import DISTRIBUTED_DECISIONS_TABLE_NAME

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.inspectdb.introspection.schema_introspector import SchemaIntrospector

ModelKey = tuple[str, str]


class ShadowDrift:
    """Drift by a shadow: the schema the models declare is built beside the database - by the same path
    ``generate_schemas`` takes - then both are read by the connection's introspector and compared part by
    part (``SchemaComparison``).

    Args:
        connection: The connection whose database is compared.
        new_state: The state the models declare.
        target_labels: The apps whose models are compared.
        schema: The schema swept for untracked tables - the connection's default one when None.
        unmanaged_model_states: The ``Meta.managed = False`` models' states - their tables count as known.
        journal_tables: The tables of migration journals besides ``hare_migrations``.
    """

    def __init__(
        self,
        connection: DatabaseClient,
        new_state: State,
        target_labels: list[str],
        schema: str | None,
        unmanaged_model_states: list[ModelState] | None,
        journal_tables: Collection[str],
    ) -> None:
        self.connection = connection
        self.new_state = new_state
        self.target_labels = target_labels
        self.schema = schema
        self.unmanaged_model_states = unmanaged_model_states or []
        self.journal_tables = journal_tables
        self.default_schema = ""
        #: On a connection to a tenant's schema, the schema of the models without Meta.tenant_schema.
        self.shared_schema: str | None = None
        #: The schemas of the compared models the database doesn't have.
        self.missing_schemas: list[str] = []

    async def detect(self) -> DriftResult:
        """Compares the database with the models.

        Returns:
            The operations, untracked tables, untracked columns and column mismatches found.

        Raises:
            UnSupportedError: The connection's dialect has no shadow database.
        """
        connection = self.connection
        dialect = connection.dialect
        shadow_database_class = dialect.shadow_database_class
        if shadow_database_class is None:
            raise UnSupportedError(f"hare drift is not supported on {dialect}")
        introspector_class = DatabaseCatalog.get_introspector_class(connection)
        self.default_schema = await DatabaseCatalog.get_default_schema(connection)
        self.shared_schema = await self.get_shared_schema()
        compared, known_tables = self.get_compared_models()
        built = self.get_built_model_states()
        real_parts = await self.read_parts(connection, introspector_class, compared, lambda schema: schema)
        schemas = {self.get_model_schema(model_state) for _key, model_state in built}
        async with shadow_database_class(connection, schemas) as shadow:
            shadow_client = cast("DatabaseClient", shadow.client)
            shadow_state = ShadowState.get(built, shadow)
            models = [shadow_state.apps.get_model(f"{app_label}.{name}") for (app_label, name), _state in built]
            shadow_editor = dialect.schema_editor_class(shadow_client)
            await shadow_client.execute_built_script(
                shadow_editor.table_creation.get_create_schema_sql(safe=False, models=models)
            )
            shadow_parts = [
                part.with_texts_replaced(shadow.replace_shadow_names)
                for part in await self.read_parts(
                    shadow_client, introspector_class, compared, shadow.get_shadow_schema_name, reading_shadow=True
                )
            ]
        comparison = SchemaComparison(
            self.new_state, dialect.schema_editor_class(connection), real_parts, shadow_parts
        )
        for key, model_state in compared:
            prefix = self.get_table_prefix(self.get_model_schema(model_state))
            comparison.compare_model(key, model_state, self.get_tables(model_state), prefix)
            comparison.compare_schema_objects(key, prefix)
        comparison.compare_enum_types()
        operations: list[HareOperation] = [
            *(CreateSchema(schema_name=schema) for schema in self.missing_schemas),
            *await self.get_extension_operations(introspector_class, compared),
            *comparison.operations,
        ]
        untracked_tables = await self.get_untracked_tables(known_tables)
        return DriftResult(
            operations=operations,
            untracked_tables=untracked_tables,
            untracked_columns=sorted(comparison.untracked_columns),
            mismatched_columns=comparison.mismatched_columns,
        )

    async def get_shared_schema(self) -> str | None:
        """The schema of the models without ``Meta.tenant_schema`` on a connection to a tenant's schema -
        the default one of the connection outside the tenant's; None on another connection."""
        if self.connection.tenant_schema is None:
            return None
        # Local import: the connections import the dialects.
        from hare.core.connections.connections import Connections

        shared_client = Connections.current().create_independent(self.connection.connection_alias)
        try:
            return await DatabaseCatalog.get_default_schema(shared_client)
        finally:
            await shared_client.close()

    def get_model_schema(self, model_state: ModelState) -> str:
        """The schema a model's table is in - its ``Meta.schema`` (a schema, an attached database or
        a database of the server - the namespace of tables the database has), else the connection's default one, the
        shared one for a model without ``Meta.tenant_schema`` on a connection to a tenant's schema."""
        if schema := model_state.options.get(ModelOption.SCHEMA):
            return schema
        if self.shared_schema is not None and not model_state.options.get(ModelOption.TENANT_SCHEMA):
            return self.shared_schema
        return self.default_schema

    def get_table_prefix(self, schema: str) -> str:
        """What names a table of a schema among the parts of every schema - nothing in the default one."""
        return "" if schema == self.default_schema else f"{schema}."

    def get_tables(self, model_state: ModelState) -> list[str]:
        """A model's tables - the one storing its rows, then one over it (a table spread over a
        cluster's servers)."""
        table_options = self.connection.dialect.get_declared_table_options(
            model_state.options.get(ModelOption.TABLE_OPTIONS, ())
        )
        storage_table = (
            model_state.table if table_options is None else table_options.get_storage_table_name(model_state.table)
        )
        return list(dict.fromkeys((storage_table, model_state.table)))

    def get_through_tables(self, key: ModelKey) -> list[str]:
        """The automatic through tables of a model's many-to-many relations."""
        model = self.new_state.apps.get_model(f"{key[0]}.{key[1]}")
        return [
            field.through
            for field in model._meta.fields_map.values()
            if isinstance(field, ManyToManyFieldInstance) and not field._generated and field.through_model is None
        ]

    def get_compared_models(self) -> tuple[list[tuple[ModelKey, ModelState]], set[tuple[str, str]]]:
        """The models compared, and the (schema, table) of the others - known, not untracked.

        Returns:
            The compared models by their (app, name), and the known tables.
        """
        # On a connection with a schema per tenant, the shared schema holds the models without
        # Meta.tenant_schema and a tenant's schema the ones with it - the others are compared there.
        tenant_schema_side = (
            True
            if self.connection.tenant_schema is not None
            else False
            if self.connection.tenant_schema_template is not None
            else None
        )
        compared: list[tuple[ModelKey, ModelState]] = []
        known_tables: set[tuple[str, str]] = set()
        for key, model_state in self.new_state.models.items():
            schema = self.get_model_schema(model_state)
            known_tables.update((schema, table) for table in self.get_tables(model_state))
            known_tables.update((schema, table) for table in self.get_through_tables(key))
            if key[0] not in self.target_labels:
                continue
            if (
                tenant_schema_side is not None
                and bool(model_state.options.get(ModelOption.TENANT_SCHEMA)) is not tenant_schema_side
            ):
                continue
            compared.append((key, model_state))
        for unmanaged_model_state in self.unmanaged_model_states:
            schema = self.get_model_schema(unmanaged_model_state)
            known_tables.add((schema, unmanaged_model_state.table))
            # The automatic through tables of its many-to-many relations are its own too.
            known_tables.update(
                (schema, field.through)
                for field in unmanaged_model_state.fields.values()
                if isinstance(field, ManyToManyFieldInstance) and field.through_model is None and field.through
            )
        return compared, known_tables

    def get_built_model_states(self) -> list[tuple[ModelKey, ModelState]]:
        """The models the shadow is built of - every model of the state, and the tables of the unmanaged
        ones a relation may point at, without the objects they declare beside them."""
        built = list(self.new_state.models.items())
        built.extend(
            ((model_state.app, model_state.name), self.get_table_only(model_state))
            for model_state in self.unmanaged_model_states
            if (model_state.app, model_state.name) not in self.new_state.models
        )
        if self.shared_schema is None:
            return built
        # The shared models are built in the shadow of their own schema, not the tenant's.
        placed: list[tuple[ModelKey, ModelState]] = []
        for key, model_state in built:
            schema = self.get_model_schema(model_state)
            if schema != self.default_schema and not model_state.options.get(ModelOption.SCHEMA):
                model_state = model_state.clone()
                model_state.options[ModelOption.SCHEMA] = schema
            placed.append((key, model_state))
        return placed

    @staticmethod
    def get_table_only(model_state: ModelState) -> ModelState:
        """An unmanaged model's state without the objects it declares beside its table.

        Args:
            model_state: The model's state.

        Returns:
            The state, its table alone.
        """
        table_state = model_state.clone()
        table_state.options = {
            name: value for name, value in model_state.options.items() if name not in DRIFT_UNMANAGED_LEFT_OUT_OPTIONS
        }
        return table_state

    async def read_parts(
        self,
        client: DatabaseClient,
        introspector_class: type[SchemaIntrospector],
        compared: list[tuple[ModelKey, ModelState]],
        get_schema: Callable[[str], str | None],
        *,
        reading_shadow: bool = False,
    ) -> list[SchemaPart]:
        """The parts of the compared models' tables and objects, each table named as among every schema.

        Args:
            client: The database's client, or the shadow's.
            introspector_class: The connection's introspector.
            compared: The compared models.
            get_schema: The schema read for a schema of the models.
            reading_shadow: Whether the shadow is read - a schema missing from it is no news.

        Returns:
            The parts.
        """
        models_by_schema: dict[str, list[tuple[ModelKey, ModelState]]] = {}
        for key, model_state in compared:
            models_by_schema.setdefault(self.get_model_schema(model_state), []).append((key, model_state))
        parts: list[SchemaPart] = []
        for schema, schema_models in models_by_schema.items():
            read_schema = get_schema(schema) or schema
            try:
                existing_tables = set(await introspector_class.fetch_table_names(client, read_schema, True))
            except SchemaNotFoundError:
                # Where migrations don't create schemas, a missing one is the connection's setting to fix.
                if not self.connection.features.supports_schemas:
                    raise
                if not reading_shadow:
                    self.missing_schemas.append(schema)
                continue
            wanted_tables: list[str] = []
            object_names: dict[SchemaPartType, set[str]] = {}
            for key, model_state in schema_models:
                wanted_tables.extend((*self.get_tables(model_state), *self.get_through_tables(key)))
                model = self.new_state.apps.get_model(f"{key[0]}.{key[1]}")
                for part_type in DRIFT_SCHEMA_OBJECT_PART_TYPES:
                    attribute_name = DRIFT_NAMED_OBJECT_OPERATIONS[part_type][0]
                    object_names.setdefault(part_type, set()).update(
                        declaration.name for declaration in getattr(model._meta, attribute_name)
                    )
            tables = [table for table in dict.fromkeys(wanted_tables) if table in existing_tables]
            schema_parts = await introspector_class.fetch_schema_parts(client, read_schema, tables, object_names)
            prefix = self.get_table_prefix(schema)
            parts.extend(
                SchemaPart(
                    part.part_type,
                    f"{prefix}{part.table}" if part.table else part.table,
                    part.name if part.table else f"{prefix}{part.name}",
                    part.definition,
                    part.value,
                )
                for part in schema_parts
            )
        return parts

    async def get_extension_operations(
        self, introspector_class: type[SchemaIntrospector], compared: list[tuple[ModelKey, ModelState]]
    ) -> list[HareOperation]:
        """The extensions the compared models need that the database lacks.

        Args:
            introspector_class: The connection's introspector.
            compared: The compared models.

        Returns:
            Their ``CreateExtension`` operations.
        """
        required: set[str] = set()
        for _key, model_state in compared:
            required |= RequiredExtensions.get_for_model_state(model_state, [self.connection.dialect])
        if not required:
            return []
        existing = await introspector_class.fetch_extensions(self.connection)
        return [CreateExtension(extension_name=name) for name in sorted(required - existing)]

    async def get_untracked_tables(self, known_tables: set[tuple[str, str]]) -> list[str]:
        """The tables of the swept schema no model, journal or hare's own bookkeeping is.

        Args:
            known_tables: The (schema, table) of the models.

        Returns:
            The tables, sorted.
        """
        swept_schema = self.default_schema if self.schema is None else self.schema
        real_tables = set(await DatabaseCatalog.get_table_names(self.connection, schema=swept_schema))
        known = {table for schema, table in known_tables if schema == swept_schema}
        known |= {NoopRecorder().table_name, DISTRIBUTED_DECISIONS_TABLE_NAME, *self.journal_tables}
        return sorted(real_tables - known)
