from __future__ import annotations

from typing import TYPE_CHECKING

from hare.ddl.enums import TriggerForEach
from hare.ddl.schema_objects.trigger import Trigger
from hare.dialects.base.schema.triggers.trigger_statements import TriggerStatements
from hare.exceptions import UnSupportedError

if TYPE_CHECKING:
    from hare.dialects.sqlite.schema.sqlite_schema_editor import SqliteSchemaEditor
    from hare.models.model_binding import ModelBinding


class SqliteTriggerStatements(TriggerStatements):
    """TriggerStatements as SQLite writes it."""

    __slots__ = ()

    editor: SqliteSchemaEditor

    def get_trigger_create_sqls(self, binding: ModelBinding, trigger: Trigger, safe: bool = False) -> list[str]:
        if trigger.for_each == TriggerForEach.STATEMENT:
            raise UnSupportedError(
                f"STATEMENT-level triggers are not supported on {self.editor.client.dialect.name}; "
                "SQLite triggers always fire per row."
            )
        if trigger.deferrable:
            raise UnSupportedError(
                f"CONSTRAINT TRIGGER (deferrable=True) is not supported on {self.editor.client.dialect.name}; "
                "SQLite has no deferrable trigger."
            )
        if not trigger.enabled:
            raise UnSupportedError(
                f"A disabled trigger (enabled=False) is not supported on {self.editor.client.dialect.name}; "
                "SQLite fires every trigger it has."
            )
        table = self.editor.get_indexed_table_sql(binding.db_table, binding.schema)
        when_sql = self.get_when_sql(binding, trigger)
        when_clause = f"WHEN ({when_sql})\n" if when_sql else ""
        template = self.editor.TRIGGER_CREATE_IF_NOT_EXISTS_TEMPLATE if safe else self.editor.TRIGGER_CREATE_TEMPLATE
        return [
            template.format(
                trigger_name=self.editor.get_index_name_sql(trigger.name, binding.schema),
                timing=trigger.timing,
                on=trigger.on,
                table=table,
                when_clause=when_clause,
                body=trigger.body.sql,
            )
        ]

    async def remove_trigger(self, binding: ModelBinding, trigger: Trigger) -> None:
        await self.editor.run_sql(
            self.editor.TRIGGER_DROP_TEMPLATE.format(
                trigger_name=self.editor.qualify_table_name(trigger.name, binding.schema)
            )
        )
