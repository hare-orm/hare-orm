from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.tables.table_creation import TableCreation
from hare.dialects.base.schema.tables.table_definition import TableDefinition
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.schema.tables.clickhouse_replica_check import ClickhouseReplicaCheck

if TYPE_CHECKING:
    from hare.dialects.clickhouse.schema.clickhouse_schema_editor import ClickhouseSchemaEditor
    from hare.models.model_binding import ModelBinding


class ClickhouseTableCreation(TableCreation):
    """The ``CREATE TABLE`` of ClickHouse - the primary key, composite too, is the ``PRIMARY KEY``
    clause of the table's engine (``ClickhouseTableOptions``)."""

    __slots__ = ()
    editor: ClickhouseSchemaEditor

    def get_definition(
        self,
        binding: ModelBinding,
        safe: bool = False,
        deferred_table_keys: frozenset[tuple[str | None, str]] = frozenset(),
        *,
        storage_table_name: str | None = None,
    ) -> TableDefinition:
        """Attach schema placement while keeping the physical identity usable by row copies."""
        definition = super().get_definition(binding, safe, deferred_table_keys, storage_table_name=storage_table_name)
        return replace(definition, placement_sql=self.editor.get_schema_cluster_sql(binding.schema))

    def get_replica_check_plan(self, binding: ModelBinding) -> SchemaChangePlan:
        """Prepare the topology check only for an unreplicated distributed storage declaration."""
        model = binding._model
        client = self.editor.client
        cluster = getattr(client, "cluster", None)
        if cluster is None:
            return SchemaChangePlan(())
        options = ClickhouseTableOptions.get_for_model(binding, client.dialect)
        if options.distributed_over is None or options.get_engine().is_replicated():
            return SchemaChangePlan(())
        return SchemaChangePlan((ClickhouseReplicaCheck(model.__name__, options.get_engine_name(), cluster.name),))

    def get_create_plan(self, binding: ModelBinding) -> SchemaChangePlan:
        """Compile all declarations before checking topology and creating the table."""
        plan = super().get_create_plan(binding)
        return SchemaChangePlan((*self.get_replica_check_plan(binding).steps, *plan.steps), after=plan.after)

    def get_composite_pk_constraint_sql(self, binding: ModelBinding, field_names: list[str]) -> str:
        return ""
