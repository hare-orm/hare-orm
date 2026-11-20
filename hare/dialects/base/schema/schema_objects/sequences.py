from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hare.models.model_binding import ModelBinding

from hare.ddl.schema_objects.database_sequence import DatabaseSequence
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart


class Sequences(SchemaEditorPart):
    """Sequences a model declares: created with their owner, dropped, altered, renamed, and their next
    value read."""

    __slots__ = ()

    def get_sequence_create_sqls(
        self, binding: ModelBinding, sequence: DatabaseSequence, safe: bool = False
    ) -> list[str]:
        """The statements creating a sequence of a model - without its owner column, which
        ``get_sequence_owner_sqls()`` sets once the table exists.

        Args:
            binding: The model declaring it.
            sequence: The sequence.
            safe: Create it only when it doesn't exist yet.

        Raises:
            UnSupportedError: The dialect has no sequences.
        """
        raise self.get_unsupported_error("Sequences")

    def get_sequence_owner_sqls(self, binding: ModelBinding, sequence: DatabaseSequence) -> list[str]:
        """The statements making a sequence of a model owned by its column - none without
        ``owned_by``.

        Raises:
            UnSupportedError: The dialect has no sequences.
        """
        raise self.get_unsupported_error("Sequences")

    async def create_sequence(self, binding: ModelBinding, sequence: DatabaseSequence) -> None:
        """Creates a sequence of a model, owned by its column."""
        await self.editor.run_sqls(
            [*self.get_sequence_create_sqls(binding, sequence), *self.get_sequence_owner_sqls(binding, sequence)]
        )

    async def drop_sequence(self, binding: ModelBinding, sequence: DatabaseSequence) -> None:
        """Drops a sequence of a model.

        Raises:
            UnSupportedError: The dialect has no sequences.
        """
        raise self.get_unsupported_error("Sequences")

    async def alter_sequence(
        self, binding: ModelBinding, old_sequence: DatabaseSequence, new_sequence: DatabaseSequence
    ) -> None:
        """Gives a sequence of a model its new settings - its current number is kept.

        Raises:
            UnSupportedError: The dialect has no sequences.
        """
        raise self.get_unsupported_error("Sequences")

    async def rename_sequence(
        self, binding: ModelBinding, old_sequence: DatabaseSequence, new_sequence: DatabaseSequence
    ) -> None:
        """Renames a sequence of a model.

        Raises:
            UnSupportedError: The dialect has no sequences.
        """
        raise self.get_unsupported_error("Sequences")

    async def get_next_sequence_value(self, binding: ModelBinding, sequence: DatabaseSequence) -> int:
        """Takes the next number of a sequence of a model.

        Raises:
            UnSupportedError: The dialect has no sequences.
        """
        raise self.get_unsupported_error("Sequences")

    def get_schema_move_plan(
        self, binding: ModelBinding, sequence: DatabaseSequence, old_schema: str | None, new_schema: str | None
    ) -> SchemaChangePlan:
        """Prepares moving a declared object with its model to another schema.

        Args:
            binding: The model, rendered in its old schema.
            sequence: The object.
            old_schema: The schema it is in; None for the connection's current schema.
            new_schema: The schema it moves to; None for the connection's current schema.

        Raises:
            UnSupportedError: The dialect moves none.
        """
        raise self.get_unsupported_error("Sequences moved to another schema")
