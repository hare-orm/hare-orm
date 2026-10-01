from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Literal, TypeVar

from hare.exceptions import (
    ConfigurationError,
)
from hare.fields.constants import CASCADE, PROTECT, PROTECT_DB_ON_DELETE_SQL, SET_DEFAULT, SET_NULL
from hare.fields.enums import OnDelete, RelationLoadStrategy, RelationType
from hare.fields.swappable import SwappableModelReference

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
from hare.fields.relations.fields.relational_field import RelationalField

TModel = TypeVar("TModel", bound="Model")


class ForeignKeyFieldInstance(RelationalField[TModel]):
    relation_type = RelationType.FOREIGN_KEY
    indexed_by_default = True
    #: Whether primary_key=True is accepted - only a one-to-one relation can be a primary key.
    allows_primary_key: ClassVar[bool] = False
    #: Whether unique=True is accepted - only a one-to-one relation emits a UNIQUE column.
    allows_unique: ClassVar[bool] = False

    def __init__(
        self,
        model_name: type[Model] | str | SwappableModelReference,
        related_name: str | None | Literal[False] = None,
        on_delete: OnDelete = CASCADE,
        lazy: RelationLoadStrategy | None = None,
        **kwargs: Any,
    ) -> None:
        if (kwargs.get("primary_key") or kwargs.get("pk")) and not self.allows_primary_key:
            raise ConfigurationError(
                "ForeignKeyField can't be a primary key - a primary key column holds at most one row per "
                "related object, which is OneToOneField(..., primary_key=True)"
            )
        # unique=True never reached the DDL of a ForeignKeyField - the column silently stayed
        # non-unique. A historical migration replays whatever schema it already applied.
        if kwargs.get("unique") and not self.allows_unique and not self.replaying_migration.get():
            raise ConfigurationError(
                "ForeignKeyField doesn't support unique=True - a relation with at most one row per related "
                "object is OneToOneField(...)"
            )
        # Set before Field.__init__ runs - is_db_default_redundant_with_default() reads it.
        self.on_delete = on_delete
        super().__init__(None, **kwargs)  # type:ignore[arg-type]
        self.validate_model_name(model_name)
        self.model_name = model_name
        self.related_name = related_name
        self.validate_on_delete(on_delete)
        if on_delete == SET_NULL and not bool(kwargs.get("null")):
            raise ConfigurationError("If on_delete is SET_NULL, then field must have null=True set")
        if on_delete == SET_DEFAULT and not self.replaying_migration.get():
            requirement_error = self.get_set_default_requirement_error()
            if requirement_error is not None:
                raise ConfigurationError(f"If on_delete is SET_DEFAULT, then {requirement_error}")
        if lazy is not None and lazy not in set(RelationLoadStrategy):
            raise ConfigurationError("lazy can only be 'joined', 'select', or None")
        self.lazy = lazy

    def is_db_default_redundant_with_default(self) -> bool:
        """Whether a db_default next to a default= can never take effect.

        Returns:
            False when the database itself performs ``ON DELETE SET DEFAULT`` and so reads the
            column's DDL default, True otherwise.
        """
        return not (self.on_delete == SET_DEFAULT and self.db_constraint)

    def get_set_default_requirement_error(self) -> str | None:
        """Describes what this field still lacks to support ``on_delete=SET_DEFAULT``.

        Returns:
            The unmet requirement, or None when the field is valid.
        """
        # With a constraint the database runs ON DELETE SET DEFAULT and sees only db_default -
        # required then. Without one hare's cascade takes default first, else db_default.
        if self.has_db_default():
            return None
        if self.db_constraint:
            return (
                "field must have db_default set when db_constraint=True: the database itself performs "
                "ON DELETE SET DEFAULT and resets the column to its SQL DEFAULT, but a Python-side "
                "default= is never emitted into the DDL, so it would be reset to NULL (or fail on a "
                "NOT NULL column) instead of default= - set db_default=..., or db_constraint=False so "
                "hare applies default= itself"
            )
        if self.default is None:
            return "field must have default or db_default set"
        return None

    @property
    def db_on_delete(self) -> str:
        """The ``ON DELETE`` clause text to emit in DDL - ``PROTECT`` has no SQL keyword of its own
        (it's enforced in Python before the DELETE is issued), so the database gets a deferrable
        ``NO ACTION`` backstop instead."""
        return PROTECT_DB_ON_DELETE_SQL if self.on_delete == PROTECT else self.on_delete

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        path, args, kwargs = super().deconstruct()
        # Relation initialization repoints source_field at the shadow attribute name, while
        # db_column_names keeps the real column a declared source_field= names. A composite key's
        # columns are always named after the field - it takes no source_field.
        if len(self.db_column_names) == 1:
            kwargs["source_field"] = self.db_column_names[0]
        elif len(self.db_column_names) > 1:
            kwargs.pop("source_field", None)
        return path, args, kwargs


ForeignKeyNullableRelation = ForeignKeyFieldInstance[TModel] | None
"""
Type hint for the result of accessing the :func:`.ForeignKeyField` field in the model
when obtained model can be nullable.
"""


ForeignKeyRelation = ForeignKeyFieldInstance[TModel]
"""
Type hint for the result of accessing the :func:`.ForeignKeyField` field in the model.
"""
