from __future__ import annotations

CONTAINER_MAX_DEPTH = 255
CONTAINER_NESTING_MESSAGE = (
    "{label}: a list, tuple or dict nesting deeper than {limit} levels, or holding itself, can't be a value"
)

#: The module a package keeps its declaration-only classes in - a class there is imported by its
#: package path, as a class in a module named after it is.
DECLARATIONS_MODULE_NAME = "declarations"
FUNCTION_DECLARATION_PATH_ATTRIBUTE = "__hare_declaration_path__"
UNSUPPORTED_COMPARABLE_VALUE = "Cannot compare declaration value of type {type}: provide deconstruct()."

#: How much of a refused value an error message shows - the characters of a text or of any other
#: value's repr, the items of a collection, the levels of nesting; past them, "...".
SHOWN_VALUE_MAX_TEXT_LENGTH = 80
SHOWN_VALUE_MAX_ITEMS = 10
SHOWN_VALUE_MAX_LEVELS = 4
