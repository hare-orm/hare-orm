from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, TypeVar

from hare.exceptions import DoesNotExist, ValidationError
from hare.fields.composite_primary_key import CompositePrimaryKey
from hare.fields.field import Field
from hare.fields.generated_field import GeneratedField
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.models.class_building.abstract_meta_inheritance import AbstractMetaInheritance
from hare.models.class_building.blind_indexes import BlindIndexes
from hare.models.class_building.field_comments import FieldComments
from hare.models.class_building.generic_foreign_keys import GenericForeignKeys
from hare.models.class_building.model_field_collection import ModelFieldCollection
from hare.models.class_building.model_managers import ModelManagers
from hare.models.class_building.primary_key_declaration import PrimaryKeyDeclaration
from hare.models.meta_info import MetaInfo
from hare.query.managers.manager import Manager
from hare.query.queryset import QuerySetSingle

if TYPE_CHECKING:
    from hare.models.model import Model

TModel = TypeVar("TModel", bound="Model")


class ModelMeta(type):
    __slots__ = ()

    def __new__(cls, name: str, bases: tuple[type, ...], attributes: dict[str, Any]) -> ModelMeta:
        fields_db_projection: dict[str, str] = {}
        primary_key_attribute: str | tuple[str, ...] = "id"
        # The C3 order of the ancestors, nearest first.
        canonical_mro = cls._linearize_mro(bases)
        meta_class = AbstractMetaInheritance.get_merged_meta_class(
            attributes.get("Meta", type("Meta", (), {})), bases, canonical_mro
        )

        # The fields of the bases, base by base in the order given - this sets the column order:
        # ancestor fields first. A diamond's value is corrected right after, in place.
        inherited_attributes: dict[str, Any] = {}
        for base in bases:
            ModelFieldCollection.search_for_field_attributes(base, inherited_attributes, name)
        ModelFieldCollection.apply_diamond_field_precedence(canonical_mro, inherited_attributes)
        # The names this class's own body declares, taken before `attributes` is rebound - an own
        # override, as opposed to a name only inherited.
        own_attribute_names = frozenset(attributes)
        if inherited_attributes:
            # Ensure that the inherited fields are before the defined ones.
            attributes = {**inherited_attributes, **attributes}
        is_abstract = getattr(meta_class, "abstract", False)
        pk_without_overlaps = any(
            isinstance(value, CompositePrimaryKey) and value.without_overlaps for value in attributes.values()
        )
        if name != "Model":
            attributes, primary_key_attribute = PrimaryKeyDeclaration.parse_custom_pk(
                attributes,
                primary_key_attribute,
                name,
                is_abstract,
                PrimaryKeyDeclaration.declares_no_primary_key(meta_class, name),
            )
        attributes, generic_fields = GenericForeignKeys.expand_declarations(attributes, name, is_abstract)
        attributes = BlindIndexes.expand_declarations(attributes, name)
        fields_map, foreign_key_fields, many_to_many_fields, one_to_one_fields = ModelFieldCollection.dispatch_fields(
            attributes, fields_db_projection, is_abstract
        )
        if name != "Model":
            ModelFieldCollection.check_field_name_conflicts(fields_map, name)

        # The fields this class's own body declares or overrides, plus a synthesized `id` - a
        # subclass tells them from fields only passed through.
        own_field_names = frozenset(
            key for key in fields_map if key in own_attribute_names or key not in inherited_attributes
        )

        # Clean the class attributes
        for slot in fields_map:
            attributes.pop(slot, None)
        attributes["_meta"] = meta = cls.build_meta(
            meta_class,
            fields_map,
            fields_db_projection,
            foreign_key_fields,
            one_to_one_fields,
            many_to_many_fields,
            primary_key_attribute,
            own_field_names,
        )
        meta.pk_without_overlaps = pk_without_overlaps and isinstance(primary_key_attribute, tuple)
        attributes["objects"] = meta.manager = ModelManagers.get_default_manager(
            name, attributes, "objects" in own_attribute_names, meta_class, meta.manager
        )

        new_class = super().__new__(cls, name, bases, attributes)
        cls._bind_to_class(new_class, meta, attributes)
        GenericForeignKeys.bind(new_class, generic_fields, is_abstract)  # type: ignore[arg-type]
        meta.finalise_fields()
        return new_class

    @staticmethod
    def _bind_to_class(new_class: ModelMeta, meta: MetaInfo, attributes: dict[str, Any]) -> None:
        """Binds the fields, the managers and the meta of a model class just created to it, and
        reads the fields' comments and the table's description from its source.

        Args:
            new_class: The model class.
            meta: The class's meta.
            attributes: The attributes the class was created with.
        """
        fields_map = meta.fields_map
        for field in fields_map.values():
            field.model = new_class  # type: ignore[assignment]
            if isinstance(field, GeneratedField):
                # A GeneratedField's output_field is a field of its own, not in fields_map - bound
                # here.
                field.output_field.model = new_class  # type: ignore[assignment]

        # A class without fields - the base Model, a mixin - has no comment to read.
        if fields_map and not attributes.get("_no_comments"):
            for field_name, comment in FieldComments.get_comments(new_class).items():  # type: ignore[arg-type]
                commented_field = fields_map.get(field_name)
                if commented_field is None:
                    continue
                commented_field.docstring = comment
                if commented_field.description is None:
                    commented_field.description = comment.split("\n")[0]

        if new_class.__doc__ and not meta.table_description:
            meta.table_description = inspect.cleandoc(new_class.__doc__).split("\n")[0]
        for value in attributes.values():
            if isinstance(value, Manager):
                value._model = new_class  # type: ignore[assignment]
        meta._model = new_class  # type: ignore[assignment]
        meta.manager._model = new_class  # type: ignore[assignment]

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

    @staticmethod
    def build_meta(
        meta_class: type[Model.Meta],
        fields_map: dict[str, Field[Any]],
        fields_db_projection: dict[str, str],
        foreign_key_fields: set[str],
        one_to_one_fields: set[str],
        many_to_many_fields: set[str],
        primary_key_attribute: str | tuple[str, ...],
        own_field_names: frozenset[str],
    ) -> MetaInfo:
        meta = MetaInfo(meta_class)
        meta.fields_map = fields_map
        meta.fields_db_projection = fields_db_projection
        meta.foreign_key_fields = foreign_key_fields
        meta.one_to_one_fields = one_to_one_fields
        meta.many_to_many_fields = many_to_many_fields
        meta.primary_key_attribute = primary_key_attribute
        meta.own_field_names = own_field_names
        if isinstance(primary_key_attribute, tuple):
            # A composite PK has no single Field/column representing it - meta.pk/db_pk_column
            # stay unset (their MetaInfo.__init__ defaults). Every code path that needs the
            # composite key works off meta.primary_key_attribute directly instead (the query executor, DDL generation).
            meta.pk_fields = tuple(fields_map[name] for name in primary_key_attribute)
        elif pk_field := fields_map.get(primary_key_attribute):
            meta.pk = pk_field
            if pk_field.source_field:
                meta.db_pk_column = pk_field.source_field
            elif isinstance(pk_field, OneToOneFieldInstance):
                meta.db_pk_column = f"{primary_key_attribute}_id"
            else:
                meta.db_pk_column = primary_key_attribute
        meta._inited = False
        if not fields_map:
            meta.abstract = True
        return meta

    def __getitem__(cls: type[TModel], key: Any) -> QuerySetSingle[TModel]:  # type: ignore[misc]
        return ModelMeta._get_by_primary_key(cls, key)  # type: ignore[return-value]

    @staticmethod
    async def _get_by_primary_key(model: type[TModel], key: Any) -> TModel:
        """The object ``Model[key]`` reads.

        Raises:
            DoesNotExist: No object has that primary key.
        """
        message = f"{model.__name__} has no object with {model._meta.primary_key_attribute}={key}"
        try:
            return await model.objects.get(pk=key)
        except DoesNotExist:
            raise DoesNotExist(model, message) from None
        except (ValueError, ValidationError) as error:
            # The key can't be coerced to the pk field's type: still "no such object" for a
            # subscript, with the real cause chained.
            raise DoesNotExist(model, message) from error
