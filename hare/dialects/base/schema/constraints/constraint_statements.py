from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.fields.field import Field
from hare.models import Model


class ConstraintStatements(SchemaEditorPart):
    """The SQL of constraints beside a table's columns: unique constraints and the partial unique
    indexes standing for them, exclusion constraints, constraints added without validation and
    validated later."""

    __slots__ = ()

    def check_unique_constraint_supported(self, constraint: UniqueConstraint) -> None:
        """Rejects a unique constraint the dialect has no DDL for.

        Args:
            constraint: The unique constraint.

        Raises:
            ConfigurationError: It is deferrable with a condition (a partial unique constraint is
                an index, and an index can't be deferred).
            UnSupportedError: It has a condition on a dialect without partial indexes, is
                deferrable on a dialect without deferrable constraints, or sets
                ``nulls_distinct`` the connection's server has no syntax for.
        """
        dialect = self.editor.client.dialect
        if constraint.condition and not dialect.features.supports_partial_indexes:
            raise UnSupportedError(f"Partial unique indexes (condition) are not supported on {dialect}")
        if constraint.deferrable and constraint.condition:
            raise ConfigurationError("UniqueConstraint.deferrable is not supported together with condition.")
        if constraint.deferrable and not dialect.features.supports_deferrable_constraints:
            raise UnSupportedError(f"DEFERRABLE unique constraints are not supported on {dialect}")
        constraint.raise_if_unsupported(self.editor.client.features, dialect)

    def exclusion_constraint_sql(self, model: type[Model], constraint: ExclusionConstraint) -> str:
        """Returns the definition of an exclusion constraint.

        Args:
            model: The constrained model.
            constraint: The constraint.

        Returns:
            The definition, as it follows ``ADD`` and stands in ``CREATE TABLE``.

        Raises:
            UnSupportedError: The dialect has no exclusion constraints.
        """
        raise UnSupportedError(f"ExclusionConstraint is not supported on {self.editor.client.dialect}")

    async def add_check_constraint_not_valid(self, model: type[Model], constraint: CheckConstraint) -> None:
        """Adds a CHECK constraint that only new and updated rows must pass - the existing rows are
        checked later, by ``validate_constraint()``. A dialect that can't leave the existing rows
        unchecked (``Features.supports_not_valid_constraints`` False) adds the constraint as usual,
        which checks every row.

        Args:
            model: The constrained model.
            constraint: The check constraint.
        """
        await self.editor.add_constraint(model, constraint)

    async def add_unique_constraint_using_index(
        self, model: type[Model], constraint: UniqueConstraint, index_name: str
    ) -> None:
        """Adds a unique constraint over a unique index the table already has, which becomes the
        constraint's. A dialect that can't hand an index over builds the constraint's own and drops
        the index.

        Args:
            model: The constrained model.
            constraint: The named unique constraint, without a condition.
            index_name: The existing unique index's name.
        """
        await self.editor.add_constraint(model, constraint)
        await self.editor.run_sql(
            self.editor.DROP_INDEX_TEMPLATE.format(
                name=self.editor.qualify_table_name(index_name, model._meta.schema),
                table=self.editor.qualify_table_name(model._meta.db_table, model._meta.schema),
            )
        )

    async def validate_constraint(self, model: type[Model], name: str) -> None:
        """Checks the existing rows against a constraint added without checking them; nothing to
        do where every constraint is checked as it is added.

        Args:
            model: The constrained model.
            name: The constraint's name.
        """

    async def rename_partial_unique_index(
        self,
        model: type[Model],
        old_constraint: UniqueConstraint,
        new_constraint: UniqueConstraint | CheckConstraint | ExclusionConstraint,
    ) -> None:
        """Renames the unique index a unique constraint with a condition is - a partial unique
        constraint has no table constraint to rename (see ``add_constraint()``). Without
        ``ALTER INDEX ... RENAME`` the index is dropped and created again under the new name.

        Args:
            model: The constrained model.
            old_constraint: The constraint's old definition.
            new_constraint: Its new definition.

        Raises:
            TypeError: The new constraint isn't a unique constraint.
        """
        if not isinstance(new_constraint, UniqueConstraint):
            raise TypeError(f"Cannot rename UniqueConstraint to {type(new_constraint).__name__}")
        old_name = self.editor.constraint_names.get_partial_unique_index_name(model, old_constraint)
        new_name = self.editor.constraint_names.get_partial_unique_index_name(model, new_constraint)
        if old_name == new_name:
            return
        if self.editor.RENAME_INDEX_TEMPLATE:
            await self.editor.run_sql(
                self.editor.RENAME_INDEX_TEMPLATE.format(
                    old_name=self.editor.qualify_table_name(old_name, model._meta.schema),
                    new_name=self.editor.quote(new_name),
                )
            )
            return
        await self.editor.run_sql(
            self.editor.DROP_INDEX_TEMPLATE.format(
                name=self.editor.qualify_table_name(old_name, model._meta.schema),
                table=self.editor.qualify_table_name(model._meta.db_table, model._meta.schema),
            )
        )
        await self.editor.add_constraint(model, new_constraint)

    @classmethod
    def get_nulls_distinct_sql(cls, nulls_distinct: bool) -> str:
        """The clause of a unique constraint stating whether NULLs collide.

        Args:
            nulls_distinct: Whether rows with NULLs stay distinct.

        Returns:
            The clause with its leading space; ``""`` by default.
        """
        return ""

    @classmethod
    def get_exclusion_constraint_extension(
        cls, constraint: ExclusionConstraint, fields_by_name: Mapping[str, Field[Any]]
    ) -> str | None:
        """The database extension an exclusion constraint needs on this dialect beyond the
        constraint support itself - the schema editor and the migration autodetector create it
        with the constraint's model.

        Args:
            constraint: The constraint.
            fields_by_name: The fields of the constraint's model, by name.

        Returns:
            The extension's name, None by default.
        """
        return None

    @classmethod
    def get_without_overlaps_extension(
        cls, field_names: Sequence[str], fields_by_name: Mapping[str, Field[Any]]
    ) -> str | None:
        """The database extension a key compared ``WITHOUT OVERLAPS`` needs on this dialect - the
        schema editor and the migration autodetector create it with the key's model.

        Args:
            field_names: The key's fields, the range last.
            fields_by_name: The fields of the key's model, by name.

        Returns:
            The extension's name, None by default.
        """
        return None
