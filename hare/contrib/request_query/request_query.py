"""``RequestQuery`` - the rows a request asks for, declared once: the queryset, the parameters it
filters by, the search, the orderings a request may choose and the pagination."""

from __future__ import annotations

import json
from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, ClassVar, Self, overload
from urllib.parse import parse_qsl

from pydantic import BaseModel, ConfigDict, PrivateAttr, ValidationError
from pydantic.fields import FieldInfo

from hare.contrib.request_query.api_getter.api_getter import ApiGetter
from hare.contrib.request_query.api_getter.api_source import ApiSource
from hare.contrib.request_query.api_getter.api_value_mapping import ApiValueMapping
from hare.contrib.request_query.api_getter.enums import ApiGetterTrigger
from hare.contrib.request_query.constants import (
    GENERATION_BUCKET_KEY,
    INVALID_VALUE_ERROR_TYPE,
    LOOKUP_SEPARATOR,
    MANY_VALUE_PARAMETERS_BUCKET_KEY,
    META_CLASS_NAME,
    PARAMETER_OPTION_NAMES,
    REQUEST_OPTIONS_BUCKET_KEY,
)
from hare.contrib.request_query.declaration.filter_declaration import FilterDeclaration
from hare.contrib.request_query.declaration.meta_option_checks import MetaOptionChecks
from hare.contrib.request_query.declaration.request_query_declaration import RequestQueryDeclaration
from hare.contrib.request_query.declaration.request_query_factory import RequestQueryFactory
from hare.contrib.request_query.declaration.request_query_generation import RequestQueryGeneration
from hare.contrib.request_query.declaration.request_query_generation_builder import (
    RequestQueryGenerationBuilder,
)
from hare.contrib.request_query.declaration.request_query_inputs import RequestQueryInputs
from hare.contrib.request_query.declaration.request_query_parameters import RequestQueryParameters
from hare.contrib.request_query.declaration.request_query_registry import RequestQueryRegistry
from hare.contrib.request_query.descriptions.parameter_describer import ParameterDescriber
from hare.contrib.request_query.descriptions.parameter_description import ParameterDescription
from hare.contrib.request_query.enums import DeletedRows, EachRowOnce, ParameterDefault
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
from hare.contrib.request_query.request_query_building import RequestQueryBuilding
from hare.contrib.request_query.row_changes import RowChanges
from hare.contrib.request_query.value_annotation import ValueAnnotation
from hare.contrib.request_query.value_counter import ValueCounter
from hare.contrib.request_query.value_target import ValueTarget
from hare.core.caching.cache_bucket import CacheBucket
from hare.core.caching.caches import Caches
from hare.core.hare_context import HareContext
from hare.exceptions import ConfigurationError, QueryError, ValidationError as FieldValidationError
from hare.models import Model
from hare.query.enums import Connector, GetException
from hare.query.expressions import Q
from hare.query.expressions.ordering import Ordering
from hare.query.queryset import QuerySet
from hare.query.queryset.selection.statement_selection import StatementSelection

