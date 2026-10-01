from __future__ import annotations

from hare.fields.enums import RelationType

#: The ``opt`` key of a route handler that runs outside the request's transaction:
#: ``@post("/import", opt={SKIP_TRANSACTION_OPT_KEY: True})``.
SKIP_TRANSACTION_OPT_KEY = "hare_skip_transaction"

#: Where Litestar's validation errors say a parameter comes from.
QUERY_PARAMETER_SOURCE = "query"
PATH_PARAMETER_SOURCE = "path"

#: The relations holding many rows for one row.
TO_MANY_RELATION_TYPES = frozenset({RelationType.MANY_TO_MANY, RelationType.BACKWARD_FOREIGN_KEY})
#: What joins the fields of a path through relations in a DTO's configuration.
DTO_PATH_SEPARATOR = "."

#: The place Litestar gives the row in a relation's "row or null", and a row in a list of rows.
DTO_CHOICE_PLACE = "0"
