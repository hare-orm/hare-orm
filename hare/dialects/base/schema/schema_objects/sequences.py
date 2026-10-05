from __future__ import annotations

from hare.ddl.schema_objects.database_sequence import DatabaseSequence
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.models import Model


class Sequences(SchemaEditorPart):
    """Sequences a model declares: created with their owner, dropped, altered, renamed, and their next
    value read."""

    __slots__ = ()

    def get_sequence_create_sqls(
        self, model: type[Model], sequence: DatabaseSequence, safe: bool = False
    ) -> list[str]:
        """The statements creating a sequence of a model - without its owner column, which
        ``get_sequence_owner_sqls()`` sets once the table exists.

        Args:
            model: The model declaring it.
            sequence: The sequence.
            safe: Create it only when it doesn't exist yet.

        Raises:
            UnSupportedError: The dialect has no sequences.
        """
        raise self.get_unsupported_error("Sequences")

    def get_sequence_owner_sqls(self, model: type[Model], sequence: DatabaseSequence) -> list[str]:
        """The statements making a sequence of a model owned by its column - none without
        ``owned_by``.

        Raises:
            UnSupportedError: The dialect has no sequences.
        """
        raise self.get_unsupported_error("Sequences")

    async def create_sequence(self, model: type[Model], sequence: DatabaseSequence) -> None:
        """Creates a sequence of a model, owned by its column."""
        await self.editor.run_sqls(
            [*self.get_sequence_create_sqls(model, sequence), *self.get_sequence_owner_sqls(model, sequence)]
        )

    async def drop_sequence(self, model: type[Model], sequence: DatabaseSequence) -> None:
        """Drops a sequence of a model.

        Raises:
            UnSupportedError: The dialect has no sequences.
        """
        raise self.get_unsupported_error("Sequences")

    async def alter_sequence(
        self, model: type[Model], old_sequence: DatabaseSequence, new_sequence: DatabaseSequence
    ) -> None:
        """Gives a sequence of a model its new settings - its current number is kept.

        Raises:
            UnSupportedError: The dialect has no sequences.
        """
        raise self.get_unsupported_error("Sequences")

    async def rename_sequence(
        self, model: type[Model], old_sequence: DatabaseSequence, new_sequence: DatabaseSequence
    ) -> None:
        """Renames a sequence of a model.

        Raises:
            UnSupportedError: The dialect has no sequences.
        """
        raise self.get_unsupported_error("Sequences")

    async def get_next_sequence_value(self, model: type[Model], sequence: DatabaseSequence) -> int:
        """Takes the next number of a sequence of a model.

        Raises:
            UnSupportedError: The dialect has no sequences.
        """
        raise self.get_unsupported_error("Sequences")
