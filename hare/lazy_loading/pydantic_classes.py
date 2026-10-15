from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any, ClassVar, TypeGuard

if TYPE_CHECKING:  # pragma: nocoverage
    from pydantic import BaseModel, TypeAdapter


class PydanticClasses:
    """pydantic's ``BaseModel`` and ``TypeAdapter``, imported on first use rather than with hare -
    pydantic is an optional dependency and takes tens of milliseconds to import. A value can only be
    a pydantic model or adapter once something imported pydantic, so checking one imports nothing
    before that.
    """

    #: Whether the import was tried.
    loaded: ClassVar[bool] = False
    #: ``pydantic.BaseModel``, None without pydantic installed.
    base_model: ClassVar[type[BaseModel] | None] = None
    #: ``pydantic.TypeAdapter``, None without pydantic installed.
    type_adapter: ClassVar[type[TypeAdapter[Any]] | None] = None

    @classmethod
    def load(cls) -> None:
        """Imports pydantic's classes, once."""
        try:
            from pydantic import BaseModel, TypeAdapter
        except ImportError:
            pass
        else:
            cls.base_model = BaseModel
            cls.type_adapter = TypeAdapter
        cls.loaded = True

    @classmethod
    def get_type_adapter(cls) -> type[TypeAdapter[Any]] | None:
        """``pydantic.TypeAdapter``, imported now when it wasn't.

        Returns:
            The class, None without pydantic installed.
        """
        if not cls.loaded:
            cls.load()
        return cls.type_adapter

    @classmethod
    def is_imported(cls) -> bool:
        """Whether pydantic's classes are at hand - imported here, or by anything else, which they
        are then taken from.

        Returns:
            True when they are.
        """
        if cls.loaded:
            return cls.base_model is not None
        if "pydantic" not in sys.modules:
            return False
        cls.load()
        return cls.base_model is not None

    @classmethod
    def is_model_instance(cls, value: Any) -> TypeGuard[BaseModel]:
        """Whether ``value`` is a pydantic model.

        Args:
            value: The value.

        Returns:
            True when it is.
        """
        return cls.is_imported() and isinstance(value, cls.base_model)  # type: ignore[arg-type]

    @classmethod
    def is_model_class(cls, value: Any) -> TypeGuard[type[BaseModel]]:
        """Whether ``value`` is a pydantic model class.

        Args:
            value: The value.

        Returns:
            True when it is.
        """
        return cls.is_imported() and isinstance(value, type) and issubclass(value, cls.base_model)  # type: ignore[arg-type]

    @classmethod
    def is_type_adapter(cls, value: Any) -> TypeGuard[TypeAdapter[Any]]:
        """Whether ``value`` is a pydantic type adapter.

        Args:
            value: The value.

        Returns:
            True when it is.
        """
        return cls.is_imported() and isinstance(value, cls.type_adapter)  # type: ignore[arg-type]
