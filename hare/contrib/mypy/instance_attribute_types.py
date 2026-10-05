from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mypy.typeops import make_simplified_union
from mypy.types import AnyType, Instance, NoneType, Type, get_proper_type

from hare.contrib.mypy.constants import DICT_FULLNAME, PRIMARY_KEY_ATTRIBUTE_NAME, STR_FULLNAME
from hare.contrib.mypy.mypy_types import MypyTypes
from hare.exceptions import HareError
from hare.fields.relations.fields.generic_foreign_key_field_instance import GenericForeignKeyFieldInstance
from hare.fields.relations.fields.relational_field import RelationalField

if TYPE_CHECKING:  # pragma: nocoverage
    from mypy.plugin import AttributeContext, Plugin

    from hare.contrib.mypy.model_registry import ModelRegistry
    from hare.fields.field import Field
    from hare.models import Model


class InstanceAttributeTypes:
    """The types of the attributes of a model instance, read from its fields: a field's value - None
    included when the field is nullable -, a relation's key column (``author_id``), the object of a
    generic foreign key, and ``pk`` - the key's value, a tuple for a composite key, None also taken on
    assignment.

    Args:
        plugin: The plugin.
        model_registry: The bound models.
    """

    def __init__(self, plugin: Plugin, model_registry: ModelRegistry) -> None:
        self.plugin = plugin
        self.model_registry = model_registry

    def get_attribute_type(self, attribute_context: AttributeContext, name: str) -> Type:
        """The type of an attribute of an instance.

        Args:
            attribute_context: The attribute access.
            name: The attribute.

        Returns:
            The type - mypy's own for an instance of no bound model or an attribute that isn't a field.
        """
        receiver = get_proper_type(attribute_context.type)
        model = None if not isinstance(receiver, Instance) else self.model_registry.get_model(receiver.type.fullname)
        if model is None:
            return attribute_context.default_attr_type
        mypy_types = MypyTypes(self.plugin, attribute_context.api, self.model_registry)
        if name == PRIMARY_KEY_ATTRIBUTE_NAME:
            return self.get_primary_key_type(attribute_context, mypy_types, model)
        generic_field = model._meta.generic_foreign_key_fields.get(name)
        if generic_field is not None:
            object_type = self.get_generic_object_type(attribute_context.default_attr_type, mypy_types, generic_field)
            if not attribute_context.is_lvalue:
                return object_type
            # An assignment takes a branch's row as a dict too, and None - no branch set.
            row_type = attribute_context.api.named_generic_type(
                DICT_FULLNAME, [attribute_context.api.named_generic_type(STR_FULLNAME, []), mypy_types.any()]
            )
            return make_simplified_union([object_type, row_type, NoneType()])
        field = model._meta.fields_map.get(name)
        if field is None or isinstance(field, RelationalField):
            return attribute_context.default_attr_type
        return self.get_field_value_type(attribute_context.default_attr_type, mypy_types, model, field)

    @staticmethod
    def get_generic_object_type(
        declared_type: Type, mypy_types: MypyTypes, generic_field: GenericForeignKeyFieldInstance[Any]
    ) -> Type:
        """The type of a generic foreign key's object - an instance of one of its models, None too
        when no branch need be set.

        Args:
            declared_type: The type mypy reads from the class.
            mypy_types: The types of the access.
            generic_field: The generic foreign key.

        Returns:
            The type - the declared one when mypy doesn't know one of the models.
        """
        branch_types: list[Type] = []
        for branch_model in generic_field.branch_by_model:
            branch_type = mypy_types.get_model_type(branch_model)
            if branch_type is None:
                return declared_type
            branch_types.append(branch_type)
        if generic_field.null:
            branch_types.append(NoneType())
        return make_simplified_union(branch_types)

    @staticmethod
    def get_field_value_type(
        declared_type: Type, mypy_types: MypyTypes, model: type[Model], field: Field[Any]
    ) -> Type:
        """The type of a field's value on an instance - the type the class declares, None added or
        dropped as the field is nullable or not; the type ``values()`` gives where the class declares
        ``Any`` (a container field, a relation's key column).

        Args:
            declared_type: The type mypy reads from the class.
            mypy_types: The types of the access.
            model: The model.
            field: The field.

        Returns:
            The type.
        """
        if not isinstance(get_proper_type(declared_type), AnyType):
            value_type = MypyTypes.without_none(declared_type)
            return make_simplified_union([value_type, NoneType()]) if field.null else value_type
        try:
            return mypy_types.get_selected_value_type(model._meta.get_lookup_info(field.model_field_name))
        except HareError:
            return declared_type

    @staticmethod
    def get_primary_key_type(attribute_context: AttributeContext, mypy_types: MypyTypes, model: type[Model]) -> Type:
        """The type of ``pk`` of an instance.

        Args:
            attribute_context: The attribute access.
            mypy_types: The types of the access.
            model: The model.

        Returns:
            The type - mypy's own for a model without a key.
        """
        if not model._meta.primary_key_attribute:
            return attribute_context.default_attr_type
        try:
            key_type = mypy_types.get_selected_value_type(model._meta.get_lookup_info(PRIMARY_KEY_ATTRIBUTE_NAME))
        except HareError:
            return attribute_context.default_attr_type
        return make_simplified_union([key_type, NoneType()]) if attribute_context.is_lvalue else key_type
