from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
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
        lookup_infos: The ORM's description of each filter key, by key: the parameters' and the
            search's, read when the declaration is built, and every other key the query meets
            (a filter method's condition, a handler's ``where()``, the access condition), read the
            first time - so a request reads no description of its conditions.
        ordering: The ordering option, None when a request can't choose the ordering.
        ordering_infos: The ORM's description of each ordering name without its direction, by
            name: the names a request may order by and the default ones, read when the declaration
            is built, and every other name the query meets (the queryset's own ordering, the model's
            ``Meta.ordering``, a handler's ``order_by()``), read the first time - so a request reads
            no description of its ordering.
        key_ordering_paths: The primary key's fields every ordering ends with - empty for a model
            without a primary key.
        fields: The fields option, None when a request can't choose the fields to load.
        include: The relations a request may ask to load, each with whether it holds many rows
            (loaded with ``prefetch_related()``) or one (``select_related()``); empty without the
            include option.
        pagination: The pagination option, None without pagination.
        join_type: How the conditions of the parameters join - ``Connector.AND`` or ``Connector.OR``.
        versions: The option keeping only the latest version of a ``VersionedModel``'s records,
            None without it.
        deleted: The option letting a request see soft-deleted rows, None without it.
        dialects: The dialects every filter was checked against.
        on_describe: Told of each description read after the declaration was built (a key or a
            name the query meets later) - the request query then knows every model its
            descriptions read; None to tell no one.
    """

    queryset: QuerySet[Any]
    filters: tuple[FilterDeclaration, ...]
    bounds: tuple[ParameterBounds, ...]
    search: SearchConfig | None
    lookup_infos: dict[str, LookupInfo]
    ordering: OrderingConfig | None
    ordering_infos: dict[str, OrderingInfo]
    key_ordering_paths: tuple[str, ...]
    fields: FieldsConfig | None
    include: IncludeConfig | None
    include_to_many: dict[str, bool]
    pagination: Pagination | None
    join_type: Connector
    versions: LatestVersions | None
    deleted: DeletedConfig | None
    dialects: tuple[Dialect, ...]
    on_describe: Callable[[LookupInfo | OrderingInfo], None] | None = dataclasses.field(
        default=None, compare=False, repr=False
    )

    @property
    def model(self) -> Any:
        """The model of the queryset."""
        return self.queryset.model

    def get_lookup_info(self, queryset: QuerySet[Any], key: str) -> LookupInfo:
        """The ORM's description of a filter key, read once per key.

        Args:
            queryset: The filtered queryset - it describes a key the declaration hasn't met.
            key: The filter key.

        Returns:
            The description.

        Raises:
            FieldError: The key names no field, relation or annotation.
        """
        lookup_info = self.lookup_infos.get(key)
        if lookup_info is None:
            lookup_info = queryset.get_lookup_info(key)
            self.lookup_infos[key] = lookup_info
            if self.on_describe is not None:
                self.on_describe(lookup_info)
        return lookup_info

    def get_ordering_info(self, queryset: QuerySet[Any], name: str) -> OrderingInfo:
        """The ORM's description of an ordering name, read once per name.

        Args:
            queryset: The queryset being ordered - it describes a name the declaration hasn't met.
            name: The ordering name, without its direction.

        Returns:
            The description.
        """
        ordering_info = self.ordering_infos.get(name)
        if ordering_info is None:
            ordering_info = queryset.get_ordering_info(name)
            self.ordering_infos[name] = ordering_info
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
