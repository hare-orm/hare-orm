from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hare.contrib.request_query.declaration.request_query_declaration import RequestQueryDeclaration
    from hare.contrib.request_query.options.request_option import RequestOption
    from hare.contrib.request_query.request_query import RequestQuery


@dataclass(frozen=True, slots=True)
class RequestQueryGeneration:
    """A completed parameter model and the declaration validated against its fields.

    Instances retain this snapshot through their generated class. Invalidating the
    declaration cache affects future construction, never a published Pydantic class.
    """

    schema: type[RequestQuery[Any]]
    declaration: RequestQueryDeclaration | None
    options: tuple[RequestOption, ...]
