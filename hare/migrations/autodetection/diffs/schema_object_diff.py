from __future__ import annotations

import abc
from collections.abc import Hashable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.migrations.operations import HareOperation
    from hare.migrations.state.model_state import ModelState


class SchemaObjectDiff(abc.ABC):
    """The operations taking the named objects of one type - indexes, constraints, triggers - a
    model has in one state to those it has in another: an unchanged object is left alone, one
    only renamed is renamed, one changed in place is altered where its type allows it, and the
    rest are removed and added. A subclass per type says what an object's signature (everything
    but its name) is and which operations it takes.

    Args:
        old_state: The model's state before.
        new_state: The model's state after.
    """

    def __init__(self, old_state: ModelState, new_state: ModelState) -> None:
        self.old_state = old_state
        self.new_state = new_state

    @abc.abstractmethod
    def get_objects(self, model_state: ModelState) -> list[Any]:
        """The objects of the type a model state declares."""

    @abc.abstractmethod
    def get_signature(self, schema_object: Any) -> Hashable:
        """Everything about an object but its name."""

    def get_name(self, schema_object: Any) -> str | None:
        """An object's declared name, None for an unnamed one."""
        return getattr(schema_object, "name", None)

    def can_be_renamed(self, schema_object: Any) -> bool:
        """Whether an object is renamed rather than removed and added when only its name changes."""
        return self.get_name(schema_object) is not None

    def is_unchanged(self, old_object: Any, new_object: Any) -> bool:
        """Whether two objects of the two states are the same object, unchanged."""
        return self.get_name(old_object) == self.get_name(new_object) and self.get_signature(
            old_object
        ) == self.get_signature(new_object)

    @abc.abstractmethod
    def get_add_operation(self, schema_object: Any) -> HareOperation:
        """The operation adding an object."""

    @abc.abstractmethod
    def get_remove_operation(self, schema_object: Any) -> HareOperation:
        """The operation removing an object."""

    def get_rename_operation(self, old_object: Any, new_object: Any) -> HareOperation:
        """The operation renaming an object."""
        raise NotImplementedError

    def get_alter_operation(self, new_object: Any) -> HareOperation | None:
        """The operation changing an object of the same name in place - None for a type changed by
        removing and adding it."""
        return None

    def is_made_by_its_field(self, new_object: Any) -> bool:
        """Whether an added object is one the DDL of its field already makes - never added on its
        own then."""
        return False

    def get_operations(self) -> list[HareOperation]:
        """The operations: renames, removes, alters, adds.

        Returns:
            The operations.
        """
        unmatched_old = dict(enumerate(self.get_objects(self.old_state)))
        unmatched_new = dict(enumerate(self.get_objects(self.new_state)))

        def match(old_position: int, new_position: int) -> None:
            del unmatched_old[old_position], unmatched_new[new_position]

        for new_position, new_object in list(unmatched_new.items()):
            for old_position, old_object in list(unmatched_old.items()):
                if self.is_unchanged(old_object, new_object):
                    match(old_position, new_position)
                    break
        rename_operations: list[HareOperation] = []
        for new_position, new_object in list(unmatched_new.items()):
            if not self.can_be_renamed(new_object):
                continue
            new_signature = self.get_signature(new_object)
            for old_position, old_object in list(unmatched_old.items()):
                if (
                    self.can_be_renamed(old_object)
                    and self.get_name(old_object) != self.get_name(new_object)
                    and self.get_signature(old_object) == new_signature
                ):
                    rename_operations.append(self.get_rename_operation(old_object, new_object))
                    match(old_position, new_position)
                    break
        alter_operations: list[HareOperation] = []
        for new_position, new_object in list(unmatched_new.items()):
            name = self.get_name(new_object)
            if name is None:
                continue
            for old_position, old_object in list(unmatched_old.items()):
                if self.get_name(old_object) != name:
                    continue
                alter_operation = self.get_alter_operation(new_object)
                if alter_operation is not None:
                    alter_operations.append(alter_operation)
                    match(old_position, new_position)
                break
        remove_operations = [self.get_remove_operation(old_object) for old_object in unmatched_old.values()]
        add_operations = [
            self.get_add_operation(new_object)
            for new_object in unmatched_new.values()
            if not self.is_made_by_its_field(new_object)
        ]
        return rename_operations + remove_operations + alter_operations + add_operations
