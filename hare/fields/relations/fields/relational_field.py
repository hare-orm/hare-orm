from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Generator
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, overload

from hare.exceptions import (
    ConfigurationError,
)
from hare.fields.base.field import Field
from hare.fields.enums import OnDelete
from hare.fields.swappable import SwappableModelReference

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model

TModel = TypeVar("TModel", bound="Model")


class RelationalField(Field[TModel]):
    has_db_field = False
    #: Whether the relation can resolve to several rows (a many-to-many or reverse foreign key) - a
    #: separate .filter() call over it gets a JOIN of its own.
    is_multi_valued = False
    #: True while a historical migration file is being imported - its fields describe a schema
    #: that was already applied, so today's construction rules (the SET_DEFAULT requirement
    #: checked by ``get_set_default_requirement_error``, the ``db_index`` default) don't apply.
    replaying_migration: ClassVar[contextvars.ContextVar[bool]] = contextvars.ContextVar(
        "hare_replaying_migration", default=False
    )
    #: The ``db_index`` a relation gets when none is passed. A migration file replays an omitted
    #: ``db_index`` as False, the value it was written with.
    indexed_by_default: ClassVar[bool] = False

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
        if kwargs.get("db_index") is None:
            kwargs["db_index"] = self.indexed_by_default and not self.replaying_migration.get()
        super().__init__(**kwargs)
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
        def __get__(self, instance: None, owner: type[Model]) -> RelationalField[TModel]: ...

        @overload
        def __get__(self, instance: Model, owner: type[Model]) -> TModel: ...

        def __get__(self, instance: Model | None, owner: type[Model]) -> RelationalField[TModel] | TModel: ...

        def __set__(self, instance: Model, value: TModel) -> None: ...

    @classmethod
    @contextlib.contextmanager
    def replaying_migration_scope(cls) -> Generator[None]:
        """Builds the relations constructed inside the block the way a historical migration file declares them."""
        token = cls.replaying_migration.set(True)
        try:
            yield
        finally:
            cls.replaying_migration.reset(token)

    @property
    def has_database_constraint(self) -> bool:
        """Whether the database enforces this relation: ``db_constraint=True`` and the database of the
        referencing table supports foreign keys. Otherwise hare runs ``on_delete`` and ``PROTECT``
        itself.
        """
        if not self.db_constraint:
            return False
        referencing_model = self.related_model if getattr(self, "_generated", False) else self.model
        return referencing_model.get_connection(for_write=True).dialect.supports_foreign_keys

    def get_annotation(self) -> Any:
        """The related model - a list of it for a to-many relation - ``| None`` when a to-one
        relation is nullable."""
        if self.is_multi_valued:
            return list[self.related_model]  # type: ignore[name-defined]
        return super().get_annotation()

    @staticmethod
    def validate_on_delete(on_delete: OnDelete) -> None:
        if on_delete not in set(OnDelete):
            raise ConfigurationError(
                "on_delete can only be CASCADE, RESTRICT, SET_NULL, SET_DEFAULT, NO_ACTION or PROTECT"
            )

    @classmethod
    def validate_model_name(cls, model_name: str | type[Model] | SwappableModelReference) -> None:
        if isinstance(model_name, SwappableModelReference):
            return
        if not isinstance(model_name, str):
            model_class: type[Model] = model_name
            try:
                model_class._meta
            except AttributeError:
                raise ConfigurationError(
                    f"{cls.__name__}({model_name!r}) is invalid. model_name must be string or type[hare.models.Model]"
                ) from None
        elif len(model_name.split(".")) != 2:
            field_type = cls.__name__.replace("Instance", "")
            raise ConfigurationError(f'{field_type} accepts model name in format "app.Model"')

    @staticmethod
    def is_model_reference_string(value: str) -> bool:
        """Whether ``value`` is an ``"app.Model"`` model reference rather than a table name."""
        parts = value.split(".")
        return len(parts) == 2 and all(part.isidentifier() for part in parts)