if TYPE_CHECKING:
    from hare.contrib.request_query.results.cursor_page import CursorPage
    from hare.contrib.request_query.results.page import Page
    from hare.contrib.request_query.results.scroll_page import ScrollPage
    from hare.core.apps.declarations import ModelReference
    from hare.dialects.base.dialect import Dialect
    from hare.fields.relations.swappable_model_reference import SwappableModelReference
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
    of one dialect (``dialect``) - each of hare's dialects has one in its package - checks
    against that dialect only, and may use the lookups only it runs.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True, populate_by_name=True)

    #: The dialect the class's filters are checked against, None for every dialect.
    dialect: ClassVar[Dialect | None] = None

    #: The buckets of the caches the class reads (``Cache.get_bucket()``) - a dict of its
    #: own for every request query class.
    cache_buckets: ClassVar[dict[int, Any]] = {}

    #: True only on an unpublished candidate or a completed generated parameter class.
    is_parameter_schema: ClassVar[bool] = False
    parameter_generation: ClassVar[RequestQueryGeneration | None] = None

    class Meta:
        """The options of a request query.

        Attributes:
            queryset: The rows the query starts from. A class without one only serves as a base.
            search: A ``SearchConfig``, None without a search.
            ordering: An ``OrderingConfig``, None when a request can't choose the ordering.
            pagination: An ``OffsetPagination``, a ``ScrollPagination`` or a ``CursorPagination``,
                None without pagination.
            filters: An iterable of ``FilterField`` - the fields a request may filter by and each
                one's lookups: ``FilterField("status", lookups=(Lookup.EXACT, Lookup.IN))`` makes
                the parameters ``status`` and ``status__in``, typed by the ORM. None for only the
                parameters the class declares.
            join_type: How the conditions of the parameters join - ``Connector.AND`` or ``Connector.OR``.
            fields: A ``FieldsConfig``, None when a request can't choose the fields to load.
            include: An ``IncludeConfig``, None when a request can't ask for relations.
            deleted: A ``DeletedConfig``, None when a request can't see soft-deleted rows.
            versions: ``LatestVersions()`` to keep only the latest version of each record of a
                ``VersionedModel``, None for every version.
            api_getter: An iterable of ``ApiSource`` - HTTP APIs of other services the rows are filled
                from - and ``ApiValueMapping`` - values of the rows copied to other attributes; None
                for none.
        """

        queryset: QuerySet[Any] | None = None
        filters: Iterable[FilterField] | None = None
        search: SearchConfig | None = None
        ordering: OrderingConfig | None = None
        pagination: Pagination | None = OffsetPagination()
        join_type: Connector = Connector.AND
        fields: FieldsConfig | None = None
        include: IncludeConfig | None = None
        deleted: DeletedConfig | None = None
        versions: LatestVersions | None = None
        api_getter: Iterable[ApiSource | ApiValueMapping] | None = None

    _request: Any = PrivateAttr(default=None)
    _request_url: str | None = PrivateAttr(default=None)
    # Tuples, not lists with a default_factory - pydantic reads a factory's signature on every
    # instance it makes.
    _conditions: tuple[Q, ...] = PrivateAttr(default=())
    _explicit_ordering: tuple[OrderingItem, ...] | None = PrivateAttr(default=None)
    _queryset_changes: tuple[Callable[[QuerySet[Any]], QuerySet[Any]], ...] = PrivateAttr(default=())

    def __new__(cls, *args: Any, **kwargs: Any) -> Self:
        """Allocate from the completed generation without changing the source class."""
        return object.__new__(cls if cls.is_parameter_schema else cls.prepare_parameters())

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
        declaration = type(self).get_declaration()
        try:
            super().__init__(
                **{name: value for name, value in values.items() if value is not ParameterDefault.MISSING}
            )
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
        for bounds in declaration.bounds:
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
        Aliases sharing an input root receive the same value. The root keeps all repeated
        values when any of its fields needs a list or an indexed alias path; Pydantic checks
        whether that shared representation satisfies each field.
        An explicit field value removes that field's URL alias roots for every consumer.
        Other fields sharing those roots need another accepted input or a default.

        Args:
            parameters: ``(name, value)`` pairs.
            request: The request, if any.
            values: Values of other parameters - they win over ``parameters``.

        Returns:
            The query.

        Raises:
            InvalidRequestQuery: See ``__init__()``.
        """
        return RequestQueryInputs.from_query_parameters(cls, parameters, request=request, **values)

    @classmethod
    def get_many_value_parameter_names(cls) -> frozenset[str]:
        """The parameters taking every value of a repeated query parameter, not one - worked out
        once the class's parameters are prepared.

        Returns:
            Their names.
        """
        if not cls.is_parameter_schema:
            return cls.prepare_parameters().get_many_value_parameter_names()
        bucket = cls.get_cache_bucket()
        names: frozenset[str] | None = bucket.get(MANY_VALUE_PARAMETERS_BUCKET_KEY)
        if names is None:
            names = frozenset(
                name
                for name, field_info in cls.model_fields.items()
                if ValueAnnotation.takes_many_values(field_info.annotation, field_info.metadata)
            )
            bucket[MANY_VALUE_PARAMETERS_BUCKET_KEY] = names
        return names

    @classmethod
    def get_cache_bucket(cls) -> CacheBucket[Any]:
        """The bounded declaration bucket attached to this request query class.

        Returns:
            The bucket.
        """
        return RequestQueryRegistry.declarations.get_bucket(cls)

    @classmethod
    def get_meta_option(cls, name: str) -> Any:
        """One ``Meta`` option of the class: from its own ``Meta``, else the nearest base's.

        Args:
            name: The option.

        Returns:
            The option.

        Raises:
            ConfigurationError: The option lacks required structure.
        """
        for klass in cls.__mro__:
            meta = vars(klass).get(META_CLASS_NAME)
            if meta is not None and name in vars(meta):
                value = vars(meta)[name]
                MetaOptionChecks.check_option(cls.__qualname__, name, value)
                return value
        return None

    @classmethod
    def __init_subclass__(cls, *, parameter_schema: bool = False, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls.is_parameter_schema = parameter_schema
        cls.parameter_generation = None

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
        if cls.is_parameter_schema:
            return
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
    def prepare_parameters(cls) -> type[Self]:
        """Return the completed Pydantic class for this declaration.

        The source class retains only its declared fields and option parameters.
        Use the returned class for ``model_fields``, ``TypeAdapter`` and nested
        Pydantic annotations. Ordinary construction selects this class automatically.
        Each returned class is a snapshot; registry invalidation affects future calls.
        """
        if cls.is_parameter_schema:
            return cls
        # Read without the lock (one read, atomic under the GIL hare runs with); it guards the build.
        generation = cls.get_cache_bucket().get(GENERATION_BUCKET_KEY)
        if generation is not None:
            return generation.schema
        with RequestQueryRegistry.lock:
            bucket = cls.get_cache_bucket()
            generation = bucket.get(GENERATION_BUCKET_KEY)
            while generation is None:
                revision = RequestQueryRegistry.revision
                try:
                    candidate = RequestQueryGenerationBuilder.build(cls)
                except BaseException:
                    RequestQueryRegistry.forget_parameters(cls)
                    raise
                if revision != RequestQueryRegistry.revision:
                    continue
                generation = bucket[GENERATION_BUCKET_KEY] = candidate
            return generation.schema

    @classmethod
    def forget_model(cls, model: type[Model]) -> None:
        """Forgets the declaration and the prepared parameters of every request query class that
        read ``model`` - one on it, or whose filters, search, orderings, fields or include cross
        it. The next use builds them again from the model as it is now.

        Args:
            model: The model that changed.
        """
        RequestQueryRegistry.forget_model(model)

    @classmethod
    def forget_all(cls) -> None:
        """Forgets the declaration and the prepared parameters of every request query class."""
        RequestQueryRegistry.forget_all()

    @classmethod
    def get_request_options(cls) -> tuple[RequestOption, ...]:
        """The class's ``Meta`` options that add parameters.

        Returns:
            The options that are set.
        """
        if cls.parameter_generation is not None:
            return cls.parameter_generation.options
        bucket = cls.get_cache_bucket()
        options: tuple[RequestOption, ...] | None = bucket.get(REQUEST_OPTIONS_BUCKET_KEY)
        if options is None:
            options = bucket[REQUEST_OPTIONS_BUCKET_KEY] = tuple(
                option
                for option_name in PARAMETER_OPTION_NAMES
                if (option := cls.get_meta_option(option_name)) is not None
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
        schema = cls.prepare_parameters()
        generation = schema.parameter_generation
        if generation is None or generation.declaration is None:
            raise ConfigurationError(f"{cls.__qualname__}.Meta.queryset is required")
        declaration = generation.declaration
        RequestQueryRegistry.check_model_registered(cls, declaration.queryset.model)
        return declaration

    @overload
    @classmethod
    def for_model[FactoryModel: Model](
        cls,
        model: type[FactoryModel],
        *,
        name: str | None = None,
        queryset: QuerySet[FactoryModel] | None = None,
        filters: Iterable[FilterField] | None = Meta.filters,
        search: SearchConfig | None = Meta.search,
        ordering: OrderingConfig | None = Meta.ordering,
        pagination: Pagination | None = Meta.pagination,
        fields: FieldsConfig | None = Meta.fields,
        include: IncludeConfig | None = Meta.include,
        deleted: DeletedConfig | None = Meta.deleted,
        versions: LatestVersions | None = Meta.versions,
        join_type: Connector = Meta.join_type,
        api_getter: Iterable[ApiSource | ApiValueMapping] | None = Meta.api_getter,
    ) -> type[RequestQuery[FactoryModel]]: ...

    @overload
    @classmethod
    def for_model(
        cls,
        model: str | SwappableModelReference,
        *,
        name: str | None = None,
        queryset: QuerySet[Any] | None = None,
        filters: Iterable[FilterField] | None = Meta.filters,
        search: SearchConfig | None = Meta.search,
        ordering: OrderingConfig | None = Meta.ordering,
        pagination: Pagination | None = Meta.pagination,
        fields: FieldsConfig | None = Meta.fields,
        include: IncludeConfig | None = Meta.include,
        deleted: DeletedConfig | None = Meta.deleted,
        versions: LatestVersions | None = Meta.versions,
        join_type: Connector = Meta.join_type,
        api_getter: Iterable[ApiSource | ApiValueMapping] | None = Meta.api_getter,
    ) -> type[RequestQuery[Any]]: ...

    @classmethod
    def for_model(
        cls,
        model: ModelReference,
        *,
        name: str | None = None,
        queryset: QuerySet[Any] | None = None,
        filters: Iterable[FilterField] | None = Meta.filters,
        search: SearchConfig | None = Meta.search,
        ordering: OrderingConfig | None = Meta.ordering,
        pagination: Pagination | None = Meta.pagination,
        fields: FieldsConfig | None = Meta.fields,
        include: IncludeConfig | None = Meta.include,
        deleted: DeletedConfig | None = Meta.deleted,
        versions: LatestVersions | None = Meta.versions,
        join_type: Connector = Meta.join_type,
        api_getter: Iterable[ApiSource | ApiValueMapping] | None = Meta.api_getter,
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
        return RequestQueryFactory.for_model(
            cls,
            model,
            name=name,
            queryset=queryset,
            filters=filters,
            search=search,
            ordering=ordering,
            pagination=pagination,
            fields=fields,
            include=include,
            deleted=deleted,
            versions=versions,
            join_type=join_type,
            api_getter=api_getter,
        )

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
        return ParameterDescriber(cls.prepare_parameters()).describe()

    @classmethod
    def check_declaration(cls) -> None:
        """Builds and checks the class's declaration afresh - ``check_declarations()`` for one
        class.

        Raises:
            ConfigurationError: See ``get_declaration()``.
        """
        RequestQueryRegistry.forget_parameters(cls)
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
                not subclass.is_parameter_schema
                and subclass.get_meta_option("queryset") is not None
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

    async def loads_api_values(self) -> bool:
        """Whether the rows the request reads are filled from ``Meta.api_getter`` - override it to leave
        them as they are for some requests (a request of another service asking for the bare rows).

        Returns:
            True by default.
        """
        return True

    async def load_api_values(
        self, items: list[ModelType], trigger: ApiGetterTrigger = ApiGetterTrigger.READ
    ) -> list[ModelType]:
        """Fills rows from ``Meta.api_getter`` - the sources whose ``triggers`` run on ``trigger`` and
        some of whose targets the request's ``fields`` names (all of them without ``fields``). ``fetch()``,
        ``page()`` and ``get()`` call it for the rows they read; a handler calls it for the rows it created
        or updated (``ApiGetterTrigger.CREATE``, ``ApiGetterTrigger.UPDATE``).

        Args:
            items: The rows.
            trigger: What the rows come from.

        Returns:
            The rows.

        Raises:
            ApiGetterError: A required value couldn't be loaded - see ``ApiGetter.set()``.
        """
        declaration = self.get_declaration()
        if not items or not (declaration.api_sources or declaration.api_mappings):
            return items
        requested_names = self.get_requested_names()
        api_mappings = [
            mapping
            for mapping in declaration.api_mappings
            if requested_names is None or ApiSource.get_root_name(mapping.set_key) in requested_names
        ]
        await ApiGetter.set(
            items, declaration.api_sources, api_mappings, trigger=trigger, requested_names=requested_names
        )
        return items

    def get_requested_names(self) -> frozenset[str] | None:
        """The names the request asks to load - the fields of its ``fields`` parameter and the first parts
        of the relations of its ``include`` parameter.

        Returns:
            The names, None when the request doesn't choose the fields.
        """
        declaration = self.get_declaration()
        if declaration.fields is None:
            return None
        parameter_values = RequestQueryParameters.get_parameter_values(self)
        field_names = declaration.fields.get_requested_names(parameter_values)
        if not field_names:
            return None
        relation_names = (
            RequestOption.split_names(parameter_values.get(declaration.include.parameter))
            if declaration.include is not None
            else ()
        )
        return frozenset([*field_names, *(relation.split(LOOKUP_SEPARATOR, 1)[0] for relation in relation_names)])

    async def finish_rows(self, items: list[ModelType]) -> list[ModelType]:
        """Fills the rows a request read from ``Meta.api_getter`` (``loads_api_values()``), then runs
        ``after_fetch()`` on them.

        Args:
            items: The rows.

        Returns:
            The rows to return.
        """
        if await self.loads_api_values():
            items = await self.load_api_values(items)
        return await self.after_fetch(items)

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
        """
        return await RequestQueryBuilding.get_request_conditions(self, skipped_target=skipped_target)

    def get_search_condition(self) -> Q | None:
        """The condition of the request's search.

        Returns:
            The condition, None without a search or a search text.
        """
        return RequestQueryBuilding.get_search_condition(self)

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
        return RequestQueryBuilding.crosses_to_many(self, queryset, condition)

    async def build_queryset(
        self,
        method_name: str,
        *,
        each_row_once: EachRowOnce,
        skipped_target: ValueTarget | None = None,
        needs_filter: bool = False,
    ) -> QuerySet[ModelType]:
        """The queryset with the handler's changes and every condition: the request's parameters
        and search, the handler's ``where()``, the access condition, the latest versions.

        Args:
            method_name: The method asking, for the error.
            each_row_once: How to take each row once when a condition joins a relation to many
                rows.
            skipped_target: The values whose own filters are left out.
            needs_filter: Refuse a request nothing but the access condition filters - a delete or an
                update never changes every row the user may see.

        Returns:
            The queryset, not ordered.

        Raises:
            RequestQueryForbidden: See ``apply_deleted()``.
            QueryError: See ``get_rows_once()``.
            InvalidRequestQuery: ``needs_filter`` and neither the request's parameters and search nor
                the handler's ``where()`` give a condition - a filter method returning None gives
                none; the request's fault, answered as an invalid request.
        """
        return await RequestQueryBuilding.build_queryset(
            self, method_name, each_row_once=each_row_once, skipped_target=skipped_target, needs_filter=needs_filter
        )

    @staticmethod
    def is_skipped(filter_declaration: FilterDeclaration, skipped_target: ValueTarget | None) -> bool:
        """Whether a filter is one of the left-out values' own.

        Args:
            filter_declaration: The filter.
            skipped_target: The left-out values, None for none.

        Returns:
            True when the filter compares those values - a filter method never is.
        """
        return RequestQueryBuilding.is_skipped(filter_declaration, skipped_target)

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
        return RequestQueryBuilding.get_ordering(self, queryset)

    @staticmethod
    def get_ordering_name(item: OrderingItem) -> str:
        """The name of an ordering item without its direction.

        Args:
            item: A name with ``-`` for descending, or an ordering expression.

        Returns:
            The name.
        """
        return RequestQueryBuilding.get_ordering_name(item)

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
        return RequestQueryBuilding.get_ordering_info(self, queryset, item)

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
        return RequestQueryBuilding.apply_selection(self, queryset, ordering)

    def get_ordered_queryset(self, queryset: QuerySet[ModelType]) -> QuerySet[ModelType]:
        """A queryset ordered by ``get_ordering()``, loading what the request's fields and include
        parameters ask for.

        Args:
            queryset: The filtered queryset.

        Returns:
            The queryset.
        """
        return RequestQueryBuilding.get_ordered_queryset(self, queryset)

    @staticmethod
    @contextmanager
    def refusing_invalid_values() -> Generator[None]:
        """Refuses a value of the request its field or the database doesn't take - out of the
        column's range, too long, holding a null byte - as an invalid request.

        Raises:
            InvalidRequestQuery: A field refused a value.
        """
        try:
            yield
        except FieldValidationError as error:
            raise InvalidRequestQuery([{"loc": [], "msg": str(error), "type": INVALID_VALUE_ERROR_TYPE}]) from error

    async def fetch(self) -> list[ModelType]:
        """Every row the request asks for, ordered, without pagination.

        Returns:
            The rows, after ``finish_rows()``.

        Raises:
            InvalidRequestQuery: See ``refusing_invalid_values()``.
        """
        with self.refusing_invalid_values():
            queryset = await self.get_filtered_queryset()
            items = list(await self.get_ordered_queryset(queryset))
        return await self.finish_rows(items)

    async def page(self) -> Page[ModelType] | ScrollPage[ModelType] | CursorPage[ModelType]:
        """The page of rows the request asks for - ``Meta.pagination`` builds it.

        Returns:
            A ``Page`` of an ``OffsetPagination``, a ``ScrollPage`` of a ``ScrollPagination``, a
            ``CursorPage`` of a ``CursorPagination``, or the page of a pagination of your own.

        Raises:
            QueryError: The query has no pagination.
            InvalidRequestQuery: The request's cursor isn't a cursor of this query; see
                ``refusing_invalid_values()``.
        """
        pagination = self.get_declaration().pagination
        if pagination is None:
            raise QueryError(f"{type(self).__qualname__} has no Meta.pagination - use fetch()")
        with self.refusing_invalid_values():
            page = await pagination.get_page(self)
        return page

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
            The row, after ``finish_rows()``; None when no row matches and
            ``does_not_exist_exception`` is None.

        Raises:
            DoesNotExist: No row matches.
            MultipleObjectsReturned: More than one row matches.
            InvalidRequestQuery: See ``refusing_invalid_values()``.
        """
        with self.refusing_invalid_values():
            queryset = await self.get_filtered_queryset()
            item = await self.apply_selection(queryset).get(
                does_not_exist_exception=does_not_exist_exception,
                multiple_objects_returned_exception=multiple_objects_returned_exception,
            )
        if item is None:
            return None
        (item,) = await self.finish_rows([item])
        return item

    async def count(self) -> int:
        """How many rows the request asks for.

        Returns:
            The number of rows.

        Raises:
            InvalidRequestQuery: See ``refusing_invalid_values()``.
        """
        with self.refusing_invalid_values():
            queryset = await self.get_filtered_queryset()
            # Counted without the relations a fetch prefetches.
            return await queryset.prefetch_related(None).count()

    async def exists(self) -> bool:
        """Whether any row matches the request.

        Returns:
            True when a row matches.

        Raises:
            InvalidRequestQuery: See ``refusing_invalid_values()``.
        """
        with self.refusing_invalid_values():
            queryset = await self.get_filtered_queryset()
            return await queryset.prefetch_related(None).exists()

    async def delete(self) -> int | None:
        """Deletes the rows the request asks for. The request's parameters or the handler's
        ``where()`` must filter - the access condition alone doesn't count, so a request without
        filters never deletes everything the user may see.

        Returns:
            How many rows were deleted, or None if the driver reports no row count.

        Raises:
            InvalidRequestQuery: Nothing filters the rows.
            InvalidRequestQuery: See ``refusing_invalid_values()``.
        """
        with self.refusing_invalid_values():
            return await RowChanges(self).delete()

    async def update(self, **values: Any) -> int | None:
        """Updates the rows the request asks for. As with ``delete()``, the request's parameters or
        the handler's ``where()`` must filter; the access condition applies.

        Args:
            values: The new values by field, as for ``QuerySet.update()``.

        Returns:
            How many rows were updated, or None if the driver reports no row count.

        Raises:
            QueryError: No value is given.
            InvalidRequestQuery: Nothing filters the rows; see ``refusing_invalid_values()`` - a new
                value its field refuses too.
        """
        with self.refusing_invalid_values():
            return await RowChanges(self).update(values)

    async def get_for_update(self) -> ModelType:
        """The one row the request asks for, locked until the surrounding transaction ends - for a
        handler that changes it (``SELECT ... FOR UPDATE``; a database locking no row refuses it). Call it
        inside a transaction. The request's fields and
        include parameters don't apply, nor does ``Meta.api_getter``: only the row itself is locked and loaded.

        Returns:
            The row, after ``after_fetch()``.

        Raises:
            DoesNotExist: No row matches.
            MultipleObjectsReturned: More than one row matches.
            QueryError: A filter crosses a relation to many rows on a model without a primary key.
            InvalidRequestQuery: See ``refusing_invalid_values()``.
        """
        with self.refusing_invalid_values():
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
            InvalidRequestQuery: See ``refusing_invalid_values()``.
        """
        with self.refusing_invalid_values():
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
        return rows_queryset.filter(pk__in=StatementSelection.get_values_rows_queryset(queryset).values(*key_paths))


Caches.register(RequestQuery)
