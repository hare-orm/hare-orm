from __future__ import annotations

from typing import TYPE_CHECKING

from hare.ddl.schema_objects.trigger import Trigger
from hare.dialects.base.schema.triggers.trigger_statements import TriggerStatements
from hare.dialects.postgresql.constants import (
    POSTGRESQL_DEFAULT_TRIGGER_LANGUAGE,
)
from hare.models import Model

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor


class PostgresqlTriggerStatements(TriggerStatements):
    """TriggerStatements as PostgreSQL writes it."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

    def get_trigger_create_sqls(self, model: type[Model], trigger: Trigger, safe: bool = False) -> list[str]:
        """The statements creating `trigger`'s backing function and the trigger itself - shared
        by add_trigger() and generate_schemas().

        Args:
            model: The model the trigger is declared on.
            trigger: The trigger.
            safe: Replace an existing function and trigger of the same name instead of failing.

        Returns:
            The DDL statements, in execution order.
        """
        table = self.editor.qualify_table_name(model._meta.db_table, model._meta.schema)
        # The function lives in the model's schema, like its table - same-named functions of several
        # schemas don't collide, and dropping the schema drops it.
        qualified_function = self.editor.qualify_table_name(trigger.function_name, model._meta.schema)
        function_template = (
            self.editor.TRIGGER_FUNCTION_CREATE_OR_REPLACE_TEMPLATE
            if safe
            else self.editor.TRIGGER_FUNCTION_CREATE_TEMPLATE
        )
        statements = [
            function_template.format(
                function_name=qualified_function,
                body=trigger.body.sql,
                language=trigger.language or POSTGRESQL_DEFAULT_TRIGGER_LANGUAGE,
            )
        ]
        if safe:
            statements.append(
                self.editor.TRIGGER_DROP_IF_EXISTS_TEMPLATE.format(
                    trigger_name=self.editor.quote(trigger.name), table=table
                )
            )
        when_sql = self.get_when_sql(model, trigger)
        when_clause = f"\n    WHEN ({when_sql})" if when_sql else ""
        if trigger.deferrable:
            statements.append(
                self.editor.TRIGGER_CONSTRAINT_CREATE_TEMPLATE.format(
                    trigger_name=self.editor.quote(trigger.name),
                    timing=trigger.timing,
                    on=trigger.on,
                    table=table,
                    initially="DEFERRED" if trigger.initially_deferred else "IMMEDIATE",
                    for_each=trigger.for_each,
                    when_clause=when_clause,
                    function_name=qualified_function,
                )
            )
        else:
            statements.append(
                self.editor.TRIGGER_CREATE_TEMPLATE.format(
                    trigger_name=self.editor.quote(trigger.name),
                    timing=trigger.timing,
                    on=trigger.on,
                    table=table,
                    for_each=trigger.for_each,
                    when_clause=when_clause,
                    function_name=qualified_function,
                )
            )
        return statements

    async def add_trigger(self, model: type[Model], trigger: Trigger) -> None:
        for statement in self.get_trigger_create_sqls(model, trigger):
            await self.editor.run_sql(statement)

    async def remove_trigger(self, model: type[Model], trigger: Trigger) -> None:
        table = self.editor.qualify_table_name(model._meta.db_table, model._meta.schema)
        qualified_function = self.editor.qualify_table_name(trigger.function_name, model._meta.schema)
        await self.editor.run_sql(
            self.editor.TRIGGER_DROP_TEMPLATE.format(trigger_name=self.editor.quote(trigger.name), table=table)
        )
        await self.editor.run_sql(self.editor.TRIGGER_FUNCTION_DROP_TEMPLATE.format(function_name=qualified_function))

    async def alter_trigger(self, model: type[Model], old_trigger: Trigger, new_trigger: Trigger) -> None:
        # PostgreSQL can't alter a trigger's timing, events or body - it is dropped and created
        # again.
        await self.remove_trigger(model, old_trigger)
        await self.add_trigger(model, new_trigger)

    async def rename_trigger(self, model: type[Model], old_trigger: Trigger, new_trigger: Trigger) -> None:
        if old_trigger.name == new_trigger.name:
            return
        if self.editor.RENAME_TRIGGER_TEMPLATE and self.editor.RENAME_TRIGGER_FUNCTION_TEMPLATE:
            table = self.editor.qualify_table_name(model._meta.db_table, model._meta.schema)
            await self.editor.run_sql(
                self.editor.RENAME_TRIGGER_TEMPLATE.format(
                    table=table,
                    old_name=self.editor.quote(old_trigger.name),
                    new_name=self.editor.quote(new_trigger.name),
                )
            )
            # A renamed trigger's backing function is renamed to match, keeping
            # Trigger.function_name's f"{name}_fn" convention consistent - RENAME TO takes just
            # the new bare name, a function can't change schema this way (nor does it need to).
            qualified_old_function = self.editor.qualify_table_name(old_trigger.function_name, model._meta.schema)
            await self.editor.run_sql(
                self.editor.RENAME_TRIGGER_FUNCTION_TEMPLATE.format(
                    function_name=qualified_old_function,
                    new_function_name=self.editor.quote(new_trigger.function_name),
                )
            )
            return
        await self.remove_trigger(model, old_trigger)
        await self.add_trigger(model, new_trigger)
