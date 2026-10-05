from __future__ import annotations

from typing import TYPE_CHECKING

from hare.ddl.schema_objects.trigger import Trigger
from hare.dialects.base.schema.triggers.trigger_statements import TriggerStatements
from hare.exceptions import UnSupportedError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.sqlite.schema.sqlite_schema_editor import SqliteSchemaEditor
    from hare.models.model import Model


class SqliteTriggerStatements(TriggerStatements):
    """TriggerStatements as SQLite writes it."""

    __slots__ = ()

    editor: SqliteSchemaEditor

    def get_trigger_create_sqls(self, model: type[Model], trigger: Trigger, safe: bool = False) -> list[str]:
        if trigger.for_each == "STATEMENT":
            raise UnSupportedError(
                f"STATEMENT-level triggers are not supported on {self.editor.client.dialect.name}; "
                "SQLite triggers always fire per row."
            )
        if trigger.deferrable:
            raise UnSupportedError(
                f"CONSTRAINT TRIGGER (deferrable=True) is not supported on {self.editor.client.dialect.name}; "
                "it's Postgres-only."
            )
        table = self.editor.qualify_table_name(model._meta.db_table, model._meta.schema)
        when_sql = self.get_when_sql(model, trigger)
        when_clause = f"WHEN ({when_sql})\n" if when_sql else ""
        template = self.editor.TRIGGER_CREATE_IF_NOT_EXISTS_TEMPLATE if safe else self.editor.TRIGGER_CREATE_TEMPLATE
        return [
            template.format(
                trigger_name=self.editor.quote(trigger.name),
                timing=trigger.timing,
                on=trigger.on,
                table=table,
                when_clause=when_clause,
                body=trigger.body.sql,
            )
        ]

    async def add_trigger(self, model: type[Model], trigger: Trigger) -> None:
        for statement in self.get_trigger_create_sqls(model, trigger):
            await self.run_ddl_statement(statement)

    async def remove_trigger(self, model: type[Model], trigger: Trigger) -> None:
        await self.editor.run_sql(
            self.editor.TRIGGER_DROP_TEMPLATE.format(trigger_name=self.editor.quote(trigger.name))
        )

    async def run_ddl_statement(self, sql: str) -> None:
        """Runs one DDL statement whole - ``run_sql()`` would split a ``CREATE TRIGGER ... BEGIN ...;
        END`` at its inner semicolons.
        """
        if self.editor.collect_sql:
            self.editor.collected_sql.append(sql)
            return
        await self.editor.client.execute(sql)
