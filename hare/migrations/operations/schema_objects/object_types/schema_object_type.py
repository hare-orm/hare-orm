from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

from hare.migrations.exceptions import IncompatibleStateError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.migrations.state.model_state import ModelState
    from hare.models import Model
    from hare.models.enums import ModelOption


class SchemaObjectType:
    """A type of named object a model declares in its ``Meta`` and the database keeps beside its
    table - an index, a constraint, a trigger: the ``Meta`` option listing them, how one is found
    in a model's state and how the schema editor creates, drops, renames and changes one. The
    schema-object operations (``AddSchemaObject`` and the rest) work on any type through it."""

    #: The ``Meta`` option listing the objects.
    option: ClassVar[ModelOption]
    #: The type's name in messages - ``index``, ``constraint``, ``trigger``.
    noun: ClassVar[str]
    #: Whether an object of the type may read or change tables other than its model's - a
    #: trigger's SQL can.
    reaches_other_tables: ClassVar[bool] = False
    #: The ``Features`` flag a database has the type with - None for a type every database has.
    feature: ClassVar[str | None] = None
    #: The type's name in the message refusing it - plural.
    plural_noun: ClassVar[str] = ""

    @classmethod
    def raise_if_unsupported(cls, editor: BaseSchemaEditor) -> None:
        """Refuses the type on a database without it, before any SQL is sent.

        Args:
            editor: The schema editor.

        Raises:
            UnSupportedError: The database lacks the type's ``feature``.
        """
        if cls.feature is not None:
            editor.raise_if_unsupported(cls.feature, cls.plural_noun)

    @classmethod
    def get_objects(cls, model_state: ModelState) -> list[Any]:
        """The objects of a model's state, in ``Meta`` order.

        Args:
            model_state: The model's state.

        Returns:
            A copy of the list.
        """
        return model_state.get_option_list(cls.option)

    @classmethod
    def update_objects(cls, model_state: ModelState, *, removed: Any = None, added: Any = None) -> None:
        """Takes an object out of a model's state and/or puts one in, last.

        Args:
            model_state: The model's state.
            removed: The object to take out - the very one the state holds.
            added: The object to put in.
        """
        entries = model_state.get_option_list(cls.option)
        if removed is not None:
            entries = [entry for entry in entries if entry is not removed]
        if added is not None:
            entries.append(added)
        model_state.set_option_list(cls.option, entries)

    @classmethod
    def get_name(cls, schema_object: Any) -> str | None:
        """The name an object is declared with, None for an unnamed one."""
        return getattr(schema_object, "name", None)

    @classmethod
    def find(
        cls,
        model_state: ModelState,
        get_model: Callable[[], type[Model]],
        *,
        name: str | None = None,
        fields: list[str] | None = None,
    ) -> Any:
        """The object of a model's state given by its name, or - for a type that has unnamed
        objects - by its fields, as it is stored there.

        Args:
            model_state: The model's state.
            get_model: Gives the model rendered from it - asked only when needed.
            name: The object's name.
            fields: The fields of an unnamed object.

        Returns:
            The object.

        Raises:
            IncompatibleStateError: No object matches.
        """
        for schema_object in cls.get_objects(model_state):
            if name is not None and cls.get_name(schema_object) == name:
                return schema_object
        raise IncompatibleStateError(f"{cls.noun.capitalize()} {name or fields} is not present on {model_state.name}")

    @classmethod
    def find_for_rename(
        cls,
        model_state: ModelState,
        get_model: Callable[[], type[Model]],
        *,
        name: str | None,
        fields: list[str] | None,
    ) -> Any:
        """The object a rename starts from - ``find()`` by default.

        Args:
            model_state: The model's state.
            get_model: Gives the model rendered from it - asked only when needed.
            name: The object's name.
            fields: The fields of an unnamed object.

        Returns:
            The object.

        Raises:
            IncompatibleStateError: No object matches.
        """
        return cls.find(model_state, get_model, name=name, fields=fields)

    @classmethod
    def get_state_entry(cls, schema_object: Any) -> Any:
        """An object as the migration state keeps it - the object itself by default."""
        return schema_object

    @classmethod
    def as_schema_object(cls, entry: Any) -> Any:
        """A state entry as the object the schema editor takes - the entry itself by default."""
        return entry

    @classmethod
    def renamed(cls, schema_object: Any, new_name: str) -> Any:
        """A copy of the object under another name."""
        raise NotImplementedError

    @classmethod
    async def add(cls, editor: BaseSchemaEditor, model: type[Model], schema_object: Any, **options: Any) -> None:
        """Creates the object in the database.

        Args:
            editor: The schema editor.
            model: The model.
            schema_object: The object.
            options: The type's options of the operation (``concurrently`` for an index).
        """
        raise NotImplementedError

    @classmethod
    async def remove(cls, editor: BaseSchemaEditor, model: type[Model], schema_object: Any, **options: Any) -> None:
        """Drops the object from the database.

        Args:
            editor: The schema editor.
            model: The model.
            schema_object: The object.
            options: The type's options of the operation.
        """
        raise NotImplementedError

    @classmethod
    async def rename(cls, editor: BaseSchemaEditor, model: type[Model], old_object: Any, new_object: Any) -> None:
        """Renames the object in the database.

        Args:
            editor: The schema editor.
            model: The model.
            old_object: The object under its old name.
            new_object: The object under its new name.
        """
        raise NotImplementedError

    @classmethod
    async def alter(cls, editor: BaseSchemaEditor, model: type[Model], old_object: Any, new_object: Any) -> None:
        """Changes the object in the database in place.

        Args:
            editor: The schema editor.
            model: The model.
            old_object: The object as it is.
            new_object: The object as it becomes.
        """
        raise NotImplementedError(f"A {cls.noun} can't be changed in place")
