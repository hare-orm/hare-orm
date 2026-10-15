from __future__ import annotations

from enum import StrEnum


class AnnotationTypeRule(StrEnum):
    """How the type of an annotation follows from its expression."""

    #: Always an ``int`` (``Count``, ``Length``).
    INT = "int"
    #: Always a ``bool`` (``Exists``).
    BOOL = "bool"
    #: The type of the field path the expression reads (``F("price")``).
    PATH_VALUE = "path_value"
    #: The type of the field path, or None when there are no rows (``Sum``, ``Min``, ``Max``).
    OPTIONAL_PATH_VALUE = "optional_path_value"
    #: The type of the expression's argument (``Value(5)``).
    ARGUMENT_TYPE = "argument_type"
