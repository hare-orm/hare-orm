from __future__ import annotations

from collections.abc import Iterable, Sequence

from hare.ddl.schema_objects.enum_type import EnumType
from hare.dialects.base.schema.schema_editor_part import SchemaEditorPart
from hare.models.model_binding import ModelBinding


class EnumTypes(SchemaEditorPart):
    """The enum types the fields of models read: found, created, dropped and given new labels."""

    __slots__ = ()

    @staticmethod
    def get_required_enum_types(models: Iterable[ModelBinding]) -> list[EnumType]:
        """The ``ENUM`` types the columns of the models are of, each once, in first-use order.

        Args:
            models: The models.

        Returns:
            The types.

        Raises:
            ConfigurationError: Two fields name one type with different labels.
        """
        types = (field.requires_enum_type for model in models for field in model.fields_map.values())
        return list(EnumType.collect(((None, types),)).values())

    def get_enum_types_create_sqls(self, enum_types: Iterable[EnumType], safe: bool = False) -> list[str]:
        """Create required types in order, accounting for earlier declarations of each name."""
        previous: dict[str, list[EnumType]] = {}
        statements = []
        for enum_type in enum_types:
            same_name = previous.setdefault(enum_type.name, [])
            statements.append(self.get_enum_type_create_sql(enum_type, safe, shared_with=same_name))
            same_name.append(enum_type)
        return statements

    def get_enum_type_create_sql(
        self, enum_type: EnumType, safe: bool = False, *, shared_with: Sequence[EnumType] = ()
    ) -> str:
        """The DDL creating an ``ENUM`` type.

        Args:
            enum_type: The type.
            safe: Create it only when it doesn't exist yet.
            shared_with: Tracked declarations that may name the same physical type.

        Raises:
            UnSupportedError: The dialect has no ``ENUM`` types.
        """
        raise self.get_unsupported_error("ENUM types")

    async def create_enum_type(self, enum_type: EnumType, *, shared_with: Sequence[EnumType] = ()) -> None:
        """Creates an ``ENUM`` type.

        Args:
            enum_type: The type.
            shared_with: Tracked declarations that may already supply the type.

        Raises:
            UnSupportedError: The dialect has no ``ENUM`` types.
        """
        await self.editor.run_sql(self.get_enum_type_create_sql(enum_type, shared_with=shared_with))

    async def drop_enum_type(self, enum_type: EnumType, *, shared_with: Sequence[EnumType] = ()) -> None:
        """Drops an ``ENUM`` type - no column may be of it any more.

        Args:
            enum_type: The type.
            shared_with: Tracked declarations that may still require the type.

        Raises:
            UnSupportedError: The dialect has no ``ENUM`` types.
        """
        raise self.get_unsupported_error("ENUM types")

    async def alter_enum_type(
        self, old_type: EnumType, new_type: EnumType, *, shared_with: Sequence[EnumType] = ()
    ) -> None:
        """Gives an ``ENUM`` type new labels - the columns of the type keep their values, which
        have to be among the new labels.

        Args:
            old_type: The type as it is.
            new_type: The type as it becomes - of the same name.
            shared_with: Other tracked declarations of the type name before the change.

        Raises:
            UnSupportedError: The dialect has no ``ENUM`` types.
        """
        raise self.get_unsupported_error("ENUM types")
