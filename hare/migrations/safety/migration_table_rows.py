from __future__ import annotations

from typing import TYPE_CHECKING

from hare.migrations.operations import (
    AlterModelSchema,
    AlterModelTable,
    CreateModel,
    DeleteModel,
    RenameModel,
    RunPython,
    RunSQL,
)
from hare.migrations.safety.declarations import TableRowEstimate
from hare.models.enums import ModelOption

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.migrations.state.model_state import ModelState
    from hare.migrations.state.prepared_operation import PreparedOperation
    from hare.migrations.state.state import State


class MigrationTableRows:
    """Follow row estimates through a prepared migration's physical table changes.

    Catalog reads always address tables as they existed before the migration. A move
    retains its estimate; a real create replaces it with a known empty table. Unknown
    physical identities and opaque data operations remain conservative. This consumes
    prepared snapshots and never calls ``state_forward`` or a data callback.
    """

    def __init__(self, state: State, client: DatabaseClient, large_table_rows: int, *, read_catalog: bool) -> None:
        self.client = client
        self.large_table_rows = large_table_rows
        self.tables = {
            (identity := self.get_identity(model)): TableRowEstimate(identity, count_pending=read_catalog)
            for model in state.models.values()
            if self.has_physical_table(model)
        }

    @staticmethod
    def get_identity(model: ModelState) -> tuple[str | None, str]:
        """The declared schema and physical table name, independently of the model name."""
        return model.options.get(ModelOption.SCHEMA) or None, model.table or model.name.lower()

    @staticmethod
    def has_physical_table(model: ModelState) -> bool:
        return not model.abstract and model.options.get(ModelOption.MANAGED, True) is not False

    @staticmethod
    def get_model(state: State, app_label: str, name: str) -> ModelState | None:
        lowercase_name = name.lower()
        return next(
            (
                model
                for (app, model_name), model in state.models.items()
                if app == app_label and model_name.lower() == lowercase_name
            ),
            None,
        )

    async def get_count(self, model: ModelState | None) -> int | None:
        if model is None or not self.has_physical_table(model):
            return None
        estimate = self.tables.get(self.get_identity(model))
        if estimate is None:
            return None
        if estimate.count_pending:
            assert estimate.original_table is not None  # nosec B101
            schema, table = estimate.original_table
            estimate.row_count = await self.client.dialect.migration_safety_rules.count_table_rows(
                self.client, table, schema, self.large_table_rows
            )
            estimate.count_pending = False
        return estimate.row_count

    async def get_counts(self, prepared: PreparedOperation, app_label: str) -> dict[str, int | None]:
        """An independent snapshot for rules that may inspect this operation later."""
        operation = prepared.operation
        counts = {}
        for name in operation.get_table_model_names():
            source_name = operation.old_name if isinstance(operation, RenameModel) else name
            model = self.get_model(prepared.before, app_label, source_name)
            if isinstance(operation, CreateModel) and not operation.state_only:
                created_model = self.get_model(prepared.after, app_label, name)
                counts[name.lower()] = (
                    0 if created_model is not None and self.has_physical_table(created_model) else None
                )
            else:
                counts[name.lower()] = await self.get_count(model)
        return counts

    def advance(self, prepared: PreparedOperation, app_label: str) -> None:
        """Retain facts after one physical operation; container branches are walked by the checker."""
        operation = prepared.operation
        if isinstance(operation, (CreateModel, DeleteModel)):
            if operation.state_only:
                return
            state = prepared.after if isinstance(operation, CreateModel) else prepared.before
            model = self.get_model(state, app_label, operation.name)
            if model is None or not self.has_physical_table(model):
                return
            identity = self.get_identity(model)
            if isinstance(operation, CreateModel):
                self.tables[identity] = TableRowEstimate(None, row_count=0)
            else:
                self.tables.pop(identity, None)
        elif isinstance(operation, (RenameModel, AlterModelTable, AlterModelSchema)):
            old_name, new_name = (
                (operation.old_name, operation.new_name)
                if isinstance(operation, RenameModel)
                else (operation.name, operation.name)
            )
            before = self.get_model(prepared.before, app_label, old_name)
            after = self.get_model(prepared.after, app_label, new_name)
            if before is not None and after is not None:
                estimate = self.tables.pop(self.get_identity(before), TableRowEstimate(None))
                self.tables[self.get_identity(after)] = estimate
        elif isinstance(operation, (RunSQL, RunPython)):
            # Opaque code may populate a newly created table. Do not infer its effects
            # from SQL text or rerun counts against the pre-migration database.
            for estimate in self.tables.values():
                estimate.row_count = None
                estimate.count_pending = False
