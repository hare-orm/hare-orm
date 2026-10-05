from __future__ import annotations

#: What separates the values of a composite key and the items of a comma-separated list in one
#: parameter (``?pk=2,1``, ``?ids=1,2,3``).
VALUE_SEPARATOR = ","

#: What separates the type of a generic foreign key's target from its key in a parameter
#: (``?target=post:1``).
GENERIC_TARGET_SEPARATOR = ":"
