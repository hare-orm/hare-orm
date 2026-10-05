"""``RequestQuery`` - the rows a request asks for, declared once: the queryset, the parameters it
filters by, the search, the orderings a request may choose and the pagination."""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
from collections.abc import Callable, Iterable
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar, Self, cast, overload
from urllib.parse import parse_qsl
from weakref import WeakKeyDictionary, WeakSet, ref

from pydantic import BaseModel, ConfigDict, PrivateAttr, ValidationError
from pydantic.fields import FieldInfo

from hare.contrib.request_query.constants import (
    DECLARATION_BUCKET_KEY,
    DESCENDING_PREFIX,
    MANY_VALUE_PARAMETERS_BUCKET_KEY,
    META_CLASS_NAME,
    PARAMETER_OPTION_NAMES,
    REQUEST_OPTIONS_BUCKET_KEY,
)
from hare.contrib.request_query.declaration.declaration_builder import DeclarationBuilder
from hare.contrib.request_query.declaration.filter_declaration import FilterDeclaration
from hare.contrib.request_query.declaration.filter_parameter_builder import FilterParameterBuilder
from hare.contrib.request_query.declaration.request_query_declaration import RequestQueryDeclaration
from hare.contrib.request_query.declaration.request_query_parameters import RequestQueryParameters
from hare.contrib.request_query.declaration.request_query_registry import RequestQueryRegistry
from hare.contrib.request_query.descriptions.parameter_describer import ParameterDescriber
from hare.contrib.request_query.descriptions.parameter_description import ParameterDescription
from hare.contrib.request_query.enums import DeletedRows, EachRowOnce
from hare.contrib.request_query.exceptions import InvalidRequestQuery, RequestQueryForbidden
from hare.contrib.request_query.options.deleted_config import DeletedConfig
from hare.contrib.request_query.options.fields_config import FieldsConfig
from hare.contrib.request_query.options.filter_field import FilterField
from hare.contrib.request_query.options.include_config import IncludeConfig
from hare.contrib.request_query.options.latest_versions import LatestVersions
from hare.contrib.request_query.options.ordering_config import OrderingConfig
from hare.contrib.request_query.options.request_option import RequestOption
from hare.contrib.request_query.options.search_config import SearchConfig
from hare.contrib.request_query.paginations.offset_pagination import OffsetPagination
from hare.contrib.request_query.paginations.pagination import Pagination
from hare.contrib.request_query.row_changes import RowChanges
from hare.contrib.request_query.value_annotation import ValueAnnotation
from hare.contrib.request_query.value_counter import ValueCounter
from hare.contrib.request_query.value_target import ValueTarget
from hare.core.caching.cache import Cache
from hare.core.caching.caches import Caches
from hare.core.hare_context import HareContext
from hare.exceptions import ConfigurationError, QueryError
from hare.models import Model
from hare.query.enums import Connector, GetException
from hare.query.expressions import Q
from hare.query.expressions.ordering import Ordering
from hare.query.queryset import QuerySet

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.contrib.request_query.results.cursor_page import CursorPage
    from hare.contrib.request_query.results.page import Page
    from hare.contrib.request_query.results.scroll_page import ScrollPage
    from hare.query.lookup_info.ordering_info import OrderingInfo
    from hare.query.queryset.single_rows.get_exception_argument import GetExceptionArgument

#: One item of an ordering: a name with ``-`` for descending, or an ordering expression.
type OrderingItem = str | Ordering


