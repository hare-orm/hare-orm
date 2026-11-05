from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, cast

from hare.dialects.base.schema.plans.enums import SchemaEffect
from hare.dialects.clickhouse.schema.constants import CLICKHOUSE_REBUILD_CHECK_PREVIEW
from hare.dialects.clickhouse.schema_objects.clickhouse_materialized_view import ClickhouseMaterializedView

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.dialects.clickhouse.schema.clickhouse_schema_editor import ClickhouseSchemaEditor
    from hare.models import Model


@dataclass(frozen=True, slots=True)
class ClickhouseRebuildCheck:
    """Check cluster placement before dropping any dependent schema object.

    A model declaration cannot establish which shards contain rows. Execution reads
    that metadata and also checks materialized views whose storage will be filled;
    collection describes the precondition without contacting the cluster.
    """

    old_model: type[Model]
    new_model: type[Model]
    effects: ClassVar[frozenset[SchemaEffect]] = frozenset({SchemaEffect.READ_CATALOG, SchemaEffect.CHECK_ROWS})

    async def execute(self, editor: BaseSchemaEditor) -> None:
        """Reject a placement that would copy only a fraction of the table's rows."""
        schema_editor = cast("ClickhouseSchemaEditor", editor)
        if schema_editor.collect_sql:
            schema_editor.collected_sql.append(CLICKHOUSE_REBUILD_CHECK_PREVIEW)
            return
        await schema_editor.table_rebuild.check_database_exchange(self.new_model)
        await schema_editor.table_rebuild.check_storage_target(self.old_model, self.new_model)
        replica_path = await schema_editor.table_rebuild.raise_if_default_replica_path_is_fixed(self.new_model)
        await schema_editor.table_rebuild.raise_if_rows_are_scattered(self.old_model)
        await schema_editor.table_rebuild.raise_if_rows_are_scattered(self.new_model, replica_path=replica_path)
        for view in self.new_model._meta.materialized_views:
            declaration = ClickhouseMaterializedView.from_materialized_view(view)
            if view.with_data and declaration.refresh is None and declaration.to is None:
                await schema_editor.materialized_views.raise_if_filled_on_one_server(self.new_model, view)
