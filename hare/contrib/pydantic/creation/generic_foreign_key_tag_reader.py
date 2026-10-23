from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from hare.fields.constants import GENERIC_FOREIGN_KEY_TYPE_FIELD

if TYPE_CHECKING:
    from hare.fields.relations.fields.generic_foreign_key_field_instance import GenericForeignKeyFieldInstance


class GenericForeignKeyTagReader:
    """The discriminator of a generic foreign key's union: reads the branch of a value - a dict's or a
    schema's ``type``, a model instance's model. Its text names the field, not the object, so the
    schema keeps one hash however often it is built.

    Args:
        generic_field: The field.
    """

    __slots__ = ("generic_field", "__name__")

    def __init__(self, generic_field: GenericForeignKeyFieldInstance[Any]) -> None:
        self.generic_field = generic_field
        #: What pydantic names the discriminator by.
        self.__name__ = f"read_{generic_field.model_field_name}_type"

    def __call__(self, value: Any) -> str | None:
        if isinstance(value, dict):
            return cast("str | None", value.get(GENERIC_FOREIGN_KEY_TYPE_FIELD))
        if hasattr(value, "_meta"):
            return self.generic_field.branch_by_model.get(type(value))
        return getattr(value, GENERIC_FOREIGN_KEY_TYPE_FIELD, None)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.generic_field.model.__name__}.{self.generic_field.model_field_name})"
