from __future__ import annotations

from collections.abc import Iterable

from hare.ddl.schema_objects.enum_type import EnumType
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.exceptions import ConfigurationError
from hare.models import Model


class EnumTypes(SchemaEditorPart):
    """The enum types the fields of models read: found, created, dropped and given new labels."""

    __slots__ = ()

    @staticmethod
    def get_required_enum_types(models: Iterable[type[Model]]) -> list[EnumType]:
        """The ``ENUM`` types the columns of the models are of, each once, in first-use order.

        Args:
            models: The models.

        Returns:
            The types.

        Raises:
            ConfigurationError: Two fields name one type with different labels.
        """
        enum_types: dict[str, EnumType] = {}
        for model in models:
            for field in model._meta.fields_map.values():
                enum_type = field.requires_enum_type
                if enum_type is None:
                    continue
                known = enum_types.setdefault(enum_type.name, enum_type)
                if known.labels != enum_type.labels:
                    raise ConfigurationError(
                        f"Two fields name the ENUM type {enum_type.name!r} with different labels: "
                        f"{list(known.labels)} and {list(enum_type.labels)}"
                    )
        return list(enum_types.values())

    def get_enum_type_create_sql(self, enum_type: EnumType, safe: bool = False) -> str:
        """The DDL creating an ``ENUM`` type.

        Args:
            enum_type: The type.
            safe: Create it only when it doesn't exist yet.

        Raises:
            UnSupportedError: The dialect has no ``ENUM`` types.
        """
        raise self.get_unsupported_error("ENUM types")

    async def create_enum_type(self, enum_type: EnumType) -> None:
        """Creates an ``ENUM`` type.

        Args:
            enum_type: The type.

        Raises:
            UnSupportedError: The dialect has no ``ENUM`` types.
        """
        await self.editor.run_sql(self.get_enum_type_create_sql(enum_type))

    async def drop_enum_type(self, enum_type: EnumType) -> None:
        """Drops an ``ENUM`` type - no column may be of it any more.

        Args:
            enum_type: The type.

        Raises:
            UnSupportedError: The dialect has no ``ENUM`` types.
        """
        raise self.get_unsupported_error("ENUM types")

    async def alter_enum_type(self, old_type: EnumType, new_type: EnumType) -> None:
        """Gives an ``ENUM`` type new labels - the columns of the type keep their values, which
        have to be among the new labels.

        Args:
            old_type: The type as it is.
            new_type: The type as it becomes - of the same name.

        Raises:
            UnSupportedError: The dialect has no ``ENUM`` types.
        """
        raise self.get_unsupported_error("ENUM types")
