from __future__ import annotations

from typing import TYPE_CHECKING

from hare.ddl.schema_objects.database_sequence import DatabaseSequence
from hare.dialects.base.schema.schema_objects.sequences import Sequences
from hare.dialects.postgresql.schema.constants import (
    POSTGRESQL_SEQUENCE_ALTER_TEMPLATE,
    POSTGRESQL_SEQUENCE_CREATE_TEMPLATE,
    POSTGRESQL_SEQUENCE_DROP_TEMPLATE,
    POSTGRESQL_SEQUENCE_NEXT_VALUE_COLUMN,
    POSTGRESQL_SEQUENCE_NEXT_VALUE_TEMPLATE,
    POSTGRESQL_SEQUENCE_NO_OWNER_SQL,
    POSTGRESQL_SEQUENCE_OWNER_TEMPLATE,
    POSTGRESQL_SEQUENCE_RENAME_TEMPLATE,
)
from hare.models import Model

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor


class PostgresqlSequences(Sequences):
    """Sequences as PostgreSQL writes it."""

    __slots__ = ()

    editor: PostgresqlSchemaEditor

    def get_sequence_create_sqls(
        self, model: type[Model], sequence: DatabaseSequence, safe: bool = False
    ) -> list[str]:
        return [
            POSTGRESQL_SEQUENCE_CREATE_TEMPLATE.format(
                if_not_exists="IF NOT EXISTS " if safe else "",
                sequence=self.editor.qualify_object_name(model, sequence.name),
                options=self.get_sequence_options_sql(sequence, altering=False),
            )
        ]

    def get_sequence_owner_sqls(self, model: type[Model], sequence: DatabaseSequence) -> list[str]:
        if sequence.owned_by is None:
            return []
        return [
            POSTGRESQL_SEQUENCE_OWNER_TEMPLATE.format(
                sequence=self.editor.qualify_object_name(model, sequence.name),
                owner=self.get_sequence_owner_sql(model, sequence),
            )
        ]

    async def drop_sequence(self, model: type[Model], sequence: DatabaseSequence) -> None:
        await self.editor.run_sql(
            POSTGRESQL_SEQUENCE_DROP_TEMPLATE.format(sequence=self.editor.qualify_object_name(model, sequence.name))
        )

    async def alter_sequence(
        self, model: type[Model], old_sequence: DatabaseSequence, new_sequence: DatabaseSequence
    ) -> None:
        qualified_sequence = self.editor.qualify_object_name(model, new_sequence.name)
        await self.editor.run_sql(
            POSTGRESQL_SEQUENCE_ALTER_TEMPLATE.format(
                sequence=qualified_sequence, options=self.get_sequence_options_sql(new_sequence, altering=True)
            )
        )
        if old_sequence.owned_by != new_sequence.owned_by:
            await self.editor.run_sql(
                POSTGRESQL_SEQUENCE_OWNER_TEMPLATE.format(
                    sequence=qualified_sequence, owner=self.get_sequence_owner_sql(model, new_sequence)
                )
            )

    async def rename_sequence(
        self, model: type[Model], old_sequence: DatabaseSequence, new_sequence: DatabaseSequence
    ) -> None:
        if old_sequence.name == new_sequence.name:
            return
        await self.editor.run_sql(
            POSTGRESQL_SEQUENCE_RENAME_TEMPLATE.format(
                sequence=self.editor.qualify_object_name(model, old_sequence.name),
                new_name=self.editor.quote(new_sequence.name),
            )
        )

    async def get_next_sequence_value(self, model: type[Model], sequence: DatabaseSequence) -> int:
        sequence_literal = self.editor.client.dialect.literals.get_string_literal_sql(
            self.editor.qualify_object_name(model, sequence.name)
        )
        rows = await self.editor.client.execute_dicts(
            POSTGRESQL_SEQUENCE_NEXT_VALUE_TEMPLATE.format(
                sequence_literal=sequence_literal, column=POSTGRESQL_SEQUENCE_NEXT_VALUE_COLUMN
            )
        )
        return int(rows[0][POSTGRESQL_SEQUENCE_NEXT_VALUE_COLUMN])

    @staticmethod
    def get_sequence_options_sql(sequence: DatabaseSequence, altering: bool) -> str:
        """A sequence's settings as ``CREATE``/``ALTER SEQUENCE`` options - altering, an unset bound
        and start go back to their defaults."""
        options = [f" INCREMENT BY {sequence.increment}"]
        if sequence.minimum is not None:
            options.append(f" MINVALUE {sequence.minimum}")
        elif altering:
            options.append(" NO MINVALUE")
        if sequence.maximum is not None:
            options.append(f" MAXVALUE {sequence.maximum}")
        elif altering:
            options.append(" NO MAXVALUE")
        start = sequence.start
        if start is None and altering:
            if sequence.increment > 0:
                start = sequence.minimum if sequence.minimum is not None else 1
            else:
                start = sequence.maximum if sequence.maximum is not None else -1
        if start is not None:
            options.append(f" START WITH {start}")
        options.append(f" CACHE {sequence.cache}")
        options.append(" CYCLE" if sequence.cycle else " NO CYCLE")
        return "".join(options)

    def get_sequence_owner_sql(self, model: type[Model], sequence: DatabaseSequence) -> str:
        """The owner column of a sequence, or ``NONE``."""
        if sequence.owned_by is None:
            return POSTGRESQL_SEQUENCE_NO_OWNER_SQL
        column = self.editor.get_column_name(model, sequence.owned_by, f"DatabaseSequence {sequence.name!r} owned_by")
        return f"{self.editor.get_model_table_sql(model)}.{self.editor.quote(column)}"
