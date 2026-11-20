from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar, overload

from hare.exceptions import (
    ConfigurationError,
)
from hare.fields.enums import OnDelete
from hare.fields.field import Field
from hare.fields.relations.bound_relation import BoundRelation
from hare.fields.relations.constants import PROTECT_DB_ON_DELETE_SQL
from hare.fields.relations.swappable_model_reference import SwappableModelReference

if TYPE_CHECKING:
    from hare.models import Model
    from hare.models.model_binding import ModelBinding

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
        self.related_model = related_model
        self.to_field: str | tuple[str, ...] | None = to_field
        self.to_field_instance = None
        # Plural forms of to_field/to_field_instance/source_field, set once the relation resolves -
        # one entry for a single-column key.
        self.to_field_names = ()
        self.to_field_instances = ()
        self.source_fields = ()
        # The database columns behind source_fields - a source_field= override makes them differ
        # from the field names.
        self.db_column_names = ()

    def _get_common_constructor_arguments(self) -> dict[str, Any]:
        """Include the relation's constraint and referenced-field declarations."""
        kwargs = super()._get_common_constructor_arguments()
        if self.db_constraint is False:
            kwargs["db_constraint"] = False
        if self.to_field is not None:
            kwargs["to_field"] = self.to_field
        return kwargs

    def _get_constructor_argument_value(self, name: str) -> Any:
        """Write a declared model class as its application label; preserve swappable references."""
        value = super()._get_constructor_argument_value(name)
        if (
            name == "model_name"
            and value is not None
            and not isinstance(value, str)
            and hasattr(value, "_declaration")
        ):
            binding = self.get_binding().model_binding
            app = binding.graph.get_binding(value).app if binding is not None else value._declaration.app
            return f"{app}.{value.__name__}" if app is not None else value
        return value

    @staticmethod
    def get_db_on_delete(on_delete: OnDelete) -> str:
        """Return the database action backing the relation's deletion policy."""
        return PROTECT_DB_ON_DELETE_SQL if on_delete == OnDelete.PROTECT else on_delete

    def get_relation_binding(self) -> BoundRelation:
        """Return the target/key state of this field's current publication."""
        binding = self.get_binding()
        if binding.relation is None:
            binding.relation = BoundRelation()
        return binding.relation

    def get_related_binding(self) -> ModelBinding:
        """The target node in this field's prepared graph, never another context's class metadata."""
        relation = self.get_relation_binding()
        if relation.related_binding is None:
            source = self.get_binding().model_binding
            if source is None or relation.related_model is None:
                raise ConfigurationError("A relation needs a prepared model graph")
            relation.related_binding = source.graph.get_binding(relation.related_model)
        return relation.related_binding

    @property
    def related_model(self) -> type[TModel]:
        return self.get_relation_binding().related_model  # type: ignore[return-value]

    @related_model.setter
    def related_model(self, value: type[TModel]) -> None:
        self.get_relation_binding().related_model = value

    @property
    def to_field_instance(self) -> Field[Any]:
        return self.get_relation_binding().to_field_instance  # type: ignore[return-value]

    @to_field_instance.setter
    def to_field_instance(self, value: Field[Any]) -> None:
        self.get_relation_binding().to_field_instance = value

    @property
    def to_field_names(self) -> tuple[str, ...]:
        return self.get_relation_binding().to_field_names

    @to_field_names.setter
    def to_field_names(self, value: tuple[str, ...]) -> None:
        self.get_relation_binding().to_field_names = value

    @property
    def to_field_instances(self) -> tuple[Field[Any], ...]:
        return self.get_relation_binding().to_field_instances

    @to_field_instances.setter
    def to_field_instances(self, value: tuple[Field[Any], ...]) -> None:
        self.get_relation_binding().to_field_instances = value

    @property
    def to_column_names(self) -> tuple[str, ...]:
        """The referenced physical columns in the key's order."""
        return tuple(field.source_field or field.model_field_name for field in self.to_field_instances)

    @property
    def source_fields(self) -> tuple[str, ...]:
        return self.get_relation_binding().source_fields

    @source_fields.setter
    def source_fields(self, value: tuple[str, ...]) -> None:
        self.get_relation_binding().source_fields = value

    @property
    def db_column_names(self) -> tuple[str, ...]:
        return self.get_relation_binding().db_column_names

    @db_column_names.setter
    def db_column_names(self, value: tuple[str, ...]) -> None:
        self.get_relation_binding().db_column_names = value

    def get_declaration_values(self) -> dict[str, Any]:
        """Remove the previous relation binding before historical models bind it again."""
        values = super().get_declaration_values()
        values.update(
            related_model=None,
            to_field_instance=None,
            to_field_names=(),
            to_field_instances=(),
            source_fields=(),
            db_column_names=(),
            field_type=None,
        )
        return values

    if TYPE_CHECKING:

        @overload
        def __get__(self, obj: None, owner: type[Model]) -> RelationalField[TModel]: ...

        @overload
        def __get__(self, obj: Model, owner: type[Model]) -> TModel: ...

        @overload
        def __get__(self, obj: object, owner: type[object]) -> RelationalField[TModel]: ...

        def __get__(self, obj: object, owner: type[object]) -> RelationalField[TModel] | TModel: ...

        @overload
        def __set__(self, obj: Model, value: TModel) -> None: ...

        @overload
        def __set__(self, obj: object, value: Field[TModel] | None) -> None: ...

        def __set__(self, obj: object, value: Field[TModel] | TModel | None) -> None: ...

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
            if not hasattr(model_name, "_declaration"):
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
