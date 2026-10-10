from __future__ import annotations

import abc
import inspect
from typing import TYPE_CHECKING, Any

from hare.migrations.reports.operation_effect import OperationEffect
from hare.migrations.state.state import State

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class Operation(abc.ABC):
    """A step of a migration - what it does to the models' state and to the database, forwards and backwards."""

    reversible = True
    reduces_to_sql = True
    atomic: bool | None = False
    #: Whether ``database_forward()`` reads the state before the operation - ``Migration.apply()``
    #: copies it only then.
    reads_old_state = True
    #: Whether an operation bound to no model runs in each tenant's schema instead of the shared one
    #: on a connection with ``tenant_schema_template`` - ``RunSQL``/``RunPython(tenant_schema=True)``.
    tenant_schema = False

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
        specification = inspect.getfullargspec(type(self).__init__)
        default_values = specification.defaults or ()
        defaults = dict(
            zip(specification.args[len(specification.args) - len(default_values) :], default_values, strict=True)
        )
        defaults.update(specification.kwonlydefaults or {})
        for name in [*specification.args[1:], *specification.kwonlyargs]:
            value = getattr(self, name)
            if name in defaults and value == defaults[name]:
                continue
            kwargs[name] = value
        return f"hare.migrations.operations.{type(self).__name__}", [], kwargs

    def get_table_model_names(self) -> tuple[str, ...]:
        """The models of the migration's own app whose table this operation changes - none for
        an operation not bound to one model."""
        return ()

    def get_referenced_model_labels(self, app_label: str) -> frozenset[str] | None:
        """The models the operation reads or changes, as lowercase ``app.model`` labels.

        Args:
            app_label: The migration's app.

        Returns:
            The labels; None when the operation can't tell - it may touch any model, so nothing
            is squashed across it. None by default.
        """
        return None

    def can_move_across(self, other: Operation, app_label: str) -> bool:
        """Whether this operation and another can swap places - neither touches a model the other
        does.

        Args:
            other: The other operation.
            app_label: The migrations' app.

        Returns:
            Whether both name the models they touch and share none.
        """
        model_labels = self.get_referenced_model_labels(app_label)
        other_model_labels = other.get_referenced_model_labels(app_label)
        return model_labels is not None and other_model_labels is not None and not model_labels & other_model_labels

    def reduce(self, other: Operation, app_label: str) -> list[Operation] | bool:
        """What this operation and a later one become when their migrations are squashed.

        Args:
            other: The later operation.
            app_label: The migrations' app.

        Returns:
            The operations replacing both (empty when they cancel out); True when ``other`` may
            move across this operation unchanged; False when neither. By default ``other`` moves
            across when the two touch no common model.
        """
        return self.can_move_across(other, app_label)

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

    def runs_in_tenant_schema(self, app_label: str, *states: State) -> bool:
        """Whether this operation changes each tenant's schema rather than the shared one - an
        operation on a model's table by the model's ``Meta.tenant_schema``, any other by its own
        ``tenant_schema``.

        Args:
            app_label: The migration's app.
            states: The states before and after the operation.
        """
        model_names = self.get_table_model_names()
        if not model_names:
            return self.tenant_schema
        return any(
            state.is_tenant_schema_model(app_label, model_name) for state in states for model_name in model_names
        )

    @abc.abstractmethod
    async def run(
        self,
        app_label: str,
        state: State,
        dry_run: bool,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None: ...

    @abc.abstractmethod
    def state_forward(self, app_label: str, state: State) -> None: ...

    @abc.abstractmethod
    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None: ...

    @abc.abstractmethod
    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None: ...
