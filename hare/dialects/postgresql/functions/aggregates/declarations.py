from __future__ import annotations

from hare.classes.declared_subclass import DeclaredSubclass
from hare.query.expressions import (
    Aggregate,
)

BoolAnd = DeclaredSubclass.make(
    Aggregate,
    "BoolAnd",
    __package__,
    """BOOL_AND(field) - true per GROUP BY group only if every row's value is true.

    Example: ``Model.objects.annotate(all_active=BoolAnd("is_active")).group_by("id")``""",
    function_name="BOOL_AND",
    ignores_repeated_rows=True,
)


BoolOr = DeclaredSubclass.make(
    Aggregate,
    "BoolOr",
    __package__,
    """BOOL_OR(field) - true per GROUP BY group if at least one row's value is true.

    Example: ``Model.objects.annotate(any_active=BoolOr("is_active")).group_by("id")``""",
    function_name="BOOL_OR",
    ignores_repeated_rows=True,
)


BitAnd = DeclaredSubclass.make(
    Aggregate,
    "BitAnd",
    __package__,
    """BIT_AND(field) - the bitwise AND of an integer column per GROUP BY group.

    Example: ``Model.objects.annotate(common_flags=BitAnd("flags")).group_by("owner")``""",
    function_name="BIT_AND",
    populate_field_object=True,
    ignores_repeated_rows=True,
)


BitOr = DeclaredSubclass.make(
    Aggregate,
    "BitOr",
    __package__,
    """BIT_OR(field) - the bitwise OR of an integer column per GROUP BY group.

    Example: ``Model.objects.annotate(any_flags=BitOr("flags")).group_by("owner")``""",
    function_name="BIT_OR",
    populate_field_object=True,
    ignores_repeated_rows=True,
)


BitXor = DeclaredSubclass.make(
    Aggregate,
    "BitXor",
    __package__,
    """BIT_XOR(field) - the bitwise exclusive OR of an integer column per GROUP BY group.

    Example: ``Model.objects.annotate(parity=BitXor("flags")).group_by("owner")``""",
    function_name="BIT_XOR",
    populate_field_object=True,
)
