from __future__ import annotations

import inspect
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any, TypeVar

from hare.exceptions import DoesNotExist, ValidationError
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.fields.field import Field
from hare.fields.generated_field import GeneratedField
from hare.models.class_building.abstract_meta_inheritance import AbstractMetaInheritance
from hare.models.class_building.blind_indexes import BlindIndexes
from hare.models.class_building.field_comments import FieldComments
from hare.models.class_building.generic_foreign_keys import GenericForeignKeys
from hare.models.class_building.model_field_collection import ModelFieldCollection
from hare.models.class_building.model_managers import ModelManagers
from hare.models.class_building.primary_key_declaration import PrimaryKeyDeclaration
from hare.models.enums import ModelOption
from hare.models.model_declaration import ModelDeclaration
from hare.query.managers.manager import Manager

if TYPE_CHECKING:
    from hare.core.hare_context import HareContext
    from hare.models.model import Model
    from hare.models.model_binding import ModelBinding

TModel = TypeVar("TModel", bound="Model")


class ModelMeta(type):
    """The metaclass of models: collects a model's fields, managers and Meta options when its class is built."""

    __slots__ = ()

    def __new__(cls, name: str, bases: tuple[type, ...], attributes: dict[str, Any]) -> ModelMeta:
        attributes = ModelFieldCollection.snapshot_attributes(attributes)
        primary_key_attribute: str | tuple[str, ...] = "id"
        # The bootstrap base has no model parent. A user's subclass may also be named Model.
        has_model_parent = any(isinstance(base, ModelMeta) for base in bases)
        # The C3 order of the ancestors, nearest first.
        canonical_mro = cls._linearize_mro(bases)
        meta_class = AbstractMetaInheritance.get_merged_meta_class(
            attributes["Meta"] if "Meta" in attributes else type("Meta", (), {}), bases, canonical_mro
        )

        # The fields of the bases, base by base in the order given - this sets the column order:
        # ancestor fields first. A diamond's value is corrected right after, in place.
        inherited_attributes: dict[str, Any] = {}
        for base in bases:
            ModelFieldCollection.search_for_field_attributes(base, inherited_attributes, name)
        ModelFieldCollection.apply_diamond_field_precedence(canonical_mro, inherited_attributes)
        # The names this class's own body declares, taken before `attributes` is rebound - an own
        # override, as opposed to a name only inherited.
        declared_attribute_names = frozenset(attributes)
        if inherited_attributes:
            # Ensure that the inherited fields are before the defined ones.
            attributes = {**inherited_attributes, **attributes}
        is_abstract = getattr(meta_class, "abstract", False)
        pk_without_overlaps = any(
            isinstance(value, CompositePrimaryKey) and value.without_overlaps for value in attributes.values()
        )
        if has_model_parent:
            attributes, primary_key_attribute = PrimaryKeyDeclaration.parse_custom_pk(
                attributes,
                primary_key_attribute,
                name,
                is_abstract,
                PrimaryKeyDeclaration.declares_no_primary_key(meta_class, name),
            )
        attributes, generic_fields = GenericForeignKeys.expand_declarations(attributes, name, is_abstract)
        if not attributes.get("__migration_state__"):
            # A model rendered from the migrations' state has the blind indexes the state holds - each
            # an AddField of its own, after the field it indexes.
            attributes = BlindIndexes.expand_declarations(attributes, name)
        fields_map = ModelFieldCollection.get_declared_fields(attributes)
        if has_model_parent:
            ModelFieldCollection.check_field_name_conflicts(fields_map, name)

        # The fields this class's own body declares or overrides, plus a synthesized `id` - a
        # subclass tells them from fields only passed through.
        declared_field_names = frozenset(
            key for key in fields_map if key in declared_attribute_names or key not in inherited_attributes
        )

        # Clean the class attributes
        for slot in fields_map:
            attributes.pop(slot, None)
        declared_manager = inspect.getattr_static(meta_class, ModelOption.MANAGER, None)
        manager = declared_manager.copy_unbound() if declared_manager is not None else Manager()
        attributes["objects"] = manager = ModelManagers.get_default_manager(
            attributes, "objects" in declared_attribute_names, meta_class, manager
        )
        new_class = super().__new__(cls, name, bases, attributes)
        table_description = cls._bind_to_class(
            new_class, fields_map, attributes, getattr(meta_class, ModelOption.TABLE_DESCRIPTION, "")
        )
        new_class._declaration = ModelDeclaration.capture(  # type: ignore[attr-defined]
            new_class,  # type: ignore[arg-type]
            meta_class,
            fields_map,
            generic_fields,
            primary_key_attribute,
            declared_field_names,
            pk_without_overlaps and isinstance(primary_key_attribute, tuple),
            manager,
            table_description,
        )
        return new_class

    @staticmethod
    def _bind_to_class(
        new_class: ModelMeta, fields_map: dict[str, Field[Any]], attributes: dict[str, Any], table_description: str
    ) -> str:
        """Apply class comments and declaration hooks, without preparing runtime metadata."""
        if fields_map and not attributes.get("_no_comments"):
            for field_name, comment in FieldComments.get_comments(new_class).items():  # type: ignore[arg-type]
                commented_field = fields_map.get(field_name)
                if commented_field is not None:
                    commented_field.docstring = comment
                    if commented_field.description is None:
                        commented_field.description = comment.split("\n")[0]
        if new_class.__doc__ and not table_description:
            table_description = inspect.cleandoc(new_class.__doc__).split("\n")[0]
        for field_name, field in fields_map.items():
            field.bind_to_model(new_class, field_name)  # type: ignore[arg-type]
            if isinstance(field, GeneratedField):
                field.output_field.bind_to_model(new_class, field_name)  # type: ignore[arg-type]
        for value in attributes.values():
            if isinstance(value, Manager):
                value._model = new_class  # type: ignore[assignment]
        return table_description or ""

    @staticmethod
    def _linearize_mro(bases: tuple[type, ...]) -> tuple[type, ...]:
        """C3-linearizes ``bases`` as a ``class`` statement would. Computed by hand: a throwaway probe
        class would register itself as a subclass of every base.

        Args:
            bases: The base classes, in the order given to the ``class`` statement.

        Returns:
            Every class in ``bases`` and their ancestors, each once, nearest first.

        Raises:
            TypeError: ``bases`` has no consistent linearization.
        """
        sequences: list[list[type]] = [list(base.__mro__) for base in bases] + [list(bases)]
        result: list[type] = []
        while True:
            sequences = [sequence for sequence in sequences if sequence]
            if not sequences:
                return tuple(result)
            candidate: type | None = None
            for sequence in sequences:
                head = sequence[0]
                if not any(head in other[1:] for other in sequences):
                    candidate = head
                    break
            if candidate is None:
                raise TypeError(f"Cannot create a consistent method resolution order for bases {bases!r}")
            result.append(candidate)
            for sequence in sequences:
                if sequence and sequence[0] is candidate:
                    del sequence[0]

    def __getitem__(cls: type[TModel], key: Any) -> Coroutine[Any, Any, TModel]:  # type: ignore[misc]
        from hare.core.hare_context import HareContext

        context = HareContext.require_current()
        return ModelMeta._get_by_primary_key(context.get_model_binding(cls), context, key)

    #: A model class isn't a sequence of its objects - without this, ``iter(Model)`` would read
    #: ``Model[0]``, ``Model[1]``, ... through ``__getitem__`` without end.
    __iter__ = None

    @staticmethod
    async def _get_by_primary_key(binding: ModelBinding, context: HareContext, key: Any) -> TModel:
        """The object ``Model[key]`` reads.

        Raises:
            DoesNotExist: No object has that primary key.
        """

        model = binding._model
        shown_key = binding.get_shown_primary_key(key)
        message = f"{model.__name__} has no object with {binding.primary_key_attribute}={shown_key}"
        cause: BaseException | None = None
        try:
            return await binding.manager.create_queryset(binding=binding, context=context).get(pk=key)
        except DoesNotExist:
            pass
        except (ValueError, ValidationError) as error:
            if not any(field.sensitive for field in binding.pk_fields):
                cause = error
        raise DoesNotExist(model, message) from cause
