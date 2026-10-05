from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, TypeVar

from hare.exceptions import (
    ConfigurationError,
)
from hare.fields.constants import CASCADE, PROTECT, SET_DEFAULT
from hare.fields.enums import OnDelete, RelationLoadStrategy, RelationType
from hare.fields.relations.constants import PROTECT_DB_ON_DELETE_SQL
from hare.fields.relations.swappable_model_reference import SwappableModelReference

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.filters.lookups.field_lookup import FieldLookup
from hare.fields.relations.fields.relational_field import RelationalField

TModel = TypeVar("TModel", bound="Model")


class ManyToManyFieldInstance(RelationalField[TModel]):
    """A many-to-many relation - what ``ManyToManyField()`` makes: rows of another model linked through a
    through table."""

    relation_type = RelationType.MANY_TO_MANY
    is_multi_valued = True
    #: Indexes the key columns of the automatic through table that its unique index doesn't lead with.
    indexed_by_default = True

    def __init__(
        self,
        model_name: type[TModel] | str | SwappableModelReference,
        through: str | type[Model] | SwappableModelReference | None = None,
        forward_key: str | None = None,
        backward_key: str = "",
        related_name: str = "",
        on_delete: OnDelete = CASCADE,
        field_type: type[TModel] = None,  # type: ignore[assignment]
        unique: bool = True,
        lazy: Literal[RelationLoadStrategy.SELECT] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(field_type, unique=unique, **kwargs)
        self.validate_model_name(model_name)
        self.model_name = model_name
        self.related_name: str = related_name
        if lazy is not None and lazy != RelationLoadStrategy.SELECT:
            # "joined" makes no sense for a many-to-many - a JOIN through the through-table
            # multiplies rows, which is exactly what select_related() (and thus lazy="joined")
            # doesn't do; only prefetch_related()'s separate-query strategy applies here.
            raise ConfigurationError("lazy on a ManyToManyField can only be 'select' or None")
        self.lazy = lazy
        # Left empty when not given - Apps fills the default once the related model is resolved (a
        # composite key needs its key names). Whether a key was given is recorded: deconstruct()
        # writes only a given one, as a derived key replayed from a migration would be expanded
        # again.
        self._forward_key_explicit = bool(forward_key)
        self._backward_key_explicit = bool(backward_key)
        self.forward_key: str = forward_key or ""
        self.forward_keys: tuple[str, ...] = ()
        self.backward_key: str = backward_key
        self.backward_keys: tuple[str, ...] = ()
        # The through table: its name, or a model declared with its own fields - an "app.Model"
        # string is a deferred model reference. Resolved by Apps, never overwritten.
        self.through_model: type[Model] | str | SwappableModelReference | None = None
        # The through model's resolved class.
        self.through_model_class: type[Model] | None = None
        self.through: str = ""
        if through is not None and (not isinstance(through, str) or self.is_model_reference_string(through)):
            self.through_model = through
            if not isinstance(through, (str, SwappableModelReference)):
                self.through_model_class = through
        else:
            self.through = through or ""
        self.through_schema: str | None = None
        self._generated: bool = False
        self.validate_on_delete(on_delete)
        # For hare's automatic through table only - a through model's own foreign keys carry their
        # on_delete. A through table column has no default to reset to.
        if self.through_model is None and on_delete == SET_DEFAULT:
            raise ConfigurationError(
                "on_delete=SET_DEFAULT is not supported on a ManyToManyField - there is no "
                "way to declare a default target row for the through-table's columns"
            )
        self.on_delete = on_delete

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        path, args, kwargs = super().deconstruct()
        # Only a key the caller gave is written.
        if not self._forward_key_explicit:
            kwargs.pop("forward_key", None)
        if not self._backward_key_explicit:
            kwargs.pop("backward_key", None)
        return path, args, kwargs

    def get_lookups(self) -> dict[str, FieldLookup]:
        """The lookups of a many-to-many relation itself - the related rows' keys."""
        # Local import: the filters package imports the fields package.
        from hare.query.filters.lookups.field_lookups import FieldLookups

        return FieldLookups.get_many_to_many(self)

    def get_through_index_keys(self) -> list[tuple[str, ...]]:
        """The key column groups of the automatic through table that get a plain index.

        Returns:
            The backward key columns unless the unique index over the pair already leads with
            them, then the forward key columns - none when ``db_index`` is off.
        """
        if not self.index:
            return []
        through_index_keys: list[tuple[str, ...]] = [] if self.unique else [self.backward_keys]
        through_index_keys.append(self.forward_keys)
        return through_index_keys

    @property
    def db_on_delete(self) -> str:
        """The ``ON DELETE`` clause text to emit in DDL - ``PROTECT`` has no SQL keyword of its own
        (it's enforced in Python before the DELETE is issued), so the database gets a deferrable
        ``NO ACTION`` backstop instead."""
        return PROTECT_DB_ON_DELETE_SQL if self.on_delete == PROTECT else self.on_delete
