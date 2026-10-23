"""Methods a dialect adds to ``QuerySet`` - ``.final()`` or ``.sample()`` of a columnar
database - without hare's core knowing them."""

from __future__ import annotations

from hare.query.queryset.extensions.query_set_extension_query import QuerySetExtensionQuery
from hare.query.queryset.extensions.query_set_extensions import QuerySetExtensions

__all__ = ["QuerySetExtensionQuery", "QuerySetExtensions"]
