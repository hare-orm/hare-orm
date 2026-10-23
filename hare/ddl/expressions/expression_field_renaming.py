from __future__ import annotations

from collections import Counter
from typing import Any

from hare.query.expressions import Expression, F, Ordering, Q
from hare.query.plans.enums import PlanPartType


class ExpressionFieldRenaming:
    """The field names an expression reads - an index key, an ordering, a generated column's
    expression - and the expression with one renamed, rebuilt from the arguments it was constructed with."""

    @staticmethod
    def get_renamed_path(path: str, old_name: str, new_name: str) -> str:
        """A field path starting at the renamed field, renamed.

        Args:
            path: The field path (``name``, ``author__name``).
            old_name: The field's former name.
            new_name: The field's new name.

        Returns:
            The path, its first part renamed when it was the field.
        """
        first, separator, rest = path.partition("__")
        return f"{new_name}{separator}{rest}" if first == old_name else path

    @staticmethod
    def get_renamed(value: Any, old_name: str, new_name: str) -> Any:
        """The value with every field name it reads renamed - an expression, an ordering, a
        condition, or a list or tuple of them.

        Args:
            value: The value.
            old_name: The field's former name.
            new_name: The field's new name.

        Returns:
            The renamed copy, or ``value`` itself when it reads no such field.
        """
        if isinstance(value, (list, tuple)):
            renamed_items = [ExpressionFieldRenaming.get_renamed(item, old_name, new_name) for item in value]
            if all(renamed is item for renamed, item in zip(renamed_items, value, strict=True)):
                return value
            return type(value)(renamed_items)
        if isinstance(value, Q):
            if old_name not in value.get_referenced_field_names():
                return value
            return value.with_renamed_field(old_name, new_name)
        if isinstance(value, Ordering):
            target = value.target
            renamed_target = (
                ExpressionFieldRenaming.get_renamed_path(target, old_name, new_name)
                if isinstance(target, str)
                else ExpressionFieldRenaming.get_renamed(target, old_name, new_name)
            )
            return value if renamed_target is target else Ordering(renamed_target, value.order)
        if type(value) is F:
            renamed_path = ExpressionFieldRenaming.get_renamed_path(value.name, old_name, new_name)
            return value if renamed_path == value.name else F(renamed_path)
        if isinstance(value, Expression):
            return ExpressionFieldRenaming.get_renamed_expression(value, old_name, new_name)
        return value

    @staticmethod
    def get_renamed_expression(expression: Expression, old_name: str, new_name: str) -> Expression:
        """An expression rebuilt from its constructor's arguments with the field renamed - a string
        argument is renamed only as often as the expression reads it as a field (a ``FIELD``/``FIELDS``
        part of its plan), the arguments taken in their order - never a literal such as ``Trunc``'s unit,
        which follows the field it reads.

        Args:
            expression: The expression.
            old_name: The field's former name.
            new_name: The field's new name.

        Returns:
            The rebuilt expression, or ``expression`` itself when it reads no such field.
        """
        _path, args, kwargs = expression.deconstruct()
        read_field_names = ExpressionFieldRenaming.get_read_field_names(expression)

        def get_renamed_argument(argument: Any) -> Any:
            if isinstance(argument, str):
                if read_field_names[argument] == 0:
                    return argument
                read_field_names[argument] -= 1
                return ExpressionFieldRenaming.get_renamed_path(argument, old_name, new_name)
            return ExpressionFieldRenaming.get_renamed(argument, old_name, new_name)

        renamed_args = [get_renamed_argument(argument) for argument in args]
        renamed_kwargs = {name: get_renamed_argument(argument) for name, argument in kwargs.items()}
        if all(renamed is argument for renamed, argument in zip(renamed_args, args, strict=True)) and all(
            renamed_kwargs[name] is argument for name, argument in kwargs.items()
        ):
            return expression
        return type(expression)(*renamed_args, **renamed_kwargs)

    @staticmethod
    def get_all_read_field_names(value: Any) -> set[str]:
        """The fields a value reads anywhere inside it - an expression and the expressions it is built
        of, an ordering, a condition, or a list or tuple of them.

        Args:
            value: The value.

        Returns:
            The names of the fields the reads begin at - ``author`` of ``author__name``.
        """
        if isinstance(value, (list, tuple)):
            return {name for item in value for name in ExpressionFieldRenaming.get_all_read_field_names(item)}
        if isinstance(value, Q):
            return value.get_referenced_field_names()
        if isinstance(value, Ordering):
            target = value.target
            if isinstance(target, str):
                return {target.partition("__")[0]}
            return ExpressionFieldRenaming.get_all_read_field_names(target)
        if type(value) is F:
            return {value.name.partition("__")[0]}
        if not isinstance(value, Expression):
            return set()
        read_field_names = {path.partition("__")[0] for path in ExpressionFieldRenaming.get_read_field_names(value)}
        _path, args, kwargs = value.deconstruct()
        for argument in (*args, *kwargs.values()):
            if not isinstance(argument, str):
                read_field_names |= ExpressionFieldRenaming.get_all_read_field_names(argument)
        return read_field_names

    @staticmethod
    def get_read_field_names(expression: Expression) -> Counter[str]:
        """The field paths an expression reads by name - the strings of its ``FIELD``/``FIELDS`` plan
        parts, each with how often it is read.

        Args:
            expression: The expression.

        Returns:
            The paths.
        """
        read_field_names: Counter[str] = Counter()
        for attribute, part_type in getattr(type(expression), "plan_parts", ()):
            if part_type not in (PlanPartType.FIELD, PlanPartType.FIELDS):
                continue
            value = getattr(expression, attribute, None)
            for item in value if isinstance(value, (list, tuple)) else (value,):
                if isinstance(item, str):
                    read_field_names[item] += 1
        return read_field_names
