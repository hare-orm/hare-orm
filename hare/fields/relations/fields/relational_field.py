from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar, overload

from hare.exceptions import (
    ConfigurationError,
)
from hare.fields.enums import OnDelete
from hare.fields.field import Field
from hare.fields.relations.swappable_model_reference import SwappableModelReference

if TYPE_CHECKING:
    from hare.models import Model

TModel = TypeVar("TModel", bound="Model")


class RelationalField(Field[TModel]):
    """The base of the fields relating a model to another: the related model, the fields of it the
    relation points at (``to_field``), and whether the database enforces the relation
    (``db_constraint``)."""

    has_db_field = False
    #: Whether the relation can resolve to several rows (a many-to-many or reverse foreign key) - a
    #: separate .filter() call over it gets a JOIN of its own.
    is_multi_valued = False

    def __init__(
        self,
        related_model: type[TModel],
        to_field: str | tuple[str, ...] | None = None,
        db_constraint: bool = True,
        **kwargs: Any,
    ) -> None:
        # Set before Field.__init__ runs - FK-specific hooks it calls (see
        # ForeignKeyFieldInstance.is_db_default_redundant_with_default) read it.
        self.db_constraint = db_constraint
        super().__init__(**kwargs)
        self.raise_if_not_to_field(to_field)
        self.related_model: type[TModel] = related_model
        self.to_field: str | tuple[str, ...] | None = to_field
        self.to_field_instance: Field[Any] = None  # type: ignore[assignment]
        # Plural forms of to_field/to_field_instance/source_field, set once the relation resolves -
        # one entry for a single-column key.
        self.to_field_names: tuple[str, ...] = ()
        self.to_field_instances: tuple[Field[Any], ...] = ()
        self.source_fields: tuple[str, ...] = ()
        # The database columns behind source_fields - a source_field= override makes them differ
        # from the field names.
        self.db_column_names: tuple[str, ...] = ()

    if TYPE_CHECKING:

        @overload
        def __get__(self, obj: None, owner: type[Model]) -> RelationalField[TModel]: ...

        @overload
        def __get__(self, obj: Model, owner: type[Model]) -> TModel: ...

        @overload
        def __get__(self, obj: object, owner: type[object]) -> RelationalField[TModel]: ...

        def __get__(self, obj: object, owner: type[object]) -> RelationalField[TModel] | TModel: ...

        def __set__(self, obj: Model, value: TModel) -> None: ...

    @property
    def has_database_constraint(self) -> bool:
        """Whether the database enforces this relation: ``db_constraint=True`` and the connection of
        the referencing table enforces foreign keys now (``DatabaseClient.enforces_foreign_keys()``).
        Otherwise hare runs ``on_delete`` and ``PROTECT`` itself.
        """
        if not self.db_constraint:
            return False
        return self.get_referencing_model().get_routed_connection(for_write=True).enforces_foreign_keys()

    def get_referencing_model(self) -> type[Model]:
        """The model whose table holds the relation's key - its database decides whether the
        relation is enforced.

        Returns:
            The model - the related one for a relation generated on the other side.
        """
        return self.related_model if getattr(self, "_generated", False) else self.model

    def get_annotation(self) -> Any:
        """The related model - a list of it for a to-many relation - ``| None`` when a to-one
        relation is nullable."""
        if self.is_multi_valued:
            return list[self.related_model]  # type: ignore[name-defined]
        return super().get_annotation()

    @staticmethod
    def validate_on_delete(on_delete: OnDelete) -> None:
        """Refuses an ``on_delete`` that is no ``OnDelete`` action.

        Args:
            on_delete: The action.

        Raises:
            ConfigurationError: It is none of them.
        """
        if on_delete not in set(OnDelete):
            raise ConfigurationError(
                "on_delete can only be CASCADE, RESTRICT, SET_NULL, SET_DEFAULT, NO_ACTION or PROTECT"
            )

    @classmethod
    def validate_model_name(cls, model_name: str | type[Model] | SwappableModelReference) -> None:
        """Refuses a related model given as neither a model class, a ``swappable()`` reference nor a
        ``"app.Model"`` name.

        Args:
            model_name: The related model as the field was given it.

        Raises:
            ConfigurationError: It is none of them.
        """
        if isinstance(model_name, SwappableModelReference):
            return
        if not isinstance(model_name, str):
            if not hasattr(model_name, "_meta"):
                raise ConfigurationError(
                    f"{cls.__name__}({model_name!r}) is invalid. model_name must be string or type[hare.models.Model]"
                )
        elif len(model_name.split(".")) != 2:
            field_type = cls.__name__.replace("Instance", "")
            raise ConfigurationError(f'{field_type} accepts model name in format "app.Model"')

    @staticmethod
    def raise_if_not_to_field(to_field: Any) -> None:
        """Refuses a ``to_field`` that is no field name, nor a tuple of them.

        Args:
            to_field: The referenced field, None for the primary key.

        Raises:
            ConfigurationError: It is something else.
        """
        if to_field is None or (isinstance(to_field, str) and to_field):
            return
        if (
            not isinstance(to_field, (list, tuple))
            or not to_field
            or not all(isinstance(name, str) and name for name in to_field)
        ):
            raise ConfigurationError(f"to_field takes a field name or a tuple of them, got {to_field!r}")

    @staticmethod
    def is_model_reference_string(value: str) -> bool:
        """Whether ``value`` is an ``"app.Model"`` model reference rather than a table name."""
        parts = value.split(".")
        return len(parts) == 2 and all(part.isidentifier() for part in parts)
