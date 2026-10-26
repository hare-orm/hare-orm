from __future__ import annotations


class FieldCondition:
    """A condition reading fields of a model by their names - a ``Q``, an exclusive arc, an enum's
    members, a tenant condition: a migration renaming or removing a field finds the conditions reading
    it through them."""

    __slots__ = ()

    def get_referenced_field_names(self) -> set[str]:
        """The names of the model's fields the condition reads."""
        raise NotImplementedError

    def with_renamed_field(self, old_name: str, new_name: str) -> FieldCondition:
        """The condition reading ``new_name`` where it read ``old_name``.

        Args:
            old_name: The field's name.
            new_name: Its new name.

        Returns:
            The condition.
        """
        raise NotImplementedError
