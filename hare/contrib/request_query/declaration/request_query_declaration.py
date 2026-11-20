from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from hare.contrib.request_query.api_getter.api_source import ApiSource
from hare.contrib.request_query.api_getter.api_value_mapping import ApiValueMapping
from hare.contrib.request_query.bounds.parameter_bounds import ParameterBounds
from hare.contrib.request_query.options.deleted_config import DeletedConfig
from hare.contrib.request_query.options.fields_config import FieldsConfig
from hare.contrib.request_query.options.include_config import IncludeConfig
from hare.contrib.request_query.options.latest_versions import LatestVersions
from hare.contrib.request_query.options.ordering_config import OrderingConfig
from hare.contrib.request_query.options.search_config import SearchConfig
from hare.contrib.request_query.paginations.pagination import Pagination
from hare.query.enums import Connector
from hare.query.queryset import QuerySet

if TYPE_CHECKING:
    from hare.models import Model
    from hare.query.lookup_info.lookup_info import LookupInfo
    from hare.query.lookup_info.ordering_info import OrderingInfo
from hare.contrib.request_query.declaration.filter_declaration import FilterDeclaration


@dataclasses.dataclass(frozen=True, slots=True)
class RequestQueryDeclaration:
    """A request query class, checked against its models.

    Attributes:
        queryset: The rows the query starts from - ``Meta.queryset``.
        filters: The parameters it filters by.
        bounds: The checks that the bounds a request gives a field leave any value between them.
        search: The search option, None without a search.
        ordering: The ordering option, None when a request can't choose the ordering.
        key_ordering_paths: The primary key's fields every ordering ends with - empty for a model
            without a primary key.
        fields: The fields option, None when a request can't choose the fields to load.
        include: The include option, None when a request can't ask for relations to load.
        include_to_many: The relations a request may ask to load, each with whether it holds many rows
            (loaded with ``prefetch_related()``) or one (``select_related()``); empty without the
            include option.
        pagination: The pagination option, None without pagination.
        join_type: How the conditions of the parameters join - ``Connector.AND`` or ``Connector.OR``.
        versions: The option keeping only the latest version of a ``VersionedModel``'s records,
            None without it.
        deleted: The option letting a request see soft-deleted rows, None without it.
        api_sources: The sources of ``Meta.api_getter``.
        api_mappings: The mappings of ``Meta.api_getter`` taking a value from the row itself.
        api_target_fields: The fields of the row each target of ``Meta.api_getter`` is filled from, by the
            target's name - loaded whenever a request's ``fields`` names the target.
        on_describe: Tracks the models of descriptions obtained through the queryset's shared
            caches, including paths first used after the declaration was built.
    """

    queryset: QuerySet[Any]
    filters: tuple[FilterDeclaration, ...]
    bounds: tuple[ParameterBounds, ...]
    search: SearchConfig | None
    ordering: OrderingConfig | None
    key_ordering_paths: tuple[str, ...]
    fields: FieldsConfig | None
    include: IncludeConfig | None
    include_to_many: Mapping[str, bool]
    pagination: Pagination | None
    join_type: Connector
    versions: LatestVersions | None
    deleted: DeletedConfig | None
    api_sources: tuple[ApiSource, ...]
    api_mappings: tuple[ApiValueMapping, ...]
    api_target_fields: Mapping[str, tuple[str, ...]]
    on_describe: Callable[[LookupInfo | OrderingInfo], None] | None = dataclasses.field(
        default=None, compare=False, repr=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "include_to_many", MappingProxyType(dict(self.include_to_many)))
        object.__setattr__(self, "api_target_fields", MappingProxyType(dict(self.api_target_fields)))

    def get_lookup_info(self, queryset: QuerySet[Any], key: str) -> LookupInfo:
        """Get a filter description through the queryset's model and annotation caches.

        Args:
            queryset: The filtered queryset, including its current annotations.
            key: The filter key.

        Returns:
            The description.

        Raises:
            FieldError: The key names no field, relation or annotation.
        """
        lookup_info = queryset.get_lookup_info(key)
        if self.on_describe is not None:
            self.on_describe(lookup_info)
        return lookup_info

    def get_ordering_info(self, queryset: QuerySet[Any], name: str) -> OrderingInfo:
        """Get an ordering description through the queryset's shared caches.

        Args:
            queryset: The queryset being ordered, including its current annotations.
            name: The ordering name, without its direction.

        Returns:
            The description.
        """
        ordering_info = queryset.get_ordering_info(name)
        if self.on_describe is not None:
            self.on_describe(ordering_info)
        return ordering_info

    @staticmethod
    def get_models(description: LookupInfo | OrderingInfo) -> set[type[Model]]:
        """The models a description reads - the model its key or name starts at and each model
        its relations lead to.

        Args:
            description: A filter key's or an ordering name's description.

        Returns:
            The models.
        """
        return {description.model, *(relation.related_model for relation in description.relations)}  # type: ignore[attr-defined]
