from __future__ import annotations

import re

#: What separates the values of a composite key and the items of a comma-separated list in one
#: parameter (``?pk=2,1``, ``?ids=1,2,3``).
VALUE_SEPARATOR = ","

#: What separates the type of a generic foreign key's target from its key in a parameter
#: (``?target=post:1``).
GENERIC_TARGET_SEPARATOR = ":"

#: The escapes of a key value's text, once the URL is decoded - a comma inside a value is `%2C`, a
#: percent sign `%25`; any other `%` stays as it is.
KEY_VALUE_ESCAPE_PATTERN = re.compile("%(2C|2c|25)")
KEY_VALUE_ESCAPES = {"2C": ",", "2c": ",", "25": "%"}
