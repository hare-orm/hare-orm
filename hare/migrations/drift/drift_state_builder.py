from __future__ import annotations

from collections.abc import Collection

from hare.dialects.base.client.database_client import DatabaseClient
from hare.inspectdb.introspection.database_catalog import DatabaseCatalog
from hare.inspectdb.introspection.table_info import TableInfo
from hare.migrations.drift.column_mismatch import ColumnMismatch
from hare.migrations.drift.observed.column_field_names import ColumnFieldNames
from hare.migrations.drift.observed.observed_fields import ObservedFields
from hare.migrations.drift.observed.observed_indexes import ObservedIndexes
from hare.migrations.drift.observed.observed_meta_options import ObservedMetaOptions
from hare.migrations.drift.observed.observed_schema_objects import ObservedSchemaObjects
from hare.migrations.drift.observed.observed_table_options import ObservedTableOptions
from hare.migrations.drift.observed.observed_uniqueness import ObservedUniqueness
from hare.migrations.loading.recorder.noop_recorder import NoopRecorder
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.migrations.state.state_apps import StateApps
from hare.models.enums import ModelOption
from hare.transactions.constants import DISTRIBUTED_DECISIONS_TABLE_NAME


class DriftStateBuilder:
    """Builds the "as the database actually is" State that `detect_drift()` diffs the current
    models against - split out from that one call so each step (expected columns, per-table
    introspection, observed-field overrides) stays independently readable/testable."""

    @staticmethod
    async def build_database_state(
        connection: DatabaseClient,
        new_state: State,
        target_labels: list[str],
        schema: str | None,
        unmanaged_model_states: list[ModelState] | None = None,
        journal_tables: Collection[str] = (),
    ) -> tuple[State, list[str], list[tuple[str, str, str, str]], list[ColumnMismatch]]:
        """Builds the state of ``target_labels``'s models as the database has them: each field copied
        with the database's null/unique/index flags, column type, foreign key constraint and
        db_default. Types and defaults are compared normalized, and what can't be normalized counts
        as equal, so a schema made by ``migrate`` shows no drift. A type difference no field
        describes is a ColumnMismatch. A model whose table doesn't exist is left out - the diff
        reports its CreateModel. A model without Meta.schema is looked up in the connection's
        default schema.

        Args:
            connection: The connection whose database is read.
            new_state: The state the models declare.
            target_labels: The apps whose models are compared.
            schema: The schema swept for untracked tables - the default schema when None. Ignored on
                SQLite.
            unmanaged_model_states: The ``Meta.managed = False`` models' states - their tables count
                as known and aren't diffed.
            journal_tables: The tables of migration journals besides ``hare_migrations`` - a
                ``MigrationRecorder(table_name=...)`` of migrations run by the application.

        Returns:
            The database's state, the untracked tables, the untracked columns and the column
            mismatches.
        """
        introspector_class = DatabaseCatalog.get_introspector_class(connection)
        default_schema = await DatabaseCatalog.get_default_schema(connection)
        # The connection's own dialect - its column types may be a variant's, by the server's version,
        # known once the connection is open.
        dialect = connection.dialect
        if schema is None or not connection.features.supports_schemas:
            schema = default_schema
        real_table_names = set(await DatabaseCatalog.get_table_names(connection, schema=schema))
        # A model is looked up in its own schema, not the swept one; each schema's tables are listed
        # once.
        real_table_names_by_other_schema: dict[str, set[str]] = {}
        database_enum_types = await introspector_class.fetch_enum_types(connection, schema)
        db_state = State(models={}, apps=StateApps(), database_enum_types=database_enum_types)
        # hare-orm's own bookkeeping tables - the migration log (on every migrated database) and
        # Transactions.distributed()'s decision log - are real tables no configured model backs,
        # so they would otherwise show up as "untracked" on every clean, fully in-sync database.
        known_tables: set[str] = {NoopRecorder().table_name, DISTRIBUTED_DECISIONS_TABLE_NAME, *journal_tables}
        for unmanaged_model_state in unmanaged_model_states or ():
            if (unmanaged_model_state.options.get(ModelOption.SCHEMA) or default_schema) == schema:
                known_tables.add(unmanaged_model_state.table)
        untracked_columns: list[tuple[str, str, str, str]] = []
        mismatched_columns: list[ColumnMismatch] = []
        # The models to inspect, found against the listed tables - inspected together below, with a
        # fixed number of catalog queries per schema.
        to_inspect: list[tuple[tuple[str, str], ModelState, str, set[str]]] = []
        # On a connection with a schema per tenant, the shared schema holds the models without
        # Meta.tenant_schema and a tenant's schema the ones with it - the others are compared in the
        # other schema, taken here as declared.
        tenant_schema_side = (
            True
            if connection.tenant_schema is not None
            else False
            if connection.tenant_schema_template is not None
            else None
        )
        for key, model_state in new_state.models.items():
            app_label, _model_name = key
            if app_label not in target_labels:
                continue
            if (
                tenant_schema_side is not None
                and bool(model_state.options.get(ModelOption.TENANT_SCHEMA)) is not tenant_schema_side
            ):
                db_state.models[key] = model_state.clone()
                known_tables.add(model_state.table)
                continue
            model_schema = model_state.options.get(ModelOption.SCHEMA) or default_schema
            storage_table = DriftStateBuilder.get_storage_table(model_state, dialect.name)
            if model_schema == schema:
                model_real_table_names = real_table_names
                known_tables.update((model_state.table, storage_table))
                known_tables.update(ObservedFields.auto_managed_through_tables(model_state.fields))
            else:
                # Not folded into `known_tables`/`untracked_tables` - those report what's extra
                # in `schema` specifically, a different schema's own tables are simply out of
                # scope for that sweep, not evidence of anything untracked in `schema`.
                if model_schema not in real_table_names_by_other_schema:
                    real_table_names_by_other_schema[model_schema] = set(
                        await DatabaseCatalog.get_table_names(connection, schema=model_schema)
                    )
                model_real_table_names = real_table_names_by_other_schema[model_schema]
            if storage_table not in model_real_table_names:
                continue
            to_inspect.append((key, model_state, model_schema, model_real_table_names))

        # One batched inspect_tables() call per schema. verify_exists=False - every entry here was
        # already matched against a just-fetched table list above.
        to_inspect_positions_by_schema: dict[str, list[int]] = {}
        for position, (_key, _model_state, model_schema, _model_real_table_names) in enumerate(to_inspect):
            to_inspect_positions_by_schema.setdefault(model_schema, []).append(position)
        table_info_by_position: dict[int, TableInfo] = {}
        for model_schema, positions in to_inspect_positions_by_schema.items():
            schema_table_infos = await DatabaseCatalog.inspect_tables(
                connection,
                [DriftStateBuilder.get_storage_table(to_inspect[position][1], dialect.name) for position in positions],
                schema=model_schema,
                verify_exists=False,
            )
            table_info_by_position.update(zip(positions, schema_table_infos, strict=True))
        for position, (key, model_state, model_schema, model_real_table_names) in enumerate(to_inspect):
            table_info = table_info_by_position[position]
            app_label, model_name = key
            expected_columns = ObservedFields.expected_columns(model_state.fields)
            untracked_columns.extend(
                (app_label, model_name, model_state.table, column.name)
                for column in table_info.columns
                if column.name not in expected_columns
            )
            if table_info.name != model_state.table and model_state.table in model_real_table_names:
                # The table over the stored one - a Distributed table - reads the same columns.
                (companion_info,) = await DatabaseCatalog.inspect_tables(
                    connection, [model_state.table], schema=model_schema, verify_exists=False
                )
                companion_mismatches = ObservedFields.get_companion_column_mismatches(table_info, companion_info)
                mismatched_columns.extend(
                    ColumnMismatch(app_label, model_name, model_state.table, column_name, detail)
                    for column_name, detail in companion_mismatches
                )
            (
                out_of_sync_many_to_many_field_names,
                unindexed_many_to_many_field_names,
                constraint_changed_many_to_many_field_names,
            ) = await ObservedFields.get_out_of_sync_many_to_many_field_names(
                connection, model_state.fields, model_real_table_names, model_schema
            )
            matched_unique_indexes = ObservedUniqueness.match_declared_unique_indexes(
                introspector_class, model_state, table_info
            )
            observed_fields = ObservedFields.build_observed_fields(
                model_state.fields,
                table_info,
                out_of_sync_many_to_many_field_names,
                frozenset(
                    ObservedIndexes.get_declared_single_field_indexes(
                        model_state,
                        unique_is_plain=not connection.features.supports_unique_constraints,
                        partial_is_plain=not introspector_class.HAS_PARTIAL_INDEXES,
                    )
                ),
                [index for index, _declared in matched_unique_indexes],
                unindexed_many_to_many_field_names,
                dialect,
                default_schema,
                constraint_changed_many_to_many_field_names,
            )
            if introspector_class.HAS_COMMENTS:
                ObservedFields.apply_observed_comments(observed_fields, table_info)
            mismatched_columns.extend(
                ColumnMismatch(app_label, model_name, model_state.table, column_name, detail)
                for column_name, detail in [
                    *ObservedFields.get_column_mismatches(model_state.fields, table_info, dialect),
                    *ObservedFields.get_primary_key_mismatches(model_state, table_info),
                    *ObservedFields.get_swappable_foreign_key_mismatches(model_state.fields, table_info),
                    *ObservedFields.get_foreign_key_rule_mismatches(model_state.fields, table_info),
                ]
            )
            observed_options = ObservedMetaOptions.build_observed_options(
                introspector_class,
                model_state,
                table_info,
                observed_fields,
                matched_unique_indexes,
                unique_is_plain=not connection.features.supports_unique_constraints,
            )
            if not connection.features.supports_unique_constraints:
                ObservedUniqueness.apply_declared_uniqueness(model_state, observed_fields, observed_options)
            await ObservedTableOptions.apply_observed_table_options(
                connection, introspector_class, dialect.name, model_state, table_info, observed_options, model_schema
            )
            await ObservedSchemaObjects.apply_observed_schema_objects(
                connection,
                introspector_class,
                model_state,
                model_schema,
                table_info,
                ColumnFieldNames.column_to_field_name(model_state.fields),
                observed_options,
            )
            table_description = model_state.description
            if introspector_class.HAS_COMMENTS and (table_info.table_description or "") != (table_description or ""):
                table_description = table_info.table_description or ""
                observed_options[ModelOption.TABLE_DESCRIPTION] = table_description
            db_state.models[key] = ModelState(
                name=model_state.name,
                app=model_state.app,
                table=model_state.table,
                abstract=model_state.abstract,
                description=table_description,
                options=observed_options,
                bases=model_state.bases,
                pk_field_name=model_state.pk_field_name,
                fields=observed_fields,
            )
        untracked_tables = sorted(real_table_names - known_tables)
        return db_state, untracked_tables, sorted(untracked_columns), mismatched_columns

    @staticmethod
    def get_storage_table(model_state: ModelState, dialect_name: str) -> str:
        """The table holding a model's rows - a table over a cluster's local tables reads theirs.

        Args:
            model_state: The model's state.
            dialect_name: The connection's dialect.

        Returns:
            The table's name.
        """
        table_options = next(
            (
                entry
                for entry in model_state.options.get(ModelOption.TABLE_OPTIONS, ())
                if entry.dialect_name == dialect_name
            ),
            None,
        )
        return model_state.table if table_options is None else table_options.get_storage_table_name(model_state.table)
