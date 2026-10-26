from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from hare.contrib.request_query.api_getter.api_getter_retry import ApiGetterRetry
from hare.contrib.request_query.api_getter.api_getter_triggers import ApiGetterTriggers
from hare.contrib.request_query.api_getter.api_value_mapping import ApiValueMapping
from hare.contrib.request_query.api_getter.constants import (
    API_PATH_SEPARATOR,
    DEFAULT_API_BATCH_SIZE,
    DEFAULT_API_CONCURRENCY,
    DEFAULT_API_TIMEOUT_SECONDS,
)
from hare.contrib.request_query.api_getter.enums import ApiRequestMode
from hare.exceptions import ConfigurationError


@dataclasses.dataclass(frozen=True, slots=True)
class ApiSource:
    """An HTTP API values come from - asked for every value of ``key`` the objects hold, each answer item
    matched back to the value it answers by ``match_key`` and put into the targets of ``mappings`` of the
    objects holding that value.

    Args:
        url: The URL; for ``ApiRequestMode.PATH``, a template naming the key's last part
            (``"https://users/api/users/{user_id}"``).
        key: The path of the value on the objects (``"partner.user_id"``) - lists on the way are walked.
        mappings: Where the answer item's values go.
        name: What errors and events call the source - ``key`` when None.
        request_mode: One request per value in the URL, one per value as a query parameter, or one per
            batch of values as a repeated query parameter.
        query_parameter: The query parameter of the values - for the query modes.
        match_key: The key of an answer item holding the value it answers - for ``QUERY_MANY`` the key's
            last part when None; for the other modes the first item answers when None.
        key_type: What the item's value is read through before it is matched (``UUID``, ``int``) - a value
            it refuses matches nothing.
        items_path: The path of the items in the answer (``"result"``) - the answer itself when None.
        headers: The headers of every request, or an ``async`` function giving them - called once a run;
            left out of the source's repr.
        required: A value without an answer raises instead of leaving its targets empty.
        timeout_seconds: The longest a request may take.
        concurrency: The most requests of the source at a time.
        batch_size: The most values one ``QUERY_MANY`` request asks for.
        retry: How a failed request is repeated.
        triggers: When the source's values are loaded.

    Raises:
        ConfigurationError: A query mode without ``query_parameter``, a ``PATH`` URL not naming the key.
    """

    url: str
    key: str
    mappings: tuple[ApiValueMapping, ...]
    name: str | None = None
    request_mode: ApiRequestMode = ApiRequestMode.QUERY_MANY
    query_parameter: str | None = None
    match_key: str | None = None
    key_type: Callable[[Any], Any] | None = None
    items_path: str | None = None
    # Left out of the repr - the headers carry the keys of the service.
    headers: Mapping[str, str] | Callable[[], Awaitable[Mapping[str, str]]] = dataclasses.field(
        default_factory=dict, repr=False
    )
    required: bool = False
    timeout_seconds: float = DEFAULT_API_TIMEOUT_SECONDS
    concurrency: int = DEFAULT_API_CONCURRENCY
    batch_size: int = DEFAULT_API_BATCH_SIZE
    retry: ApiGetterRetry = ApiGetterRetry()
    triggers: ApiGetterTriggers = ApiGetterTriggers()

    def __post_init__(self) -> None:
        if self.request_mode is ApiRequestMode.PATH:
            placeholder = "{" + self.get_key_name() + "}"
            if placeholder not in self.url:
                raise ConfigurationError(
                    f"ApiSource {self.key!r}: a PATH url must name {placeholder}, got {self.url!r}"
                )
        elif self.query_parameter is None:
            raise ConfigurationError(f"ApiSource {self.key!r}: a {self.request_mode} source needs query_parameter")

    def get_name(self) -> str:
        """What errors and events call the source."""
        return self.name or self.key

    def get_key_name(self) -> str:
        """The last part of ``key`` - the name of the value in the object holding it."""
        return self.key.rsplit(API_PATH_SEPARATOR, 1)[-1]

    def get_target_names(self) -> frozenset[str]:
        """The first parts of the targets' paths - the names a request's ``fields`` asks for them by."""
        return frozenset(self.get_root_name(mapping.set_key) for mapping in self.mappings)

    def get_key_root_name(self) -> str:
        """The first part of ``key`` - the field of the rows holding the values asked for."""
        return self.get_root_name(self.key)

    @staticmethod
    def get_root_name(path: str) -> str:
        """The first part of a path.

        Args:
            path: The path.
        """
        return path.split(API_PATH_SEPARATOR, 1)[0]
