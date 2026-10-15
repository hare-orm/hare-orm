from __future__ import annotations

import re

from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance

#: The collation providers CreateCollation takes.
COLLATION_PROVIDERS = frozenset({"libc", "icu"})

DIRECT_RELATION_FIELDS = (
    ForeignKeyFieldInstance,
    ManyToManyFieldInstance,
    OneToOneFieldInstance,
)

#: A column in a view's query, ``{column}`` standing for its escaped name: with the ``AS`` making it
#: an output's name, the table it is read of, and its quote.
VIEW_QUERY_COLUMN_PATTERN_TEMPLATE = (
    r"(?P<alias>(?i:\bAS\s+))?(?P<qualifier>(?:[\w\"`]+\.)*)(?P<quote>[\"`]?)\b{column}\b(?P=quote)"
)

#: What stands before a column selected as it is - the ``SELECT`` or the comma of the item before it.
SELECTED_COLUMN_START_RE = re.compile(r"(?:,|\bSELECT(?:\s+DISTINCT)?)$", re.IGNORECASE)

#: What stands after a column selected as it is - the comma of the next item, the ``FROM``, the end.
SELECTED_COLUMN_END_RE = re.compile(r"(?:,|FROM\b|$)", re.IGNORECASE)

#: The error of an operation naming a model the migrations before it don't create.
MISSING_MODEL_MESSAGE = "Model {app_label}.{model_name} is not present - no migration before this one creates it"
