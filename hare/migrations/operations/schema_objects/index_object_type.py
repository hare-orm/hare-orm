from __future__ import annotations

from collections.abc import Callable
from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar

from hare.ddl.indexes.index import Index
from hare.exceptions import ConfigurationError
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations.schema_objects.schema_object_type import SchemaObjectType
from hare.models.enums import ModelOption

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.editor import BaseSchemaEditor
    from hare.migrations.state.project.model_state import ModelState
    from hare.models import Model


class IndexObjectType(SchemaObjectType):
    """The indexes of ``Meta.indexes`` - an ``Index``, or a tuple of field names standing for a
    plain one. An unnamed index is found by the name it was created under, or by its fields."""

    option: ClassVar[ModelOption] = ModelOption.INDEXES
    noun: ClassVar[str] = "index"

    @classmethod
    def find(
        cls,
        model_state: ModelState,
        get_model: Callable[[], type[Model]],
        *,
        name: str | None = None,
        fields: list[str] | None = None,
    ) -> Any:
        indexes = cls.get_objects(model_state)
        if name:
            for index in indexes:
                if isinstance(index, Index) and index.name == name:
                    return index
            for index in indexes:
                if isinstance(index, Index) and not index.name:
                    model = get_model()
                    index.get_expressions(model)
                    if index.get_generated_expression_name(model._meta.db_table) == name:
                        return index
        if fields:
            for index in indexes:
                if isinstance(index, Index) and list(index.field_names) == list(fields):
                    return index
                if not isinstance(index, Index) and list(index) == list(fields):
                    return index
        raise IncompatibleStateError(f"Index {name or fields} is not present on {model_state.name}")

    @classmethod
    def find_for_rename(
        cls,
        model_state: ModelState,
        get_model: Callable[[], type[Model]],
        *,
        name: str | None,
        fields: list[str] | None,
    ) -> Any:
        # An index renamed by its fields may be a field tuple, or not in the state at all - it is
        # renamed as the plain index over them.
        if fields:
            try:
                return cls.find(model_state, get_model, fields=fields)
            except IncompatibleStateError:
                return Index(fields=tuple(fields), name=name)
        return cls.find(model_state, get_model, name=name)

    @staticmethod
    def raise_if_concurrently_in_transaction(state_editor: BaseSchemaEditor) -> None:
        """Refuses building or dropping an index without blocking writes inside the migration's
        transaction, where the dialect has such a build - it can't run in one.

        Args:
            state_editor: The migration's schema editor.

        Raises:
            ConfigurationError: The migration is atomic.
        """
        if (
            state_editor.client.dialect.supports_concurrent_indexes
            and state_editor.atomic_migration
            and not state_editor.collect_sql
        ):
            raise ConfigurationError(
                "An index can't be built or dropped concurrently inside a transaction - set atomic = False on "
                "the migration."
            )

    @classmethod
    def as_schema_object(cls, entry: Any) -> Index:
        return entry if isinstance(entry, Index) else Index(fields=tuple(entry))

    @classmethod
    def renamed(cls, schema_object: Any, new_name: str) -> Index:
        if not isinstance(schema_object, Index):
            return Index(fields=tuple(schema_object), name=new_name)
        renamed_index = copy(schema_object)
        renamed_index.name = new_name
        return renamed_index

    @classmethod
    async def add(cls, editor: BaseSchemaEditor, model: type[Model], schema_object: Any, **options: Any) -> None:
        await editor.add_index(
            model, cls.as_schema_object(schema_object), concurrently=options.get("concurrently", False)
        )

    @classmethod
    async def remove(cls, editor: BaseSchemaEditor, model: type[Model], schema_object: Any, **options: Any) -> None:
        await editor.remove_index(
            model, cls.as_schema_object(schema_object), concurrently=options.get("concurrently", False)
        )

    @classmethod
    async def rename(cls, editor: BaseSchemaEditor, model: type[Model], old_object: Any, new_object: Any) -> None:
        await editor.rename_index(model, cls.as_schema_object(old_object), cls.as_schema_object(new_object))
