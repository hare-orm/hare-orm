"""Checks the annotation of a parameter against the value its filter takes, as the ORM describes it."""

from __future__ import annotations

from enum import Enum
from types import NoneType, UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from hare.contrib.request_query.types.text_parameter import TextParameter
from hare.query.enums import LookupValueShape

#: The generic containers a list-shaped value may be annotated with.
LIST_CONTAINERS = (list, set, frozenset, tuple)


class ValueAnnotation:
    """Reads a parameter's annotation: whether it takes the value a lookup takes."""

    @staticmethod
    def strip(annotation: Any) -> Any:
        """Removes ``Annotated[...]`` wrappers.

        Args:
            annotation: An annotation.

        Returns:
            The annotated type.
        """
        while get_origin(annotation) is Annotated:
            annotation = get_args(annotation)[0]
        return annotation

    @classmethod
    def get_alternatives(cls, annotation: Any) -> list[Any]:
        """The types a value of ``annotation`` may have, without ``None``.

        Args:
            annotation: An annotation - a union is split into its members.

        Returns:
            The non-None members, each without ``Annotated[...]``.
        """
        annotation = cls.strip(annotation)
        if get_origin(annotation) in (Union, UnionType):
            return [cls.strip(member) for member in get_args(annotation) if member is not NoneType]
        return [annotation]

    @classmethod
    def accepts_value(cls, annotation: Any, expected_type: Any) -> bool:
        """Whether one value of ``annotation`` is a value of ``expected_type``.

        Args:
            annotation: The annotation of one value.
            expected_type: The type the lookup takes - a tuple of types for a composite key,
                ``object`` or None for any value.

        Returns:
            True when every value the annotation allows is of the expected type.
        """
        if expected_type is None or expected_type is object:
            return True
        alternatives = cls.get_alternatives(annotation)
        if not alternatives:
            return False
        return all(cls.accepts_single_type(alternative, expected_type) for alternative in alternatives)

    @classmethod
    def accepts_single_type(cls, annotation: Any, expected_type: Any) -> bool:
        """Whether a type that is not a union takes values of ``expected_type``.

        Args:
            annotation: A type, not a union.
            expected_type: The type the lookup takes.

        Returns:
            True for the type itself, a subclass (except ``bool`` for ``int``), an enum whose
            values are of the type, a ``Literal`` of such values, and a tuple matching a
            composite key item by item.
        """
        if isinstance(expected_type, tuple):
            arguments = get_args(annotation)
            return (
                get_origin(annotation) is tuple
                and len(arguments) == len(expected_type)
                and Ellipsis not in arguments
                and all(
                    cls.accepts_value(argument, item) for argument, item in zip(arguments, expected_type, strict=True)
                )
            )
        if get_origin(annotation) is Literal:
            return all(cls.is_instance(value, expected_type) for value in get_args(annotation))
        if not isinstance(annotation, type) or not isinstance(expected_type, type):
            return False
        if annotation is bool and expected_type is not bool:
            return False
        if issubclass(annotation, expected_type):
            return True
        if issubclass(annotation, Enum):
            return all(cls.is_instance(member.value, expected_type) for member in annotation)
        return False

    @staticmethod
    def is_instance(value: Any, expected_type: type) -> bool:
        """``isinstance()`` that doesn't count a bool as an int.

        Args:
            value: A value.
            expected_type: A type.

        Returns:
            Whether ``value`` is a value of ``expected_type``.
        """
        if isinstance(value, bool) and expected_type is not bool:
            return False
        return isinstance(value, expected_type)

    @classmethod
    def accepts_shape(cls, annotation: Any, value_shape: LookupValueShape, expected_type: Any) -> bool:
        """Whether a parameter's annotation takes the value of a lookup.

        Args:
            annotation: The parameter's annotation.
            value_shape: Whether the lookup takes one value, a list or a two-item range.
            expected_type: The type of the value - of each item of a list or range.

        Returns:
            True when every value the annotation allows fits the lookup.
        """
        if value_shape is LookupValueShape.VALUE:
            return cls.accepts_value(annotation, expected_type)
        alternatives = cls.get_alternatives(annotation)
        if not alternatives:
            return False
        for alternative in alternatives:
            origin = get_origin(alternative)
            arguments = get_args(alternative)
            if value_shape is LookupValueShape.RANGE:
                if origin is tuple and len(arguments) == 2 and Ellipsis not in arguments:
                    if not all(cls.accepts_value(argument, expected_type) for argument in arguments):
                        return False
                    continue
                if origin is list and len(arguments) == 1 and cls.accepts_value(arguments[0], expected_type):
                    continue
                return False
            if origin not in LIST_CONTAINERS or not arguments:
                return False
            if origin is tuple and (len(arguments) != 2 or arguments[1] is not Ellipsis):
                return False
            if not cls.accepts_value(arguments[0], expected_type):
                return False
        return True

    @classmethod
    def takes_many_values(cls, annotation: Any, metadata: list[Any]) -> bool:
        """Whether a parameter takes every value of a repeated query parameter, not one.

        A text type (``KeyColumns``, ``CommaSeparated``) takes one text value, except that a
        ``CommaSeparated`` list joins the texts of a repeated parameter.

        Args:
            annotation: The parameter's annotation, its top-level ``Annotated`` metadata aside.
            metadata: That metadata.

        Returns:
            True for a list, set, tuple or range, and for a ``CommaSeparated`` list.
        """
        if any(isinstance(item, TextParameter) for item in metadata):
            return get_origin(annotation) is list
        stripped = annotation
        while get_origin(stripped) is Annotated:
            stripped = get_args(stripped)[0]
        members = (
            [member for member in get_args(stripped) if member is not NoneType]
            if get_origin(stripped) in (Union, UnionType)
            else [annotation]
        )
        for member in members:
            if get_origin(member) is Annotated:
                arguments = get_args(member)
                if any(isinstance(item, TextParameter) for item in arguments[1:]):
                    return get_origin(arguments[0]) is list
            if get_origin(cls.strip(member)) in LIST_CONTAINERS:
                return True
        return False

    @classmethod
    def describe(cls, value_shape: LookupValueShape, expected_type: Any) -> str:
        """How the value a lookup takes is annotated, for an error message.

        Args:
            value_shape: One value, a list or a two-item range.
            expected_type: The type of the value - of each item of a list or range.

        Returns:
            ``int``, ``list[int]``, ``tuple[int, int]``, ``tuple[UUID, int]`` for a composite key.
        """
        item = cls.describe_type(expected_type)
        if value_shape is LookupValueShape.LIST:
            return f"list[{item}]"
        if value_shape is LookupValueShape.RANGE:
            return f"tuple[{item}, {item}]"
        return item

    @classmethod
    def describe_type(cls, expected_type: Any) -> str:
        """The name of a value type, for an error message.

        Args:
            expected_type: A type, or a tuple of types for a composite key.

        Returns:
            The type's name.
        """
        if isinstance(expected_type, tuple):
            return f"tuple[{', '.join(cls.describe_type(item) for item in expected_type)}]"
        if expected_type is None or expected_type is object:
            return "any value"
        return getattr(expected_type, "__name__", repr(expected_type))
