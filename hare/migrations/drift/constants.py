from __future__ import annotations

import re

from hare.models.enums import ModelOption

#: Whitespace and parentheses - left out of a default expression's fingerprint, since the
#: database adds and drops both when it echoes the expression back.
SQL_FINGERPRINT_NOISE_RE = re.compile(r"[\s()]+")

#: The fingerprint of a default taking the current time, whichever spelling it has.
CURRENT_TIME_DEFAULT_FINGERPRINT = "now"

#: The objects a model declares beside its table drift compares with the ones the database has, where
#: the connection's introspector reads them.
DRIFT_COMPARED_SCHEMA_OBJECT_OPTIONS = (ModelOption.VIEWS, ModelOption.MATERIALIZED_VIEWS, ModelOption.DICTIONARIES)
