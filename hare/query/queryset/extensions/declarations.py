from __future__ import annotations

from dataclasses import dataclass

from hare.query.queryset.extensions.query_set_extension import QuerySetExtension
from hare.query.queryset.extensions.query_set_extension_call import QuerySetExtensionCall


@dataclass(frozen=True, slots=True)
class BoundExtensionCall:
    call: QuerySetExtensionCall
    implementation: QuerySetExtension
