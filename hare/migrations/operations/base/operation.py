from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

from hare.migrations.reports.operation_effect import OperationEffect
from hare.migrations.state.project.state import State

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.schema.editor import BaseSchemaEditor


class Operation:
    reversible = True
    reduces_to_sql = True
    atomic: bool | None = False
    #: Whether ``database_forward()`` reads the state before the operation - ``Migration.apply()``
    #: copies it only then.
    reads_old_state = True

    def describe(self) -> str:
        return self.__class__.__name__

    def __str__(self) -> str:
        return self.__class__.__name__

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        """The operation as a migration file writes it: its class, and every constructor argument
        whose value isn't the argument's default - read from the attribute of the same name.

        Returns:
            ``(path, args, kwargs)`` - the path names the class among the operations.
        """
        kwargs: dict[str, Any] = {}
        spec = inspect.getfullargspec(type(self).__init__)
        default_values = spec.defaults or ()
        defaults = dict(zip(spec.args[len(spec.args) - len(default_values) :], default_values, strict=True))
        defaults.update(spec.kwonlydefaults or {})
        for name in [*spec.args[1:], *spec.kwonlyargs]:
            value = getattr(self, name)
            if name in defaults and value == defaults[name]:
                continue
            kwargs[name] = value
        return f"hare.migrations.operations.{type(self).__name__}", [], kwargs

    def get_table_model_names(self) -> tuple[str, ...]:
        """The models of the migration's own app whose table this operation changes - none for
        an operation not bound to one model."""
        return ()

    def get_effect(self, app_label: str, state: State, dialect: Dialect) -> OperationEffect:
        """What the operation does to the database's data - known before it runs.

        Args:
            app_label: The migration's app.
            state: The state before the operation.
            dialect: The dialect of the database it runs on.

        Returns:
            Whether it can be unapplied, rewrites a table or loses data - by default only
            whether it can be unapplied.
        """
        return OperationEffect(operation=self, reversible=self.reversible)

    def touches_swapped_model(self, app_label: str, *states: State) -> bool:
        """Whether this operation changes the table of a model swapped for another one by its
        ``swappable`` setting in any of `states` - such a model has no table, so the operation
        only changes the migration state.

        Args:
            app_label: The migration's app.
            states: The states before and after the operation.
        """
        return any(
            state.is_swapped_model(app_label, model_name)
            for state in states
            for model_name in self.get_table_model_names()
        )

    async def run(
        self,
        app_label: str,
        state: State,
        dry_run: bool,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        raise NotImplementedError()

    def state_forward(self, app_label: str, state: State) -> None:
        raise NotImplementedError()

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        raise NotImplementedError()

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        raise NotImplementedError()
