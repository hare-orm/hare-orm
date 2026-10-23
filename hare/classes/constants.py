from __future__ import annotations

#: The module a package keeps its declaration-only classes in - a class there is imported by its
#: package path, as a class in a module named after it is.
DECLARATIONS_MODULE_NAME = "declarations"

#: How much of a refused value an error message shows - the characters of a text or of any other
#: value's repr, the items of a collection, the levels of nesting; past them, "...".
SHOWN_VALUE_MAX_TEXT_LENGTH = 80
SHOWN_VALUE_MAX_ITEMS = 10
SHOWN_VALUE_MAX_LEVELS = 4
