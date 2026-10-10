from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast, get_args

from pydantic import field_validator

from hare import (
    BackwardForeignKeyRelation,
    BackwardOneToOneRelation,
    ForeignKeyFieldInstance,
    OneToOneFieldInstance,
)
from hare.contrib.pydantic.creation.data_field_schemas import DataFieldSchemas
from hare.contrib.pydantic.creation.generic_foreign_key_schemas import GenericForeignKeySchemas
from hare.contrib.pydantic.models.pydantic_model import PydanticModel
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.query.scopes.row_scopes import RowScopes

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.contrib.pydantic.creation.pydantic_model_creator import PydanticModelCreator, StackEntry
    from hare.models import Model


class RelationFieldSchemas:
    """The fields a model's relations get in the pydantic model: a nested model of the related one, or
    its key - for a relation to one row, a list for a relation to many, a discriminated union for a
    generic foreign key."""

    @staticmethod
    def add_relation_id_names(model: type[Model], names: tuple[str, ...]) -> tuple[str, ...]:
        """Adds the shadow id field names of every forward FK/O2O named in ``names``.

        Args:
            model: The Hare model.
            names: Field names, possibly naming relations.

        Returns:
            ``names`` followed by the shadow id field names of the relations it names.
        """
        relation_id_names: list[str] = []
        for name in names:
            field = model._meta.fields_map.get(name)
            if isinstance(field, ForeignKeyFieldInstance):
                relation_id_names.extend(field.source_fields)
        return tuple(names) + tuple(relation_id_names)

    @staticmethod
    def add_generic_foreign_keys(creator: PydanticModelCreator) -> None:
        """Puts each generic foreign key of the model in the schema in place of its branches: on
        output the union of its targets' schemas told apart by ``type``, on input (and with
        ``relations_as_ids``) ``{"type": "<branch>", <the target's key fields>}``. A schema leaving
        out every branch, or naming the field in ``exclude``, keeps the branches as they are.

        Args:
            creator: The creator of the pydantic model.
        """
        fields_map = creator._model_class._meta.fields_map
        for name, generic_field in creator._model_class._meta.generic_foreign_key_fields.items():
            if not generic_field.branch_names or name in creator.meta.exclude:
                continue
            branch_keys = [
                key
                for branch_name in generic_field.branch_names
                for key in (branch_name, *fields_map[branch_name].source_fields)  # type: ignore[attr-defined]
            ]
            present_keys = [key for key in branch_keys if key in creator._properties]
            if not present_keys:
                continue
            submodels = {
                branch_name: RelationFieldSchemas.get_property_model(creator, branch_name)
                for branch_name in generic_field.branch_names
                if branch_name in creator._properties
            }
            as_keys = (
                creator._exclude_read_only
                or creator._relations_as_ids
                or len(submodels) != len(generic_field.branch_names)
                or any(submodel is None for submodel in submodels.values())
            )
            if as_keys:
                annotation = GenericForeignKeySchemas.get_key_union(generic_field, creator._fqname)
            else:
                annotation = GenericForeignKeySchemas.get_object_union(
                    generic_field, cast("dict[str, type[PydanticModel]]", submodels), creator._fqname
                )
            optional = name in creator._optional or generic_field.default is not None
            if generic_field.null or optional:
                annotation = annotation | None
            # The field takes the place of its first branch property; the others are dropped.
            first_key = present_keys[0]
            field_info = GenericForeignKeySchemas.get_field_info(generic_field, optional)
            creator._properties = {
                (name if key == first_key else key): ((annotation, field_info) if key == first_key else value)
                for key, value in creator._properties.items()
                if key not in branch_keys or key == first_key
            }
            creator._generic_field_validators[f"_hare_generic_{name}"] = field_validator(name, mode="before")(
                GenericForeignKeySchemas.get_validator(generic_field, as_keys=as_keys)
            )

    @staticmethod
    def get_property_model(creator: PydanticModelCreator, field_name: str) -> type[PydanticModel] | None:
        """The schema a relation property of the schema holds - None for anything else.

        Args:
            creator: The creator of the pydantic model.
            field_name: The property name.

        Returns:
            The schema.
        """
        value = creator._properties[field_name]
        annotation = value[0] if isinstance(value, tuple) else value
        for candidate in (annotation, *get_args(annotation)):
            if isinstance(candidate, type) and issubclass(candidate, PydanticModel):
                return candidate
        return None

    @staticmethod
    def can_hide_target(
        field: ForeignKeyFieldInstance[Model] | OneToOneFieldInstance[Model] | BackwardOneToOneRelation[Model],
    ) -> bool:
        """Whether reading forward relation ``field`` can give ``None`` for a set key - its target
        model's default scope (soft delete, tenant, a custom ``Meta.manager`` filter) may hide the
        row.

        Args:
            field: The relation field.
        """
        if not isinstance(field, (ForeignKeyFieldInstance, OneToOneFieldInstance)):
            return False
        related_model = field.related_model
        return bool(RowScopes.of(related_model))

    @staticmethod
    def process_single_field_relation(
        creator: PydanticModelCreator,
        field_name: str,
        field: ForeignKeyFieldInstance[Model] | OneToOneFieldInstance[Model] | BackwardOneToOneRelation[Model],
        json_schema_extra: dict[str, Any],
    ) -> type[PydanticModel] | None:
        python_type = field.get_python_type()
        model: type[PydanticModel] | None = RelationFieldSchemas.get_submodel(creator, python_type, field_name)
        if model:
            creator._relational_fields_index.append((field_name, model.__name__))
            if field.null:
                json_schema_extra["nullable"] = True
            if (
                field.null
                or field.default is not None
                or field_name in creator._optional
                or DataFieldSchemas.is_tenant_field(creator, field_name, field)
                or RelationFieldSchemas.can_hide_target(field)
            ):
                return cast("type[PydanticModel] | None", model | None)
            return model
        return None

    @staticmethod
    def process_many_field_relation(
        creator: PydanticModelCreator,
        field_name: str,
        field: BackwardForeignKeyRelation[Model] | ManyToManyFieldInstance[Model],
    ) -> type[list[type[PydanticModel]]] | None:
        python_type = field.related_model
        model = RelationFieldSchemas.get_submodel(creator, python_type, field_name)
        if model:
            creator._relational_fields_index.append((field_name, model.__name__))
            return list[model]  # type: ignore[valid-type]
        return None

    @staticmethod
    def create_submodel(
        cls: type[Model],
        *,
        stack: tuple[StackEntry, ...],
        exclude: tuple[str, ...] = (),
        include: tuple[str, ...] = (),
        computed: tuple[str, ...] = (),
        name: str | None = None,
        allow_cycles: bool = False,
        sort_alphabetically: bool | None = None,
        max_recursion: int,
        exclude_sensitive: bool = False,
        relations_as_ids: bool = False,
    ) -> type[PydanticModel] | None:
        """Create a Pydantic submodel with recursion protection against cyclic references."""
        # Local import: the creator module imports this module.
        from hare.contrib.pydantic.creation.pydantic_model_creator import PydanticModelCreator

        if not allow_cycles and cls in (stack_entry[0] for stack_entry in stack[:-1]):
            return None

        # Level 0 is the root model itself, so the Nth stack entry (0-indexed) is level N -
        # reject once level reaches the configured max_recursion, so max_recursion=1 permits
        # exactly one level of nested submodels.
        for level, (_, _, parent_max_recursion) in enumerate(stack):
            if level >= parent_max_recursion:
                return None
        creator = PydanticModelCreator(
            cls,
            exclude=exclude,
            include=include,
            computed=computed,
            name=name,
            _stack=stack,
            allow_cycles=allow_cycles,
            sort_alphabetically=sort_alphabetically,
            _max_recursion=max_recursion,
            _as_submodel=True,
            exclude_sensitive=exclude_sensitive,
            relations_as_ids=relations_as_ids,
        )
        return creator.create_pydantic_model()

    @staticmethod
    def get_submodel(
        creator: PydanticModelCreator, related_model: type[Model] | None, field_name: str
    ) -> type[PydanticModel] | None:
        """Get Pydantic model for the submodel

        Args:
            creator: The creator of the pydantic model.
            related_model: The model the relation points at; None leaves the field out.
            field_name: The relation field's name.
        """

        if related_model:
            new_stack = creator._stack + ((creator._model_class, field_name, creator.meta.max_recursion),)

            prefix_length = len(field_name) + 1

            def get_fields_to_carry_on(field_tuple: tuple[str, ...]) -> tuple[str, ...]:
                return tuple(
                    str(field_path[prefix_length:])
                    for field_path in field_tuple
                    if field_path.startswith(field_name + ".")
                )

            submodel = RelationFieldSchemas.create_submodel(
                related_model,
                exclude=get_fields_to_carry_on(creator.meta.exclude),
                include=get_fields_to_carry_on(creator.meta.include),
                computed=get_fields_to_carry_on(creator.meta.computed),
                stack=new_stack,
                allow_cycles=creator.meta.allow_cycles,
                sort_alphabetically=creator.meta.sort_alphabetically,
                max_recursion=creator.meta.max_recursion,
                exclude_sensitive=creator._exclude_sensitive,
                relations_as_ids=creator._relations_as_ids,
            )
        else:
            submodel = None

        if submodel is None:
            creator.meta.exclude += (field_name,)

        return submodel
