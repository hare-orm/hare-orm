from __future__ import annotations

from hare.ddl.conditions.trigger_condition import TriggerCondition
from hare.ddl.schema_objects.trigger import Trigger
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.models import Model


class TriggerStatements(SchemaEditorPart):
    """Triggers of a table: created, removed, altered and renamed."""

    __slots__ = ()

    def get_trigger_create_sqls(self, model: type[Model], trigger: Trigger, safe: bool = False) -> list[str]:
        """The statements creating a trigger - shared by add_trigger() and generate_schemas().

        Args:
            model: The model the trigger is declared on.
            trigger: The trigger.
            safe: Replace an existing trigger of the same name instead of failing.

        Returns:
            The DDL statements, in execution order.

        Raises:
            UnSupportedError: The dialect has no triggers.
        """
        raise self.get_unsupported_error("Triggers")

    def get_when_sql(self, model: type[Model], trigger: Trigger) -> str | None:
        """The SQL of a trigger's ``WHEN`` condition.

        Args:
            model: The model the trigger is declared on.
            trigger: The trigger.

        Returns:
            The condition, None for a trigger without one.
        """
        if trigger.when is None:
            return None
        return TriggerCondition.get_sql(trigger.when, trigger.get_condition_row(), model, self.editor.client)

    async def add_trigger(self, model: type[Model], trigger: Trigger) -> None:
        for statement in self.get_trigger_create_sqls(model, trigger):
            await self.editor.run_sql(statement)

    async def remove_trigger(self, model: type[Model], trigger: Trigger) -> None:
        """Drops a trigger.

        Raises:
            UnSupportedError: The dialect has no triggers.
        """
        raise self.get_unsupported_error("Triggers")

    async def alter_trigger(self, model: type[Model], old_trigger: Trigger, new_trigger: Trigger) -> None:
        await self.remove_trigger(model, old_trigger)
        await self.add_trigger(model, new_trigger)

    async def rename_trigger(self, model: type[Model], old_trigger: Trigger, new_trigger: Trigger) -> None:
        if old_trigger.name == new_trigger.name:
            return
        await self.remove_trigger(model, old_trigger)
        await self.add_trigger(model, new_trigger)
