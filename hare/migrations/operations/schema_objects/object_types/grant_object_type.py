from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations.schema_objects.object_types.schema_object_type import SchemaObjectType
from hare.models.enums import ModelOption

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.security.grant import Grant
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.migrations.state.model_state import ModelState
    from hare.models import Model


class GrantObjectType(SchemaObjectType):
    """The grants of ``Meta.grants`` - unnamed, each known by everything it grants; never renamed
    or changed in place, only granted and revoked."""

    option: ClassVar[ModelOption] = ModelOption.GRANTS
    noun: ClassVar[str] = "grant"
    plural_noun: ClassVar[str] = "grants"
    feature: ClassVar[str | None] = "supports_grants"

    @classmethod
    def find_grant(cls, model_state: ModelState, grant: Grant) -> Grant:
        """The grant of a model's state equal to ``grant``, as the state stores it.

        Args:
            model_state: The model's state.
            grant: The grant.

        Returns:
            The grant.

        Raises:
            IncompatibleStateError: The state has no such grant.
        """
        for schema_object in cls.get_objects(model_state):
            if schema_object == grant:
                return schema_object
        raise IncompatibleStateError(f"Grant of {grant.describe()} is not present on {model_state.name}")

    @classmethod
    def find(
        cls,
        model_state: ModelState,
        get_model: Callable[[], type[Model]],
        *,
        name: str | None = None,
        fields: list[str] | None = None,
    ) -> Any:
        raise IncompatibleStateError(f"A grant has no name - it is found by what it grants on {model_state.name}")

    @classmethod
    async def add(cls, editor: BaseSchemaEditor, model: type[Model], schema_object: Any, **options: Any) -> None:
        cls.raise_if_unsupported(editor)
        await editor.grants.add_grant(model, schema_object)

    @classmethod
    async def remove(cls, editor: BaseSchemaEditor, model: type[Model], schema_object: Any, **options: Any) -> None:
        cls.raise_if_unsupported(editor)
        await editor.grants.remove_grant(model, schema_object)
