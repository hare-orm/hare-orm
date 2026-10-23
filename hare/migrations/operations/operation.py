from __future__ import annotations

import abc
import inspect
from typing import TYPE_CHECKING, Any

from hare.migrations.reports.operation_effect import OperationEffect
from hare.migrations.state.state import State

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.migrations.operations.dependencies.operation_dependency import OperationDependency


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
    #: What an operation makemigrations made needs beside its own requirements - known when it was
    #: made, never written into the migration file.
    generation_dependencies: tuple[OperationDependency, ...] = ()

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

    def get_provided_dependencies(self, app_label: str) -> list[OperationDependency]:
        """What the operation creates, removes or changes - an operation needing it runs after it.

        Args:
            app_label: The migration's app.

        Returns:
            The dependencies - none by default.
        """
        return []

    def get_required_dependencies(self, app_label: str, state: State) -> list[OperationDependency]:
        """What must be created, removed or changed before the operation runs - each met by the first
        operation providing it; one no operation of the migrations provides is met already.

        Args:
            app_label: The migration's app.
            state: The state before the migrations being ordered.

        Returns:
            The dependencies - none by default.
        """
        return []

    def get_unmet_requirement_message(self, app_label: str, requirement: OperationDependency) -> str:
        """Why the operation can't be made: what it needs another app to do, which none of that app's new
        operations does.

        Args:
            app_label: The migration's app.
            requirement: The requirement no operation meets.

        Returns:
            The message.
        """
        subject = ".".join(
            name for name in (requirement.app_label, requirement.model_name, requirement.name) if name is not None
        )
        return (
            f"{self.describe()} (app {app_label!r}) needs {requirement.subject} {subject} "
            f"{requirement.dependency_type} first - run makemigrations for {requirement.app_label!r} in the same run."
        )

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

    def raise_if_unsupported(
        self, app_label: str, old_state: State, new_state: State, schema_editor: BaseSchemaEditor
    ) -> None:
        """Refuses the operation on the schema editor's connection as ``database_forward()`` would before
        its DDL - checked for every operation of a migration run outside a transaction before any of
        them runs. Nothing by default.

        Args:
            app_label: The migration's app.
            old_state: The state before the operation.
            new_state: The state after it.
            schema_editor: The schema editor it runs with.

        Raises:
            ConfigurationError: The operation's model doesn't fit its options.
            UnSupportedError: The connection can't make the change.
        """

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
    def state_forward(self, app_label: str, state: State) -> None:
        """Applies the operation to the migration state.

        Args:
            app_label: The migration's app.
            state: The state, changed in place.
        """

    @abc.abstractmethod
    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        """Changes the database as the operation changes the state.

        Args:
            app_label: The migration's app.
            old_state: The state before the operation.
            new_state: The state after it.
            state_editor: The schema editor of the connection - None writes nothing.
        """

    @abc.abstractmethod
    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        """Undoes the operation's change of the database - the migration being unapplied.

        Args:
            app_label: The migration's app.
            old_state: The state the operation is undone to.
            new_state: The state it leaves.
            state_editor: The schema editor of the connection - None writes nothing.
        """