class RequestQuery[ModelType: Model](BaseModel):
    """The rows a request asks for.

    A subclass declares the queryset it starts from, its parameters - each a filter the ORM
    describes, checked when the application starts - and its search, ordering and pagination::

        class BookQuery(RequestQuery[Book]):
            author: int | None = None
            title__icontains: str | None = None
            genre_ids: Annotated[list[int] | None, Filter("genres", lookup="in")] = None

            class Meta:
                queryset = Book.objects.all().select_related("author")
                search = SearchConfig(fields=("title", "author__name"))
                ordering = OrderingConfig(fields=("title", "published_at"), default=("-published_at",))
                pagination = OffsetPagination(default_limit=50)

    A framework adapter builds an instance from the request's parameters; ``page()``, ``fetch()``,
    ``get()``, ``count()``, ``exists()`` and ``delete()`` run it.

    ``RequestQuery`` checks its filters against every dialect a driver connects to; a subclass
    naming one dialect (``dialect_name``) - each of hare's dialects has one in its package - checks
    against that dialect only, and may use the lookups only it runs.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    #: The dialect the class's filters are checked against, None for every dialect.
    dialect_name: ClassVar[str | None] = None

    #: The buckets of the caches the class reads (``Cache.get_owner_bucket()``) - a dict of its
    #: own for every request query class.
    cache_buckets: ClassVar[dict[int, Any]] = {}

    #: The checked declaration of each request query class, its options that add parameters and
    #: the parameters taking every value of a repeated query parameter, in a bucket the class
    #: keeps - so a class nothing else holds (one a test declares) is gone with it.
    declarations: ClassVar[Cache[Any]] = Cache(holds_sql=False, keyed_by_model=False)

    #: The request query classes ``prepare_parameters()`` completed - weakly, as ``declarations``.
    prepared_classes: ClassVar[WeakSet[type]] = WeakSet()

    #: The parameters of each prepared class as they were before ``prepare_parameters()`` added
    #: ``Meta.filters``' and described the filters - put back when the class is forgotten.
    unprepared_fields: ClassVar[WeakKeyDictionary[type, dict[str, FieldInfo]]] = WeakKeyDictionary()

    #: The request query classes whose parameters or declaration read each model - both weakly.
    #: A change to the model (``Caches.forget_model_caches()``: a live model unregistered or
    #: registered again, a relation added, any registration in ``Registries``) forgets them.
    classes_by_model: ClassVar[WeakKeyDictionary[type[Model], WeakSet[type]]] = WeakKeyDictionary()

    #: Every model a request query was prepared for while it was bound - one that is no longer
    #: bound was unregistered, not merely not bound yet.
    bound_models: ClassVar[WeakSet[type[Model]]] = WeakSet()

    class Meta:
        """The options of a request query.

        Attributes:
            queryset: The rows the query starts from. A class without one only serves as a base.
            search: A ``SearchConfig``, None without a search.
            ordering: An ``OrderingConfig``, None when a request can't choose the ordering.
            pagination: An ``OffsetPagination``, a ``ScrollPagination`` or a ``CursorPagination``,
                None without pagination.
            filters: A tuple of ``FilterField`` - the fields a request may filter by and each
                one's lookups: ``FilterField("status", lookups=(Lookup.EXACT, Lookup.IN))`` makes
                the parameters ``status`` and ``status__in``, typed by the ORM. None for only the
                parameters the class declares.
            join_type: How the conditions of the parameters join - ``Connector.AND`` or ``Connector.OR``.
            fields: A ``FieldsConfig``, None when a request can't choose the fields to load.
            include: An ``IncludeConfig``, None when a request can't ask for relations.
            deleted: A ``DeletedConfig``, None when a request can't see soft-deleted rows.
            versions: ``LatestVersions()`` to keep only the latest version of each record of a
                ``VersionedModel``, None for every version.
        """

        queryset: QuerySet[Any] | None = None
        filters: tuple[FilterField, ...] | None = None
        search: SearchConfig | None = None
        ordering: OrderingConfig | None = None
        pagination: Pagination | None = OffsetPagination()
        join_type: Connector = Connector.AND
        fields: FieldsConfig | None = None
        include: IncludeConfig | None = None
        deleted: DeletedConfig | None = None
        versions: LatestVersions | None = None

    _request: Any = PrivateAttr(default=None)
    _request_url: str | None = PrivateAttr(default=None)
    # Tuples, not lists with a default_factory - pydantic reads a factory's signature on every
    # instance it makes.
    _conditions: tuple[Q, ...] = PrivateAttr(default=())
    _explicit_ordering: tuple[OrderingItem, ...] | None = PrivateAttr(default=None)
    _queryset_changes: tuple[Callable[[QuerySet[Any]], QuerySet[Any]], ...] = PrivateAttr(default=())

    def __init__(self, request: Any = None, **values: Any) -> None:
        """Reads the parameters of a request.

        Args:
            request: The request the parameters come from - its ``url`` makes the links of a page.
            values: The parameters.

        Raises:
            InvalidRequestQuery: A value has the wrong type or is out of range, an option's
                parameter names what the option doesn't allow, or a field's bounds leave no value
                between them (``published_at__gte`` after ``published_at__lte``).
        """
        type(self).prepare_parameters()
        try:
            super().__init__(**values)
        except ValidationError as error:
            raise InvalidRequestQuery(
                [
                    {"loc": list(item["loc"]), "msg": item["msg"], "type": item["type"]}
                    for item in error.errors(include_url=False)
                ]
            ) from error
        self._request = request
        parameter_values = RequestQueryParameters.get_parameter_values(self)
        for option in self.get_request_options():
            option.check_request(parameter_values)
        for bounds in self.get_declaration().bounds:
            bounds.check(parameter_values)

    @classmethod
    def from_query_string(cls, query_string: str, *, request: Any = None, **values: Any) -> Self:
        """Builds the query from a URL's query string, without a web framework - for tests, a CLI,
        a WebSocket message or a framework without an adapter.

        Args:
            query_string: The query string, without ``?`` (``status=draft&tag=1&tag=2``).
            request: The request, if any - its ``url`` makes the links of a page.
            values: Values of other parameters (path parameters) - they win over the query string.

        Returns:
            The query.

        Raises:
            InvalidRequestQuery: See ``__init__()``.
        """
        return cls.from_query_parameters(parse_qsl(query_string, keep_blank_values=True), request=request, **values)

    @classmethod
    def from_query_parameters(
        cls, parameters: Iterable[tuple[str, str]], *, request: Any = None, **values: Any
    ) -> Self:
        """Builds the query from query parameters as pairs, a name repeated for each of its values.
        A parameter taking a list gets every value, any other the last one; a name the query
        doesn't have is ignored, as a web framework ignores it.

        Args:
            parameters: ``(name, value)`` pairs.
            request: The request, if any.
            values: Values of other parameters - they win over ``parameters``.

        Returns:
            The query.

        Raises:
            InvalidRequestQuery: See ``__init__()``.
        """
        cls.prepare_parameters()
        collected: dict[str, list[str]] = {}
        for name, value in parameters:
            collected.setdefault(name, []).append(value)
        many_value_names = cls.get_many_value_parameter_names()
        arguments: dict[str, Any] = {}
        for name in cls.model_fields:
            if name in collected:
                arguments[name] = collected[name] if name in many_value_names else collected[name][-1]
        arguments.update(values)
        return cls(request=request, **arguments)

    @classmethod
    def get_many_value_parameter_names(cls) -> frozenset[str]:
        """The parameters taking every value of a repeated query parameter, not one - worked out
        once the class's parameters are prepared.

        Returns:
            Their names.
        """
        bucket = cls.get_cache_bucket()
        names: frozenset[str] | None = bucket.get(MANY_VALUE_PARAMETERS_BUCKET_KEY)
        if names is None:
            names = frozenset(
                name
                for name, field_info in cls.model_fields.items()
                if ValueAnnotation.takes_many_values(field_info.annotation, field_info.metadata)
            )
            # The parameters of a class not prepared yet still change.
            if cls in RequestQuery.prepared_classes:
                bucket[MANY_VALUE_PARAMETERS_BUCKET_KEY] = names
        return names

    @classmethod
    def get_cache_bucket(cls) -> dict[Any, Any]:
        """The bucket of ``declarations`` the class keeps - read off the class's own buckets, which
        costs less than asking the cache.

        Returns:
            The bucket.
        """
        bucket = cls.cache_buckets.get(RequestQuery.declarations.bucket_id)
        return bucket if bucket is not None else RequestQuery.declarations.get_owner_bucket(cls)

    @classmethod
    def get_meta_option(cls, name: str) -> Any:
        """One ``Meta`` option of the class: from its own ``Meta``, else the nearest base's.

        Args:
            name: The option.

        Returns:
            The option.
        """
        for klass in cls.__mro__:
            meta = vars(klass).get(META_CLASS_NAME)
            if meta is not None and name in vars(meta):
                return vars(meta)[name]
        return None

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        """Adds the parameters the class's options bring - the search, the ordering, the
        pagination parameters and the others - once pydantic built the class. A parameter the
        class declares itself is kept as declared.

        Args:
            kwargs: The class's keyword arguments.
        """
        super().__pydantic_init_subclass__(**kwargs)
        cls.cache_buckets = {}
        RequestQueryParameters.add_parameter_fields(
            cls,
            {
                parameter_field.name: FieldInfo.from_annotated_attribute(
                    parameter_field.annotation, parameter_field.default
                )
                for option in cls.get_request_options()
                for parameter_field in option.get_parameter_fields()
                if parameter_field.name not in cls.model_fields
            },
            raise_errors=False,
        )

    @classmethod
    def prepare_parameters(cls) -> None:
        """Adds the parameters of ``Meta.filters`` and describes the filter parameters the class
        declares, from the ORM's descriptions of their filters - once per class. It needs the
        models bound (``Hare.bind_models()`` or ``Hare.init()``), not the connections:
        a framework adapter calls it before it reads the parameters for the handlers' signatures,
        and the query calls it when it is first built.

        Raises:
            ConfigurationError: ``Meta.filters`` is wrong - see ``FilterParameterBuilder`` - or the
                queryset's model was unregistered.
        """
        if cls in RequestQuery.prepared_classes:
            return
        queryset = cls.get_meta_option("queryset")
        if isinstance(queryset, QuerySet):
            RequestQueryRegistry.check_model_registered(cls, queryset.model)
            builder = FilterParameterBuilder(cls)
            fields = builder.build_fields(queryset)
            RequestQuery.unprepared_fields.setdefault(cls, dict(cls.__pydantic_fields__))
            RequestQueryParameters.add_parameter_fields(cls, fields)
            RequestQueryRegistry.track_models(cls, {queryset.model, *builder.described_models})
            RequestQuery.bound_models.add(queryset.model)
        RequestQuery.prepared_classes.add(cls)

    @classmethod
    def forget_model(cls, model: type[Model]) -> None:
        """Forgets the declaration and the prepared parameters of every request query class that
        read ``model`` - one on it, or whose filters, search, orderings, fields or include cross
        it. The next use builds them again from the model as it is now.

        Args:
            model: The model that changed.
        """
        for request_query_class in list(RequestQuery.classes_by_model.pop(model, ())):
            RequestQueryRegistry.forget_parameters(cast("type[RequestQuery[Any]]", request_query_class))

    @classmethod
    def forget_all(cls) -> None:
        """Forgets the declaration and the prepared parameters of every request query class."""
        request_query_classes = {*RequestQuery.declarations.owners, *RequestQuery.prepared_classes}
        RequestQuery.classes_by_model.clear()
        for request_query_class in request_query_classes:
            RequestQueryRegistry.forget_parameters(cast("type[RequestQuery[Any]]", request_query_class))

    @classmethod
    def get_request_options(cls) -> tuple[RequestOption, ...]:
        """The class's ``Meta`` options that add parameters.

        Returns:
            The options that are set.
        """
        bucket = cls.get_cache_bucket()
        options: tuple[RequestOption, ...] | None = bucket.get(REQUEST_OPTIONS_BUCKET_KEY)
        if options is None:
            options = bucket[REQUEST_OPTIONS_BUCKET_KEY] = tuple(
                option
                for option_name in PARAMETER_OPTION_NAMES
                if isinstance(option := cls.get_meta_option(option_name), RequestOption)
            )
        return options

    @classmethod
    def get_declaration(cls) -> RequestQueryDeclaration:
        """The class's checked declaration, built the first time it is asked for.

        Returns:
            The declaration.

        Raises:
            ConfigurationError: The class is a base without a queryset, declares a filter, search
                or ordering the model doesn't have or the class's dialects don't run, or its
                queryset's model was unregistered.
        """
        declarations = cls.get_cache_bucket()
        declaration = declarations.get(DECLARATION_BUCKET_KEY)
        # A registration while the declaration is built (a driver or dialect loaded on first use)
        # forgets the half-prepared class - its parameters are put back - so it is built again.
        while declaration is None:
            queryset = cls.get_meta_option("queryset")
            if isinstance(queryset, QuerySet):
                RequestQueryRegistry.check_model_registered(cls, queryset.model)
            cls.prepare_parameters()
            builder = DeclarationBuilder(cls)
            built_declaration = dataclasses.replace(
                builder.build(), on_describe=partial(RequestQueryRegistry.track_described_models, ref(cls))
            )
            if cls not in RequestQuery.prepared_classes:
                continue
            RequestQueryRegistry.track_models(cls, builder.described_models)
            declaration = declarations[DECLARATION_BUCKET_KEY] = built_declaration
        return cast("RequestQueryDeclaration", declaration)

    @classmethod
    def for_model[FactoryModel: Model](
        cls,
        model: type[FactoryModel],
        *,
        name: str | None = None,
        queryset: QuerySet[FactoryModel] | None = None,
        filters: tuple[FilterField, ...] | None = Meta.filters,
        search: SearchConfig | None = Meta.search,
        ordering: OrderingConfig | None = Meta.ordering,
        pagination: Pagination | None = Meta.pagination,
        fields: FieldsConfig | None = Meta.fields,
        include: IncludeConfig | None = Meta.include,
        deleted: DeletedConfig | None = Meta.deleted,
        versions: LatestVersions | None = Meta.versions,
        join_type: Connector = Meta.join_type,
    ) -> type[RequestQuery[FactoryModel]]:
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
            model: The model - bound, so its filters can be checked.
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

        Returns:
            The request query class.

        Raises:
            ConfigurationError: ``model`` isn't a model, ``queryset`` is of another model, ``name``
                isn't an identifier, or the declaration is wrong - as ``check_declarations()``
                finds it.
        """
        if not (isinstance(model, type) and issubclass(model, Model)):
            raise ConfigurationError(f"{cls.__qualname__}.for_model() takes a model class, got {model!r}")
        if queryset is None:
            queryset = model.objects.all()
        elif not isinstance(queryset, QuerySet) or queryset.model is not model:
            raise ConfigurationError(
                f"{cls.__qualname__}.for_model({model.__name__}) takes a queryset of {model.__name__}, "
                f"got {queryset!r}"
            )
        class_name = name if name is not None else f"{model.__name__}RequestQuery"
        if not isinstance(class_name, str) or not class_name.isidentifier():
            raise ConfigurationError(f"{cls.__qualname__}.for_model() name must be an identifier, got {class_name!r}")
        # Named as a class nested in the new one - pydantic leaves such a class out of the fields.
        meta = type(
            META_CLASS_NAME,
            (),
            {
                "__module__": cls.__module__,
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
            },
        )
        base = cls[model] if cls.__pydantic_generic_metadata__["parameters"] else cls  # type: ignore[index]
        request_query_class = cast(
            "type[RequestQuery[FactoryModel]]",
            type(
                class_name, (base,), {"__module__": cls.__module__, "__qualname__": class_name, META_CLASS_NAME: meta}
            ),
        )
        request_query_class.check_declaration()
        return request_query_class

    @classmethod
    def describe_parameters(cls) -> tuple[ParameterDescription, ...]:
        """What each parameter of the class is - for building a UI over it: a filter's key, path,
        lookup, value and relation, the bound it pairs with, the choices of an enum and of an
        option. Needs the models bound, not the connections, as ``get_declaration()``; a class
        forgotten with its model (a live model registered again) describes the model as it is now.

        Returns:
            One description per parameter, in the parameters' order.

        Raises:
            ConfigurationError: See ``get_declaration()``.
        """
        return ParameterDescriber(cls).describe()

    @classmethod
    def check_declaration(cls) -> None:
        """Builds and checks the class's declaration afresh - ``check_declarations()`` for one
        class.

        Raises:
            ConfigurationError: See ``get_declaration()``.
        """
        RequestQuery.declarations.forget_owner(cls)
        cls.get_declaration()

    @classmethod
    def check_declarations(cls) -> None:
        """Builds and checks the declaration of every request query class below this one whose
        queryset's model is registered in the current Hare context - an application calls it once
        its models are set up, so a wrong declaration fails at startup, not on the first request.
        A class of a model the context doesn't have belongs to another application and is skipped.

        Raises:
            ConfigurationError: A declaration is wrong - every wrong one is listed - or no Hare
                context is active.
        """
        apps = HareContext.require_current().apps
        registered_models = set(apps.get_models_iterable()) if apps is not None else set()
        errors = []
        for request_query_class in cls.get_concrete_subclasses():
            queryset = request_query_class.get_meta_option("queryset")
            if isinstance(queryset, QuerySet) and queryset.model not in registered_models:
                continue
            try:
                request_query_class.check_declaration()
            except ConfigurationError as error:
                errors.append(str(error))
        if errors:
            raise ConfigurationError("Wrong request queries:\n" + "\n".join(errors))

    @classmethod
    def get_concrete_subclasses(cls) -> list[type[RequestQuery[Any]]]:
        """The classes below this one that declare a queryset.

        Returns:
            The classes, each once.
        """
        found: dict[type[RequestQuery[Any]], None] = {}
        pending: list[type[RequestQuery[Any]]] = list(cls.__subclasses__())
        while pending:
            subclass = pending.pop()
            if subclass in found:
                continue
            if (
                subclass.get_meta_option("queryset") is not None
                and not subclass.__pydantic_generic_metadata__["origin"]
            ):
                found[subclass] = None
            pending.extend(subclass.__subclasses__())
        return list(found)

    @property
    def request(self) -> Any:
        """The request the parameters came from, None when built without one."""
        return self._request

    def get_request_url(self) -> str | None:
        """The address of the request, with its query string - the links of a page are built from
        it.

        Returns:
            The address a framework adapter set, else the request's ``url`` as text; None without
            either.
        """
        if self._request_url is not None:
            return self._request_url
        url = getattr(self._request, "url", None)
        return None if url is None else str(url)

    def set_request_url(self, url: str) -> None:
        """Sets the address of the request - for a framework whose request's ``url`` isn't the
        whole address with its query string.

        Args:
            url: The address.
        """
        self._request_url = url

    def cache_key(self) -> str:
        """A key naming the query and the values of its parameters, the same for the same request -
        for caching its result together with whatever else the result depends on (the user).

        Returns:
            The class's path and its parameter values as JSON.
        """
        values = self.model_dump(mode="json", exclude_none=True)
        return f"{type(self).__module__}.{type(self).__qualname__}:{json.dumps(values, sort_keys=True)}"

    def get_queryset(self) -> QuerySet[ModelType]:
        """The rows the query starts from - a copy of ``Meta.queryset``. Override it for rows that
        depend on the request; filters on annotations still need them in ``Meta.queryset``, which
        the declaration is checked against.

        Returns:
            The queryset.
        """
        return self.get_declaration().queryset.all()

    def where(self, *conditions: Q, **filters: Any) -> Self:
        """Adds conditions of the handler's own to the request's - they join with ``AND``.

        Args:
            conditions: ``Q`` conditions.
            filters: ``.filter()`` keyword conditions.

        Returns:
            The query itself.
        """
        if conditions or filters:
            self._conditions = (*self._conditions, Q(*conditions, **filters))
        return self

    def order_by(self, *names: OrderingItem) -> Self:
        """Orders the rows by the handler's choice instead of the request's or the default one.

        Args:
            names: Ordering names, ``-`` for descending, or ordering expressions.

        Returns:
            The query itself.
        """
        self._explicit_ordering = names
        return self

    def select_related(self, *fields: str) -> Self:
        """Loads related rows along with the rows.

        Args:
            fields: Relations, as for ``QuerySet.select_related()``.

        Returns:
            The query itself.
        """
        self._queryset_changes = (*self._queryset_changes, lambda queryset: queryset.select_related(*fields))
        return self

    def prefetch_related(self, *lookups: Any) -> Self:
        """Loads related rows with separate queries.

        Args:
            lookups: Relations or ``Prefetch`` objects, as for ``QuerySet.prefetch_related()``.

        Returns:
            The query itself.
        """
        self._queryset_changes = (*self._queryset_changes, lambda queryset: queryset.prefetch_related(*lookups))
        return self

    def annotate(self, **annotations: Any) -> Self:
        """Adds annotations to the rows.

        Args:
            annotations: Annotations by name, as for ``QuerySet.annotate()``.

        Returns:
            The query itself.
        """
        self._queryset_changes = (*self._queryset_changes, lambda queryset: queryset.annotate(**annotations))
        return self

    async def may_see_deleted(self) -> bool:
        """Whether the request may see soft-deleted rows - override it with the application's
        access rules. It is asked only when the request asks for deleted rows with
        ``Meta.deleted``'s parameter.

        Returns:
            True by default - declaring ``Meta.deleted`` lets every request see them.
        """
        return True

    async def apply_deleted(self, queryset: QuerySet[ModelType]) -> QuerySet[ModelType]:
        """Includes or selects the soft-deleted rows the request asks for with ``Meta.deleted``.

        Args:
            queryset: A queryset of the model.

        Returns:
            The queryset - unchanged without ``Meta.deleted`` or when the request asks for none.

        Raises:
            RequestQueryForbidden: The request asks for deleted rows ``may_see_deleted()`` doesn't
                allow it to see.
        """
        deleted = self.get_declaration().deleted
        if deleted is None:
            return queryset
        deleted_rows = deleted.get_requested(RequestQueryParameters.get_parameter_values(self))
        if deleted_rows is not DeletedRows.EXCLUDE and not await self.may_see_deleted():
            raise RequestQueryForbidden(deleted.parameter, "The request may not see deleted rows")
        return deleted.apply(queryset, deleted_rows)

    async def get_rows_queryset(self) -> QuerySet[ModelType]:
        """The rows the query starts from with the handler's changes and the deleted rows the
        request asks for - before any condition.

        Returns:
            The queryset.

        Raises:
            RequestQueryForbidden: See ``apply_deleted()``.
        """
        queryset = await self.apply_deleted(self.get_queryset())
        for change in self._queryset_changes:
            queryset = change(queryset)
        return queryset

    async def get_access_condition(self) -> Q | None:
        """The rows the request may see at all - override it with the application's access rules
        (the request's user, its roles). It joins every query with ``AND``, ``delete()`` included.

        Returns:
            The condition, None for every row.
        """
        return None

    async def after_fetch(self, items: list[ModelType]) -> list[ModelType]:
        """Runs on the rows of ``fetch()``, ``page()`` and ``get()`` once they are fetched - override
        it to load what the rows need from elsewhere.

        Args:
            items: The rows.

        Returns:
            The rows to return.
        """
        return items

    async def get_request_conditions(self, *, skipped_target: ValueTarget | None = None) -> list[Q]:
        """The conditions of the request's parameters and search. The async ``filter_<parameter>``
        methods of the parameters with a value run concurrently; the conditions keep the order the
        parameters are declared in.

        Args:
            skipped_target: The values whose own filters are left out - ``count_by()`` counts a
                field under every filter of the request but its own.

        Returns:
            The parameters' conditions joined by ``Meta.join_type`` as one condition, and the
            search's as another; an empty list when the request filters by nothing.

        Raises:
            TypeError: A filter method returned something other than a ``Q`` or None.
        """
        declaration = self.get_declaration()
        # Each parameter's condition in declaration order, with the method that gave it: an async
        # filter method's is awaited below, every one of them at once.
        method_conditions: list[tuple[str | None, Any]] = []
        for filter_declaration in declaration.filters:
            value = getattr(self, filter_declaration.parameter)
            if value is None or self.is_skipped(filter_declaration, skipped_target):
                continue
            if filter_declaration.method_name is not None:
                method = getattr(self, filter_declaration.method_name)
                method_conditions.append((filter_declaration.method_name, method(value)))
                continue
            method_conditions.append((None, Q(**{cast("str", filter_declaration.filter_key): value})))
        pending_positions = [
            position for position, (__, condition) in enumerate(method_conditions) if inspect.isawaitable(condition)
        ]
        if pending_positions:
            awaited_conditions = await asyncio.gather(
                *(method_conditions[position][1] for position in pending_positions)
            )
            for position, condition in zip(pending_positions, awaited_conditions, strict=True):
                method_conditions[position] = (method_conditions[position][0], condition)
        filter_conditions: list[Q] = []
        for method_name, condition in method_conditions:
            if condition is None:
                continue
            if not isinstance(condition, Q):
                raise TypeError(
                    f"{type(self).__qualname__}.{method_name}() must return a Q or None, got {condition!r}"
                )
            filter_conditions.append(condition)
        conditions = []
        if filter_conditions:
            conditions.append(Q.with_connector(declaration.join_type, *filter_conditions))
        search_condition = self.get_search_condition()
        if search_condition is not None:
            conditions.append(search_condition)
        return conditions

    def get_search_condition(self) -> Q | None:
        """The condition of the request's search.

        Returns:
            The condition, None without a search or a search text.
        """
        search = self.get_declaration().search
        return None if search is None else search.get_condition(RequestQueryParameters.get_parameter_values(self))

    def is_filtered(self) -> bool:
        """Whether anything but the access condition filters the rows - the handler's ``where()``,
        a filter parameter or the search.

        Returns:
            True when the handler added a condition, or a filter parameter or the search text has a
            value.
        """
        if self._conditions:
            return True
        declaration = self.get_declaration()
        if any(getattr(self, filter_declaration.parameter) is not None for filter_declaration in declaration.filters):
            return True
        return self.get_search_condition() is not None

    def crosses_to_many(self, queryset: QuerySet[Any], condition: Q) -> bool:
        """Whether a condition joins a relation to many rows - the query can then return a row more
        than once.

        Args:
            queryset: The queryset the condition filters.
            condition: The condition.

        Returns:
            True when a filter key of the condition or of a condition inside it crosses such a
            relation.

        Raises:
            FieldError: A filter key names no field, relation or annotation.
        """
        declaration = self.get_declaration()
        return any(declaration.get_lookup_info(queryset, key).crosses_to_many for key in condition.filters) or any(
            self.crosses_to_many(queryset, child) for child in condition.children
        )

    async def build_queryset(
        self, method_name: str, *, each_row_once: EachRowOnce, skipped_target: ValueTarget | None = None
    ) -> QuerySet[ModelType]:
        """The queryset with the handler's changes and every condition: the request's parameters
        and search, the handler's ``where()``, the access condition, the latest versions.

        Args:
            method_name: The method asking, for the error.
            each_row_once: How to take each row once when a condition joins a relation to many
                rows.
            skipped_target: The values whose own filters are left out.

        Returns:
            The queryset, not ordered.

        Raises:
            RequestQueryForbidden: See ``apply_deleted()``.
            QueryError: See ``get_rows_once()``.
        """
        queryset = await self.get_rows_queryset()
        conditions = [*await self.get_request_conditions(skipped_target=skipped_target), *self._conditions]
        access_condition = await self.get_access_condition()
        if access_condition is not None:
            conditions.append(access_condition)
        versions = self.get_declaration().versions
        if versions is not None:
            all_versions = await self.apply_deleted(self.get_queryset())
            conditions.append(versions.get_condition(all_versions, access_condition))
        if not conditions:
            return queryset
        queryset = queryset.filter(*conditions)
        if each_row_once is EachRowOnce.NOT_NEEDED or not any(
            self.crosses_to_many(queryset, condition) for condition in conditions
        ):
            return queryset
        if each_row_once is EachRowOnce.DISTINCT:
            return queryset.distinct()
        return await self.get_rows_once(queryset, method_name)

    @staticmethod
    def is_skipped(filter_declaration: FilterDeclaration, skipped_target: ValueTarget | None) -> bool:
        """Whether a filter is one of the left-out values' own.

        Args:
            filter_declaration: The filter.
            skipped_target: The left-out values, None for none.

        Returns:
            True when the filter compares those values - a filter method never is.
        """
        if skipped_target is None or filter_declaration.lookup_info is None:
            return False
        return ValueTarget.of(filter_declaration.lookup_info) == skipped_target

    async def get_filtered_queryset(self) -> QuerySet[ModelType]:
        """The rows the request asks for, each once, not ordered - for what the methods below don't
        do.

        Returns:
            The queryset.
        """
        return await self.build_queryset("get_filtered_queryset", each_row_once=EachRowOnce.DISTINCT)

    def get_ordering(self, queryset: QuerySet[ModelType]) -> tuple[OrderingItem, ...]:
        """The ordering of the rows: the handler's ``order_by()``, else the request's, else
        ``OrderingConfig.default``, else the queryset's own, else the model's ``Meta.ordering`` - and
        then the primary key's fields that aren't ordered by yet, so equal values keep one order. A
        model without a primary key gets no such fields.

        Args:
            queryset: The queryset being ordered.

        Returns:
            The ordering.
        """
        declaration = self.get_declaration()
        ordering = declaration.ordering
        items: tuple[OrderingItem, ...] = ()
        if self._explicit_ordering is not None:
            items = self._explicit_ordering
        elif ordering is not None and (
            requested := ordering.get_requested(RequestQueryParameters.get_parameter_values(self))
        ):
            items = requested
        elif ordering is not None and ordering.default:
            items = ordering.default
        elif queryset.orderings:
            items = tuple(Ordering(field_name, order) for field_name, order in queryset.orderings)
        elif queryset.model._meta.ordering:
            items = tuple(Ordering(field_name, order) for field_name, order in queryset.model._meta.ordering)
        if not declaration.key_ordering_paths:
            return items
        ordered_paths = {path for item in items for path in self.get_ordering_info(queryset, item).paths}
        return (*items, *(path for path in declaration.key_ordering_paths if path not in ordered_paths))

    @staticmethod
    def get_ordering_name(item: OrderingItem) -> str:
        """The name of an ordering item without its direction.

        Args:
            item: A name with ``-`` for descending, or an ordering expression.

        Returns:
            The name.
        """
        return item.field_name if isinstance(item, Ordering) else item.removeprefix(DESCENDING_PREFIX)

    def get_ordering_info(self, queryset: QuerySet[Any], item: OrderingItem) -> OrderingInfo:
        """The ORM's description of one ordering item, from the class's declaration - read once
        per name, not on every request.

        Args:
            queryset: The queryset being ordered.
            item: The ordering item.

        Returns:
            The description: the names the query orders by (a relation's key column, ``pk``'s key
            fields), the fields and the relations crossed.
        """
        return self.get_declaration().get_ordering_info(queryset, self.get_ordering_name(item))

    def apply_selection(
        self, queryset: QuerySet[ModelType], ordering: Iterable[OrderingItem] = ()
    ) -> QuerySet[ModelType]:
        """Loads what the request's fields and include parameters ask for: only the named fields
        (with the primary key and the fields the ordering reads), and the named relations.

        Args:
            queryset: The queryset of the rows.
            ordering: The ordering of the rows - its own fields are loaded too.

        Returns:
            The queryset.
        """
        declaration = self.get_declaration()
        parameter_values = RequestQueryParameters.get_parameter_values(self)
        if declaration.fields is not None:
            ordered_fields: list[str] = []
            for item in ordering:
                ordering_info = self.get_ordering_info(queryset, item)
                if not ordering_info.relations:
                    ordered_fields.extend(ordering_info.paths)
            queryset = declaration.fields.apply(
                queryset, parameter_values, [*declaration.key_ordering_paths, *ordered_fields]
            )
        if declaration.include is not None:
            queryset = declaration.include.apply(queryset, parameter_values, declaration.include_to_many)
        return queryset

    def get_ordered_queryset(self, queryset: QuerySet[ModelType]) -> QuerySet[ModelType]:
        """A queryset ordered by ``get_ordering()``, loading what the request's fields and include
        parameters ask for.

        Args:
            queryset: The filtered queryset.

        Returns:
            The queryset.
        """
        ordering = self.get_ordering(queryset)
        return self.apply_selection(queryset.order_by(*ordering), ordering)

    async def fetch(self) -> list[ModelType]:
        """Every row the request asks for, ordered, without pagination.

        Returns:
            The rows, after ``after_fetch()``.
        """
        queryset = await self.get_filtered_queryset()
        items = list(await self.get_ordered_queryset(queryset))
        return await self.after_fetch(items)

    async def page(self) -> Page[ModelType] | ScrollPage[ModelType] | CursorPage[ModelType]:
        """The page of rows the request asks for - ``Meta.pagination`` builds it.

        Returns:
            A ``Page`` of an ``OffsetPagination``, a ``ScrollPage`` of a ``ScrollPagination``, a
            ``CursorPage`` of a ``CursorPagination``, or the page of a pagination of your own.

        Raises:
            ConfigurationError: The query has no pagination.
            InvalidRequestQuery: The request's cursor isn't a cursor of this query.
        """
        pagination = self.get_declaration().pagination
        if pagination is None:
            raise QueryError(f"{type(self).__qualname__} has no Meta.pagination - use fetch()")
        return cast("Page[ModelType] | ScrollPage[ModelType] | CursorPage[ModelType]", await pagination.get_page(self))

    @overload
    async def get(
        self,
        *,
        does_not_exist_exception: None,
        multiple_objects_returned_exception: GetExceptionArgument = GetException.STANDARD,
    ) -> ModelType | None: ...

    @overload
    async def get(
        self,
        *,
        does_not_exist_exception: type[BaseException] | BaseException | GetException = GetException.STANDARD,
        multiple_objects_returned_exception: GetExceptionArgument = GetException.STANDARD,
    ) -> ModelType: ...

    async def get(
        self,
        *,
        does_not_exist_exception: GetExceptionArgument = GetException.STANDARD,
        multiple_objects_returned_exception: GetExceptionArgument = GetException.STANDARD,
    ) -> ModelType | None:
        """The one row the request asks for.

        Args:
            does_not_exist_exception: As ``QuerySet.get()`` takes it - None gives None when no row
                matches.
            multiple_objects_returned_exception: As ``QuerySet.get()`` takes it - None reads one of
                the matching rows without counting them.

        Returns:
            The row, after ``after_fetch()``; None when no row matches and
            ``does_not_exist_exception`` is None.

        Raises:
            DoesNotExist: No row matches.
            MultipleObjectsReturned: More than one row matches.
        """
        queryset = await self.get_filtered_queryset()
        item = await self.apply_selection(queryset).get(
            does_not_exist_exception=does_not_exist_exception,
            multiple_objects_returned_exception=multiple_objects_returned_exception,
        )
        if item is None:
            return None
        (item,) = await self.after_fetch([item])
        return item

    async def count(self) -> int:
        """How many rows the request asks for.

        Returns:
            The number of rows.
        """
        queryset = await self.get_filtered_queryset()
        return await queryset.count()

    async def exists(self) -> bool:
        """Whether any row matches the request.

        Returns:
            True when a row matches.
        """
        queryset = await self.get_filtered_queryset()
        return await queryset.exists()

    async def delete(self) -> int:
        """Deletes the rows the request asks for. The request's parameters or the handler's
        ``where()`` must filter - the access condition alone doesn't count, so a request without
        filters never deletes everything the user may see.

        Returns:
            How many rows were deleted.

        Raises:
            QueryError: Nothing filters the rows.
        """
        return await RowChanges(self).delete()

    async def update(self, **values: Any) -> int:
        """Updates the rows the request asks for. As with ``delete()``, the request's parameters or
        the handler's ``where()`` must filter; the access condition applies.

        Args:
            values: The new values by field, as for ``QuerySet.update()``.

        Returns:
            How many rows were updated.

        Raises:
            QueryError: Nothing filters the rows, or no value is given.
        """
        return await RowChanges(self).update(values)

    async def get_for_update(self) -> ModelType:
        """The one row the request asks for, locked until the surrounding transaction ends - for a
        handler that changes it (``SELECT ... FOR UPDATE`` where the database locks rows; SQLite
        serializes writers instead). Call it inside a transaction. The request's fields and
        include parameters don't apply: only the row itself is locked and loaded.

        Returns:
            The row, after ``after_fetch()``.

        Raises:
            DoesNotExist: No row matches.
            MultipleObjectsReturned: More than one row matches.
            QueryError: A filter crosses a relation to many rows on a model without a primary key.
        """
        return await RowChanges(self).get_for_update()

    async def count_by(self, *names: str, limit: int | None = None) -> dict[str, dict[Any, int]]:
        """How many of the rows the request asks for have each value of each field - for the
        filters of a list, which show how many rows each choice leaves.

        Each field is counted under every filter of the request except its own
        (``?status=draft`` still counts every status), with the search, the handler's ``where()``
        and the access condition. A relation counts by the related key - a tuple for a composite
        key; a relation to many rows counts each row once per related row; ``None`` counts the rows
        without a value.

        Args:
            names: The fields - fields or relations, through relations (``status``, ``author``,
                ``tags``, ``author__profile__city``).
            limit: How many of the most frequent values of each field to return, None for all.

        Returns:
            For each field, the count of rows by value, most frequent first.

        Raises:
            FieldError: A name isn't a field or relation of the model.
            QueryError: ``limit`` isn't a positive int, or a filter crosses a relation to many rows
                on a model without a primary key.
        """
        return await ValueCounter(self).count(names, limit)

    async def get_rows_once(self, queryset: QuerySet[ModelType], method_name: str) -> QuerySet[ModelType]:
        """The rows of a queryset whose filters join a relation to many rows, each once - as the
        rows whose primary key the queryset selects, so no ``DISTINCT`` is needed.

        Args:
            queryset: The filtered queryset.
            method_name: The method asking, for the error.

        Returns:
            The rows, each once.

        Raises:
            QueryError: The model has no primary key to tell its rows apart.
        """
        key_paths = self.get_declaration().key_ordering_paths
        if not key_paths:
            raise QueryError(
                f"{type(self).__qualname__}.{method_name}() can't take a filter across a relation to many rows - "
                f"{queryset.model.__name__} has no primary key to tell its rows apart"
            )
        rows_queryset = await self.get_rows_queryset()
        return rows_queryset.filter(pk__in=queryset.values(*key_paths))


Caches.register(RequestQuery)
