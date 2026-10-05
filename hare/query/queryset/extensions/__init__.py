"""Methods a dialect adds to ``QuerySet`` - ``.final()`` or ``.sample()`` of a columnar
database - without hare's core knowing them."""

from __future__ import annotations

from hare.query.queryset.extensions.query_set_extension import QuerySetExtension
from hare.query.queryset.extensions.query_set_extension_call import QuerySetExtensionCall
from hare.query.queryset.extensions.query_set_extension_query import QuerySetExtensionQuery
from hare.query.queryset.extensions.query_set_extensions import ExtensionApply, QuerySetExtensions

__all__ = [
    "ExtensionApply",
    "QuerySetExtension",
    "QuerySetExtensionCall",
    "QuerySetExtensionQuery",
    "QuerySetExtensions",
]
