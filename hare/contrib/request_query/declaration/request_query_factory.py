from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.contrib.request_query.api_getter.api_source import ApiSource
from hare.contrib.request_query.api_getter.api_value_mapping import ApiValueMapping
from hare.contrib.request_query.constants import META_CLASS_NAME
from hare.contrib.request_query.options.deleted_config import DeletedConfig
from hare.contrib.request_query.options.fields_config import FieldsConfig
from hare.contrib.request_query.options.filter_field import FilterField
from hare.contrib.request_query.options.include_config import IncludeConfig
from hare.contrib.request_query.options.latest_versions import LatestVersions
from hare.contrib.request_query.options.ordering_config import OrderingConfig
from hare.contrib.request_query.options.search_config import SearchConfig
from hare.contrib.request_query.paginations.pagination import Pagination
from hare.core.hare_context import HareContext
from hare.exceptions import ConfigurationError
from hare.query.enums import Connector
from hare.query.queryset import QuerySet

if TYPE_CHECKING:
    from hare.contrib.request_query.request_query import RequestQuery
    from hare.core.apps.declarations import ModelReference


class RequestQueryFactory:
    """Builds a request query declaration for a runtime model, retaining the requested base class."""

    @staticmethod
    def for_model(
        query_class: type[RequestQuery[Any]],
        model: ModelReference,
        *,
        name: str | None = None,
        queryset: QuerySet[Any] | None = None,
        filters: Iterable[FilterField] | None,
        search: SearchConfig | None,
        ordering: OrderingConfig | None,
        pagination: Pagination | None,
        fields: FieldsConfig | None,
        include: IncludeConfig | None,
        deleted: DeletedConfig | None,
        versions: LatestVersions | None,
        join_type: Connector,
        api_getter: Iterable[ApiSource | ApiValueMapping] | None,
    ) -> type[RequestQuery[Any]]:
        """A request query class of a model known only while the application runs - one
        registered with ``Hare.register_live_models()`` - built and checked at once::

            ContentQuery = RequestQuery.for_model(
                content_model,
                filters=(FilterField("status", lookups=(Lookup.EXACT, Lookup.IN)),),
                ordering=OrderingConfig(fields=("title",)),
            )

        The class is held weakly like any other: once nothing holds it, it leaves the
        declarations and ``get_concrete_subclasses()``. Called on a dialect's request query,
        the class is of that dialect.

        Args:
            model: The model - bound, so its filters can be checked: its class, its ``"app.Model"`` label
                or a ``swappable()`` setting, looked up in the current context.
            name: The class's name, ``<Model>RequestQuery`` by default.
            queryset: The rows the query starts from - ``model.objects`` by default.
            filters: ``Meta.filters``.
            search: ``Meta.search``.
            ordering: ``Meta.ordering``.
            pagination: ``Meta.pagination`` - an ``OffsetPagination()`` by default.
            fields: ``Meta.fields``.
            include: ``Meta.include``.
            deleted: ``Meta.deleted``.
            versions: ``Meta.versions``.
            join_type: ``Meta.join_type``.
            api_getter: ``Meta.api_getter``.

        Returns:
            The request query class.

        Raises:
            ConfigurationError: ``queryset`` is of another model, or the declaration is wrong - as
                ``check_declarations()`` finds it.
        """
        if not isinstance(model, type):
            model = HareContext.require_current().get_model(model)
        if queryset is None:
            queryset = model.objects.all()
        elif queryset.model is not model:
            raise ConfigurationError(
                f"{query_class.__qualname__}.for_model({model.__name__}) takes a queryset of {model.__name__}, "
                f"got {queryset!r}"
            )
        class_name = name if name is not None else f"{model.__name__}RequestQuery"
        # Named as a class nested in the new one - pydantic leaves such a class out of the fields.
        meta = type(
            META_CLASS_NAME,
            (),
            {
                "__module__": query_class.__module__,
                "__qualname__": f"{class_name}.{META_CLASS_NAME}",
                "queryset": queryset,
                "filters": filters,
                "search": search,
                "ordering": ordering,
                "pagination": pagination,
                "fields": fields,
                "include": include,
                "deleted": deleted,
                "versions": versions,
                "join_type": join_type,
                "api_getter": api_getter,
            },
        )
        base = query_class[model] if query_class.__pydantic_generic_metadata__["parameters"] else query_class  # type: ignore[index]
        request_query_class: type[RequestQuery[Any]] = type(
            class_name,
            (base,),
            {"__module__": query_class.__module__, "__qualname__": class_name, META_CLASS_NAME: meta},
        )
        request_query_class.check_declaration()
        return request_query_class
