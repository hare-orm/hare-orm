"""Shared indexability and foreign-key rules for inspected model declarations.

Column types are mapped by each dialect's schema introspector.
"""

from __future__ import annotations

from hare.declarations.declaration_value_serializer import DeclarationValueSerializer
from hare.fields.enums import OnDelete
from hare.fields.relations.constants import PROTECT_DB_ON_DELETE_DEFERRAL
from hare.inspectdb.constants import FOREIGN_KEY_NO_ACTION


class InspectedFieldRules:
    """Indexability and foreign-key rules used when reconstructing field declarations."""

    @staticmethod
    def is_indexable_field_path(path: str) -> bool:
        """Whether the field class at ``path`` accepts unique=/db_index= and index membership.

        Args:
            path: Dotted field class path, as returned by map_column_type().

        Returns:
            The field class's own ``indexable`` flag.
        """
        return bool(getattr(DeclarationValueSerializer.get_callable(path), "indexable", True))

    @staticmethod
    def get_foreign_key_rules(
        on_delete: OnDelete, on_update: str, deferral: str | None
    ) -> tuple[OnDelete, str | None]:
        """Maps a FOREIGN KEY's rules onto what a ForeignKeyField declares: a deferrable NO ACTION
        checked at once is ``PROTECT``'s; an ON UPDATE action and any other deferral no relation
        declares.

        Args:
            on_delete: The ON DELETE action read off the database.
            on_update: The ON UPDATE action.
            deferral: The deferral, None where the database doesn't tell.

        Returns:
            The on_delete, and a TODO reason naming what no relation declares.
        """
        if on_delete == OnDelete.NO_ACTION and deferral == PROTECT_DB_ON_DELETE_DEFERRAL:
            on_delete, deferral = OnDelete.PROTECT, ""
        undeclared = [f"ON UPDATE {on_update}"] if on_update != FOREIGN_KEY_NO_ACTION else []
        if deferral:
            undeclared.append(deferral)
        if not undeclared:
            return on_delete, None
        return on_delete, (
            f"the database's FOREIGN KEY is {' '.join(undeclared)}, which no relation declares - a "
            "migration of the model creates it without; keep it with a RunSQL migration of your own"
        )

    @staticmethod
    def get_on_delete_fallback(
        on_delete: OnDelete, has_column_default: bool, nullable: bool
    ) -> tuple[OnDelete, str | None]:
        """Maps an ON DELETE action onto what a ForeignKeyField takes: SET NULL of a NOT NULL column
        fails the delete - NO_ACTION; SET DEFAULT without a column DEFAULT sets NULL (nullable) or
        fails the delete (NOT NULL) - SET_NULL / NO_ACTION.

        Args:
            on_delete: The action read off the database.
            has_column_default: Whether every key column has a DEFAULT.
            nullable: Whether the key columns accept NULL.

        Returns:
            The on_delete, and a TODO reason when it differs from the database's.
        """
        if on_delete == OnDelete.SET_NULL and not nullable:
            return OnDelete.NO_ACTION, (
                "the database declares ON DELETE SET NULL on a NOT NULL column, so a delete of the referenced "
                "row fails - rendered as NO_ACTION; make the column nullable (null=True) and on_delete=SET_NULL "
                "if the relation is meant to be cleared"
            )
        if on_delete != OnDelete.SET_DEFAULT or has_column_default:
            return on_delete, None
        fallback = OnDelete.SET_NULL if nullable else OnDelete.NO_ACTION
        return fallback, (
            f"the database declares ON DELETE SET DEFAULT but the column has no DEFAULT, so it resets to "
            f"NULL or fails - rendered as {fallback.name}; add a db_default and on_delete=SET_DEFAULT "
            "if a real fallback row is intended"
        )
