"""The parameters of a request query as a framework reads them for a handler's signature."""

from __future__ import annotations

from functools import reduce
from operator import or_
from types import UnionType
from typing import TYPE_CHECKING, Annotated, Any, Union, get_args, get_origin

from hare.contrib.frameworks.constants import CONTAINER_ORIGINS
from hare.contrib.request_query.options.in_path import InPath
from hare.contrib.request_query.types.text_parameter import TextParameter

if TYPE_CHECKING:  # pragma: nocoverage
    from pydantic.fields import FieldInfo


class ParameterAnnotation:
    """Presents a request query's parameter to a framework: a type read from one text value
    (``KeyColumns``, ``CommaSeparated``) is text the query parses itself."""

    @staticmethod
    def is_text(field_info: FieldInfo) -> bool:
        """Whether a parameter is read from one text value as a whole.

        Args:
            field_info: The parameter's pydantic field.

        Returns:
            True when its top-level metadata marks a text type.
        """
        return any(isinstance(item, TextParameter) for item in field_info.metadata)

    @staticmethod
    def is_in_path(field_info: FieldInfo) -> bool:
        """Whether a parameter is read from the route's path.

        Args:
            field_info: The parameter's pydantic field.

        Returns:
            True when it is marked ``InPath()``.
        """
        return any(isinstance(item, InPath) for item in field_info.metadata)

    @classmethod
    def get_type(cls, field_info: FieldInfo) -> Any:
        """The type a framework reads a parameter as.

        Args:
            field_info: The parameter's pydantic field.

        Returns:
            ``str`` for a type read from one text value, else its annotation with each such type
            inside replaced by ``str``.
        """
        return str if cls.is_text(field_info) else cls.present(field_info.annotation)

    @classmethod
    def present(cls, annotation: Any) -> Any:
        """Replaces each type read from one text value inside an annotation with ``str`` - inside
        unions and containers too (``list[KeyColumns[int, int]] | None`` becomes
        ``list[str] | None``).

        Args:
            annotation: An annotation.

        Returns:
            The annotation a framework reads the parameter with.
        """
        origin = get_origin(annotation)
        arguments = get_args(annotation)
        if origin is Annotated:
            if any(isinstance(item, TextParameter) for item in arguments[1:]):
                return str
            return Annotated[(cls.present(arguments[0]), *arguments[1:])]
        if origin in {Union, UnionType}:
            return reduce(or_, (cls.present(argument) for argument in arguments))
        if origin in CONTAINER_ORIGINS and arguments:
            return origin[tuple(argument if argument is Ellipsis else cls.present(argument) for argument in arguments)]
        return annotation
